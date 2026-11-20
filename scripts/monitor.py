#!/usr/bin/env python3
"""Monitoramento diário de anúncios de lojas na Meta Ad Library.

Guarda cada coleta num SQLite (padrão: ./ads-monitor/monitor.db, ou $ADS_MONITOR_DIR)
para acompanhar produtos anunciados, anúncios por produto, tempo no ar e mudanças. Produtos são identificados só pelo link do anúncio.

Comandos:
  add      cadastra uma loja (por ID de página, URL da Ad Library e/ou site)
  resolve  descobre quais páginas anunciam um site/marca
  list     lista lojas cadastradas e última coleta
  remove   remove uma loja (e o histórico dela)
  run      coleta os anúncios ativos de todas (ou algumas) lojas e grava um snapshot
  report   relatório de mudanças: produtos, anúncios por produto, dias no ar, novos/pausados

Exemplos:
  python monitor.py add --name "Loja X" --site lojax.com.br --country BR
  python monitor.py add --name "Loja Y" --page-id 123456789 --page-id 987654321
  python monitor.py run
  python monitor.py report --out relatorio.md --csv-dir exports/
"""
import argparse
import json
import re
import os
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape_ads  # noqa: E402
from build_url import build_url  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, page_ids TEXT DEFAULT '[]',
  urls TEXT DEFAULT '[]', domains TEXT DEFAULT '[]', country TEXT DEFAULT 'ALL', created_at TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, store_id INTEGER, run_at TEXT, n_ads INTEGER, total_reported INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS ads (
  ad_id TEXT, store_id INTEGER, page_id TEXT, page_name TEXT, first_seen TEXT, last_seen TEXT,
  start_date INTEGER, end_date INTEGER, is_active INTEGER, format TEXT, title TEXT, body TEXT, cta TEXT,
  link_url TEXT, platforms TEXT, media_url TEXT, collation_count INTEGER, data_json TEXT,
  PRIMARY KEY (ad_id, store_id));
CREATE TABLE IF NOT EXISTS sightings (
  run_id INTEGER, ad_id TEXT, collation_count INTEGER, PRIMARY KEY (run_id, ad_id));
CREATE TABLE IF NOT EXISTS ad_products (
  store_id INTEGER, ad_id TEXT, product_key TEXT, PRIMARY KEY (store_id, ad_id, product_key));
CREATE TABLE IF NOT EXISTS products (
  store_id INTEGER, product_key TEXT, name TEXT, url TEXT, image TEXT, first_seen TEXT, last_seen TEXT,
  PRIMARY KEY (store_id, product_key));
"""

KEEP_QUERY = {"id", "product_id", "productid", "pid", "sku", "p", "produto"}


# ---------------- utilidades ----------------

def db_open(path):
    os.makedirs(path, exist_ok=True)
    con = sqlite3.connect(os.path.join(path, "monitor.db"))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:  # bancos criados antes da coluna existir
        con.execute("ALTER TABLE runs ADD COLUMN capped INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:  # caminho da imagem de capa do anúncio (criativos/<loja>/<id>.jpg)
        con.execute("ALTER TABLE ads ADD COLUMN cover TEXT")
    except sqlite3.OperationalError:
        pass
    return con


def norm_domain(d):
    d = d.strip().lower()
    if "://" in d:
        d = urlparse(d).netloc
    return d.split("/")[0].removeprefix("www.")


STORE_PREFIXES = ("m.", "shop.", "store.", "loja.", "go.", "try.")


def store_domain(d):
    """m.loja.com / shop.loja.com -> loja.com (lojas usam vários subdomínios nos anúncios)."""
    d = norm_domain(d)
    for p in STORE_PREFIXES:
        if d.startswith(p) and d.count(".") >= 2:
            return d[len(p):]
    return d


def links_to(keys, dom):
    return any(k.split("/")[0] == dom or k.split("/")[0].endswith("." + dom) for k in keys)


def get_source(a):
    s = getattr(a, "source", None) or os.environ.get("ADS_SOURCE")
    if not s:
        s = "scrapecreators" if os.environ.get("SCRAPECREATORS_API_KEY") else "browser"
    return s


_SC = None


def sc():
    global _SC
    if _SC is None:
        from sc_client import ScrapeCreators
        _SC = ScrapeCreators()
    return _SC


def product_key(url):
    """Agrupa links que levam ao mesmo produto: ignora UTMs, variantes, coleções."""
    if not url or "{{" in url:
        return None
    u = urlparse(url if "://" in url else "https://" + url)
    host = u.netloc.lower().removeprefix("www.")
    if host.endswith("facebook.com") and u.path.startswith("/l.php"):
        target = parse_qs(u.query).get("u")
        return product_key(target[0]) if target else None
    if not host:
        return None
    host = store_domain(host)  # m.loja.com e shop.loja.com contam como a mesma loja
    path = unquote(u.path).lower().rstrip("/")
    for marker in ("/products/", "/product/", "/funnel/", "/pages/news-", "/lp/"):
        if marker in path:  # página de produto ou advertorial do produto -> mesmo produto
            path = "/products/" + clean_slug(path.split(marker, 1)[1].split("/")[0], host)
            break
    q = {k.lower(): v[0] for k, v in parse_qs(u.query).items() if k.lower() in KEEP_QUERY}
    key = host + (path or "/")
    if q:
        key += "?" + "&".join(f"{k}={v}" for k, v in sorted(q.items()))
    return key


LEAD_WORDS = {"news", "adv", "advertorial", "article", "blog", "lp", "landing", "discover", "new", "hot", "sale",
              "buy", "shop", "get", "official", "the", "best", "top"}
ACRONYMS = {"ssd", "dvd", "tv", "pc", "gps", "rgb", "hd", "mp3", "mp4", "lcd", "usb", "led", "uv", "rv",
            "utv", "atv", "xl", "xxl", "3d", "4k", "8k", "5g", "dc", "ac", "bbq", "diy", "cpr", "ems"}


def clean_slug(slug, host=""):
    """Reduz o link ao "núcleo" do produto, sem abrir a página:
    discover-sakerplus-usb-heated-scarf-with-3-heat-levels-7 -> usb-heated-scarf-with-3-heat-levels
    news-solar-power-bank-cp-1778132558913 -> solar-power-bank"""
    slug = re.sub(r"-(cp|v|var|id)?-?\d{6,}$", "", slug)            # ids longos de campanha
    tokens = [t for t in re.split(r"-+", slug) if t]
    brand = host.split(".")[0] if host else ""
    changed = True
    while changed and len(tokens) > 2:                               # prefixos: palavras de funil e a marca da loja
        changed = False
        t = tokens[0]
        if t in LEAD_WORDS or (brand and len(t) >= 4 and brand.startswith(t)):
            tokens, changed = tokens[1:], True
    while len(tokens) > 2:                                           # sufixos de variante: -7, -51, -v2, -qzw, -xy
        t = tokens[-1]
        if re.fullmatch(r"\d{1,3}|v\d+", t) or (len(t) <= 4 and not re.search(r"[aeiou]", t) and t not in ACRONYMS):
            tokens = tokens[:-1]
        else:
            break
    return "-".join(tokens)


REAL_SHORT = {"set", "kit", "box", "bag", "pen", "cup", "mat", "pad", "car", "toy", "fan", "hat", "cap", "saw",
              "red", "big", "pro", "max", "air", "bed", "tub", "jar", "pot", "pan", "lid", "rod", "net", "dog",
              "cat", "pet", "bar", "gun", "key", "oil", "gel", "spray", "one", "two", "for", "kid", "men", "art",
              "bus", "sun", "ice", "eye", "ear", "arm", "leg", "toe", "tie", "zip", "map", "mug", "tea", "bbq"}


def _is_code(t):
    """Token com cara de código de variante/campanha: yi, san, ye, 7, v2, cp... (não palavras curtas reais)."""
    if t in REAL_SHORT or t in ACRONYMS:
        return False
    return bool(re.fullmatch(r"\d{1,3}|v\d+", t)) or len(t) <= 3 or not re.search(r"[aeiou]", t)


def canonical_keys(keys, known):
    """Junta variações do mesmo produto dentro da loja, olhando os outros links que ela usa:
    - prefixo de marca: aless-heated-shawl-wraps -> heated-shawl-wraps (se a versão curta existir)
    - mesmo texto sem hífens: multi-function-x -> multifunction-x
    - sufixo curto: mini-chainsaw-yi, mini-chainsaw-san -> mini-chainsaw;
      feral-cat-house-cp-valley -> feral-cat-house; ...-led -> ...-led-lantern (link truncado)"""
    pool = set(keys) | set(known)
    alias = {}
    flat = {}
    for k in sorted(pool):
        flat.setdefault(k.replace("-", ""), k)
    for k in keys:
        if flat[k.replace("-", "")] != k:
            alias[k] = flat[k.replace("-", "")]
    for k in keys:
        host, _, path = k.partition("/products/")
        if k in alias or not path or "-" not in path:
            continue
        rest = path.split("-", 1)[1]
        if f"{host}/products/{rest}" in pool and len(rest) >= 8:
            alias[k] = f"{host}/products/{rest}"
    # sufixos: um link = outro link da mesma loja + 1-2 termos extras
    by_host = defaultdict(list)
    for k in pool:
        host, sep, path = k.partition("/products/")
        if sep and path:
            by_host[host].append(path.split("-"))
    for host, slugs in by_host.items():
        slugs.sort(key=len)                                   # o mais curto vira a base
        base_of = {}
        for i, short in enumerate(slugs):
            for long in slugs[i + 1:]:
                extra = long[len(short):]
                if long[:len(short)] != short or not 1 <= len(extra) <= 2:
                    continue
                if (len(short) >= 2 and all(_is_code(t) for t in extra)) or len(short) >= 4:
                    base_of.setdefault("-".join(long), "-".join(short))
        # variantes irmãs sem a versão base: x-san e x-yi -> x
        stems = defaultdict(list)
        for sl in slugs:
            if "-".join(sl) in base_of:
                continue
            st = sl[:]
            while len(st) > 3 and len(sl) - len(st) < 2 and _is_code(st[-1]):
                st = st[:-1]
            if st != sl:
                stems["-".join(st)].append("-".join(sl))
        for st, members in stems.items():
            if len(members) >= 2 or any(st == "-".join(x) for x in slugs):
                for m in members:
                    base_of.setdefault(m, st)
        for long, short in base_of.items():
            while short in base_of:                           # encadeia até a base final
                short = base_of[short]
            k = f"{host}/products/{long}"
            alias[k] = f"{host}/products/{short}"
    # resolve aliases encadeados
    for k in list(alias):
        seen = {k}
        while alias[k] in alias and alias[k] not in seen:
            seen.add(alias[k])
            alias[k] = alias[alias[k]]
    return {k: v for k, v in alias.items() if k in keys and k != v}


def label_from_key(key):
    host, _, path = key.partition("/")
    seg = [s for s in path.split("?")[0].split("/") if s]
    if not seg:
        return f"Página inicial ({host})"
    words = seg[-1].replace(".html", "").replace("_", "-").split("-")
    words = [w.upper() if w in ACRONYMS else w for w in words if w]
    name = " ".join(words)
    return name[:1].upper() + name[1:] if name else key


def ad_links(ad):
    s = ad.get("snapshot") or {}
    links = [s.get("linkUrl")] + [c.get("linkUrl") for c in s.get("cards") or []]
    return [x for x in links if x]


def ad_media(ad):
    s = ad.get("snapshot") or {}
    for v in (s.get("videos") or []) + (s.get("cards") or []):
        if v.get("videoHdUrl") or v.get("videoSdUrl"):
            return v.get("videoHdUrl") or v.get("videoSdUrl")
    for i in (s.get("images") or []) + (s.get("cards") or []):
        if i.get("originalImageUrl") or i.get("resizedImageUrl"):
            return i.get("originalImageUrl") or i.get("resizedImageUrl")
    return None


def cover_url(ad):
    """URL da imagem de capa: quadro do vídeo, imagem do anúncio ou do 1º card. Expira em poucos dias."""
    s = ad.get("snapshot") or {}
    for v in s.get("videos") or []:
        if v.get("videoPreviewImageUrl"):
            return v["videoPreviewImageUrl"]
    for i in s.get("images") or []:
        if i.get("resizedImageUrl") or i.get("originalImageUrl"):
            return i.get("resizedImageUrl") or i.get("originalImageUrl")
    for c in s.get("cards") or []:
        for f in ("videoPreviewImageUrl", "resizedImageUrl", "originalImageUrl"):
            if c.get(f):
                return c[f]
    return None


def slim_ad(ad):
    """Guarda só o que o monitor usa. Ficam de fora os links de mídia do Facebook (expiram em dias
    e trazem códigos longos de acesso). O criativo fica preservado pela capa salva e pelo link
    permanente da Biblioteca de Anúncios."""
    s = ad.get("snapshot") or {}
    card_fields = ("title", "body", "linkUrl", "linkDescription", "caption", "ctaText", "ctaType")
    texts = []
    for t in s.get("extraTexts") or []:
        t = t.get("text") if isinstance(t, dict) else t
        if isinstance(t, str) and t.strip():
            texts.append(t)
    snap = {k: s.get(k) for k in ("title", "caption", "ctaText", "ctaType", "displayFormat", "linkUrl",
                                 "linkDescription", "pageName") if s.get(k) is not None}
    snap["body"] = {"text": (s.get("body") or {}).get("text")}
    snap["cards"] = [{k: c.get(k) for k in card_fields if c.get(k) is not None} for c in s.get("cards") or []]
    if texts:
        snap["extraTexts"] = texts
    out = {k: ad.get(k) for k in ("adArchiveID", "pageID", "pageName", "isActive", "startDate", "endDate",
                                 "startDateFormatted", "endDateFormatted", "collationCount", "collationId",
                                 "publisherPlatform", "displayFormat") if ad.get(k) is not None}
    out["snapshot"] = snap
    return out


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "loja"


def download_cover(url, path, max_px=320):
    """Baixa a capa e salva como miniatura JPEG (~10–30 KB). Usa Pillow se instalado."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(8_000_000)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data)).convert("RGB")
        img.thumbnail((max_px, max_px))
        img.save(path, "JPEG", quality=72, optimize=True)
    except ImportError:
        open(path, "wb").write(data)
    return path


def save_covers(con, store, fresh, a):
    """Baixa a capa de cada anúncio desta coleta que ainda não tem capa (na prática: os anúncios novos)."""
    if a.no_covers:
        return 0, 0
    todo = []
    for ad_id, ad in fresh.items():
        row = con.execute("SELECT cover FROM ads WHERE ad_id=? AND store_id=?", (ad_id, store["id"])).fetchone()
        url = cover_url(ad)
        if url and not (row and row["cover"] and os.path.exists(row["cover"])):
            todo.append((ad_id, url, os.path.join(a.covers_dir, slug(store["name"]), f"{ad_id}.jpg")))

    def work(item):
        ad_id, url, path = item
        try:
            return ad_id, download_cover(url, path)
        except Exception:
            return ad_id, None

    ok = 0
    with ThreadPoolExecutor(8) as ex:
        for ad_id, path in ex.map(work, todo):
            if path:
                ok += 1
                con.execute("UPDATE ads SET cover=? WHERE ad_id=? AND store_id=?", (path, ad_id, store["id"]))
    return ok, len(todo) - ok


def slim_stored(con):
    """Enxuga anúncios já guardados (bancos criados antes do formato enxuto)."""
    rows = con.execute("SELECT ad_id, store_id, data_json FROM ads WHERE data_json LIKE '%fbcdn%' "
                       "OR media_url IS NOT NULL").fetchall()
    for r in rows:
        con.execute("UPDATE ads SET data_json=?, media_url=NULL WHERE ad_id=? AND store_id=?",
                    (json.dumps(slim_ad(json.loads(r["data_json"] or "{}")), ensure_ascii=False),
                     r["ad_id"], r["store_id"]))
    return len(rows)


def first_real(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip() and "{{" not in v:
            return v.strip()
    return None


def ts_of(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def days_between(a_ts, b_ts):
    if not a_ts or not b_ts:
        return None
    return max(0, int((b_ts - a_ts) // 86400))


def get_store(con, name):
    row = con.execute("SELECT * FROM stores WHERE lower(name)=lower(?)", (name,)).fetchone()
    if not row:
        sys.exit(f"Loja '{name}' não cadastrada. Use: monitor.py list")
    return row


SORT_API = {"impressions": "total_impressions", "recent": "relevancy_monthly_grouped"}


def fetch_target(kind, value, country, a, max_ads=None):
    """kind: 'page' (ID da página), 'keyword' (termo/domínio) ou 'url' (link da Ad Library).
    Retorna (anúncios normalizados, total informado, capped)."""
    max_ads = a.max_ads if max_ads is None else max_ads
    if get_source(a) == "scrapecreators":
        if kind == "url":
            q = parse_qs(urlparse(value).query)
            c = (q.get("country") or [country])[0]
            if q.get("view_all_page_id"):
                return fetch_target("page", q["view_all_page_id"][0], c, a, max_ads)
            if q.get("q"):
                return fetch_target("keyword", q["q"][0], c, a, max_ads)
            raise RuntimeError(f"URL sem view_all_page_id nem q (não dá para usar na API): {value}")
        print(f"  [api] {kind}: {value} ({country})", file=sys.stderr)
        call = sc().company_ads if kind == "page" else sc().search_ads
        raw, total, capped = call(value, country=country, sort_by=SORT_API.get(a.sort), max_ads=max_ads)
        label = f"scrapecreators:{kind}:{value}:{country}"
    else:
        url = value if kind == "url" else build_url(
            page_id=value if kind == "page" else None, keyword=value if kind == "keyword" else None,
            country=country, status="active", sort=a.sort)
        raw, total = scrape_ads.scrape(url, max_ads, False, a.headful, a.max_idle, a.chromium_path)
        capped = bool(max_ads) and len(raw) >= max_ads
        label = url
    seen, out = set(), []
    for r in raw:
        if r.get("ad_archive_id") and r["ad_archive_id"] not in seen:
            seen.add(r["ad_archive_id"])
            out.append(scrape_ads.normalize(r, label))
    return out, total, capped


# ---------------- resolve / add / list / remove ----------------

def resolve(query, country, a):
    """Busca a marca/site por palavra-chave e ranqueia as páginas que anunciam para o domínio."""
    domain = store_domain(query) if "." in query else None
    pages = {}
    ads, _, _ = fetch_target("keyword", domain or query, country, a, max_ads=a.resolve_ads)
    for ad in ads:
        pid = ad.get("pageID")
        p = pages.setdefault(pid, {"page_id": pid, "page_name": ad.get("pageName"), "ads": 0, "ads_to_domain": 0})
        p["ads"] += 1
        if domain and links_to([product_key(u) or "" for u in ad_links(ad)], domain):
            p["ads_to_domain"] += 1
    return sorted(pages.values(), key=lambda p: (p["ads_to_domain"], p["ads"]), reverse=True), domain


def cmd_resolve(con, a):
    cands, domain = resolve(a.query, a.country, a)
    if not cands:
        print("Nenhuma página encontrada.")
    for c in cands[:15]:
        extra = f" | {c['ads_to_domain']} levam para {domain}" if domain else ""
        print(f"{c['page_id']}  {c['page_name']}  ({c['ads']} anúncios{extra})")


def cmd_add(con, a):
    page_ids, urls, domains = list(a.page_id), list(a.url), [store_domain(d) for d in a.site]
    if not (page_ids or urls or domains):
        sys.exit("Informe --page-id, --url ou --site")
    if domains and a.find_pages:
        for d in domains:
            print(f"Procurando páginas que anunciam {d}...", file=sys.stderr)
            cands, _ = resolve(d, a.country, a)
            hits = [c for c in cands if c["ads_to_domain"] > 0]
            for c in hits:
                print(f"  + {c['page_name']} ({c['page_id']}): {c['ads_to_domain']} anúncios para {d}", file=sys.stderr)
            page_ids += [c["page_id"] for c in hits]
    old = con.execute("SELECT * FROM stores WHERE lower(name)=lower(?)", (a.name,)).fetchone()
    if old:  # mescla
        page_ids = list(dict.fromkeys(json.loads(old["page_ids"]) + page_ids))
        urls = list(dict.fromkeys(json.loads(old["urls"]) + urls))
        domains = list(dict.fromkeys(json.loads(old["domains"]) + domains))
        con.execute("UPDATE stores SET page_ids=?, urls=?, domains=?, country=? WHERE id=?",
                    (json.dumps(page_ids), json.dumps(urls), json.dumps(domains), a.country, old["id"]))
    else:
        con.execute("INSERT INTO stores(name,page_ids,urls,domains,country,created_at) VALUES(?,?,?,?,?,?)",
                    (a.name, json.dumps(page_ids), json.dumps(urls), json.dumps(domains), a.country,
                     datetime.now(timezone.utc).isoformat()))
    con.commit()
    print(f"Loja '{a.name}' salva: páginas={page_ids} urls={len(urls)} sites={domains} país={a.country}")


def cmd_sync(con, a):
    """Cadastra/atualiza as lojas de um arquivo JSON (útil em automações, ex.: GitHub Actions).
    Formato: [{"nome": "Loja X", "site": "m.lojax.com", "pais": "US"}, ...]
    Também aceita "page_ids": [...] e "urls": [...]. Lojas fora do arquivo não são apagadas."""
    lojas = json.load(open(a.file, encoding="utf-8"))
    for l in lojas:
        name = l.get("nome") or l.get("name")
        sites = l.get("site") or l.get("sites") or []
        sites = [sites] if isinstance(sites, str) else sites
        domains = [store_domain(d) for d in sites]
        page_ids = [str(x) for x in l.get("page_ids") or []]
        urls = l.get("urls") or []
        country = (l.get("pais") or l.get("country") or "US").upper()
        old = con.execute("SELECT * FROM stores WHERE lower(name)=lower(?)", (name,)).fetchone()
        if old:
            con.execute("UPDATE stores SET page_ids=?, urls=?, domains=?, country=? WHERE id=?",
                        (json.dumps(page_ids), json.dumps(urls), json.dumps(domains), country, old["id"]))
        else:
            con.execute("INSERT INTO stores(name,page_ids,urls,domains,country,created_at) VALUES(?,?,?,?,?,?)",
                        (name, json.dumps(page_ids), json.dumps(urls), json.dumps(domains), country,
                         datetime.now(timezone.utc).isoformat()))
        print(f"  {name}: sites={domains} páginas={page_ids} país={country}", file=sys.stderr)
    con.commit()
    known = {(l.get("nome") or l.get("name")).lower() for l in lojas}
    extra = [r["name"] for r in con.execute("SELECT name FROM stores") if r["name"].lower() not in known]
    if extra:
        print(f"  Atenção: lojas no banco que não estão no arquivo (continuam sendo coletadas): {extra}. "
              "Para parar, use: monitor.py remove --name ...", file=sys.stderr)


def rekey_store(con, store_id):
    """Recalcula anúncio→produto de todo o histórico com as regras atuais de agrupamento,
    a partir dos links originais guardados em ads.data_json. Seguro rodar sempre."""
    ad_links_of = {}
    for r in con.execute("SELECT ad_id, data_json FROM ads WHERE store_id=?", (store_id,)):
        ad_links_of[r["ad_id"]] = ad_links(json.loads(r["data_json"] or "{}"))
    keys = {k for links in ad_links_of.values() for k in map(product_key, links) if k}
    alias = canonical_keys(keys, [])
    old_first = {r["product_key"]: (r["first_seen"], r["last_seen"], r["url"]) for r in
                 con.execute("SELECT * FROM products WHERE store_id=?", (store_id,))}
    seen = {}
    for r in con.execute("""SELECT ap.ad_id, min(a.first_seen) f, max(a.last_seen) l FROM ad_products ap
                            JOIN ads a ON a.ad_id=ap.ad_id AND a.store_id=ap.store_id
                            WHERE ap.store_id=? GROUP BY ap.ad_id""", (store_id,)):
        seen[r["ad_id"]] = (r["f"], r["l"])
    con.execute("DELETE FROM ad_products WHERE store_id=?", (store_id,))
    con.execute("DELETE FROM products WHERE store_id=?", (store_id,))
    for ad_id, links in ad_links_of.items():
        f, l = seen.get(ad_id) or (None, None)
        for link in links:
            k = product_key(link)
            if not k:
                continue
            k = alias.get(k, k)
            con.execute("INSERT OR IGNORE INTO ad_products VALUES(?,?,?)", (store_id, ad_id, k))
            con.execute("INSERT OR IGNORE INTO products(store_id, product_key, name, url, first_seen, last_seen) "
                        "VALUES(?,?,?,?,?,?)", (store_id, k, None, link, f, l))
            con.execute("""UPDATE products SET first_seen=min(coalesce(first_seen, ?), coalesce(?, first_seen)),
                           last_seen=max(coalesce(last_seen, ?), coalesce(?, last_seen))
                           WHERE store_id=? AND product_key=?""", (f, f, l, l, store_id, k))
    return len(keys), len({alias.get(k, k) for k in keys})


def cmd_rekey(con, a):
    stores = [get_store(con, n) for n in a.store] if a.store else con.execute("SELECT * FROM stores").fetchall()
    for st in stores:
        n_links, n_prod = rekey_store(con, st["id"])
        print(f"  {st['name']}: {n_links} links -> {n_prod} produtos", file=sys.stderr)
    con.commit()


def cmd_list(con, a):
    rows = con.execute("""SELECT s.*, (SELECT max(run_at) FROM runs r WHERE r.store_id=s.id) last_run,
                          (SELECT n_ads FROM runs r WHERE r.store_id=s.id ORDER BY id DESC LIMIT 1) last_n
                          FROM stores s ORDER BY name""").fetchall()
    if not rows:
        print("Nenhuma loja cadastrada.")
    for r in rows:
        print(f"- {r['name']} | páginas {json.loads(r['page_ids'])} | sites {json.loads(r['domains'])} | "
              f"urls {len(json.loads(r['urls']))} | {r['country']} | última coleta: {r['last_run'] or '—'} "
              f"({r['last_n'] if r['last_n'] is not None else '—'} anúncios)")


def cmd_remove(con, a):
    s = get_store(con, a.name)
    for t in ("ads", "ad_products", "products"):
        con.execute(f"DELETE FROM {t} WHERE store_id=?", (s["id"],))
    con.execute("DELETE FROM sightings WHERE run_id IN (SELECT id FROM runs WHERE store_id=?)", (s["id"],))
    con.execute("DELETE FROM runs WHERE store_id=?", (s["id"],))
    con.execute("DELETE FROM stores WHERE id=?", (s["id"],))
    con.commit()
    print(f"Loja '{a.name}' removida.")


# ---------------- run ----------------

def collect(store, a):
    page_ids, urls, domains = (json.loads(store[k]) for k in ("page_ids", "urls", "domains"))
    # Domínio: busca o domínio na Ad Library e fica só com anúncios que levam para ele
    # (pega todas as páginas que anunciam a loja). Páginas/URLs: todos os anúncios delas.
    targets = [("keyword", d, d) for d in domains]
    targets += [("page", p, None) for p in page_ids] + [("url", u, None) for u in urls]
    ads, total, ok, capped = {}, 0, True, False
    for kind, value, dom in targets:
        try:
            found, t, cap = fetch_target(kind, value, store["country"], a)
        except Exception as e:  # um alvo com erro não derruba a loja inteira
            print(f"  ERRO em {value}: {e}", file=sys.stderr)
            ok = False
            continue
        total += t or 0
        capped = capped or cap
        for ad in found:
            keys = list(dict.fromkeys(k for k in map(product_key, ad_links(ad)) if k))
            if dom and not links_to(keys, dom):
                continue
            if ad.get("isActive") is False:
                continue
            ads[ad["adArchiveID"]] = (ad, keys)
    return ads, total, ok, capped


def cmd_run(con, a):
    stores = [get_store(con, n) for n in a.store] if a.store else con.execute("SELECT * FROM stores").fetchall()
    if not stores:
        sys.exit("Nenhuma loja cadastrada. Use: monitor.py add ...")
    now = a.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for st in stores:
        print(f"== {st['name']} ==", file=sys.stderr)
        ads, total, ok, capped = collect(st, a)
        prev = con.execute("SELECT * FROM runs WHERE store_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                           (st["id"],)).fetchone()
        suspicious = not ads and prev and prev["n_ads"] > 0
        status = "ok" if ok and not suspicious else ("empty" if suspicious else "partial")
        cur = con.execute("INSERT INTO runs(store_id, run_at, n_ads, total_reported, status, capped) "
                          "VALUES(?,?,?,?,?,?)", (st["id"], now, len(ads), total, status, int(capped)))
        if capped:
            print(f"  limite de {a.max_ads} anúncios por busca atingido (a Ad Library indica ~{total}). "
                  "Anúncios fora do limite não serão marcados como pausados.", file=sys.stderr)
        run_id = cur.lastrowid
        if suspicious:
            print("  AVISO: 0 anúncios, mas a coleta anterior tinha anúncios. Provável bloqueio; "
                  "coleta marcada como 'empty' e ignorada nos relatórios.", file=sys.stderr)
            con.commit()
            continue

        seen_products = set()
        known = [r["product_key"] for r in
                 con.execute("SELECT product_key FROM products WHERE store_id=?", (st["id"],))]
        alias = canonical_keys({k for _, ks in ads.values() for k in ks}, known)
        for ad_id, (ad, keys) in ads.items():
            s = ad.get("snapshot") or {}
            cards = s.get("cards") or [{}]
            row = (ad.get("pageID"), ad.get("pageName"), ad.get("startDate"), ad.get("endDate"),
                   s.get("displayFormat"),
                   first_real(s.get("title"), cards[0].get("title")),
                   first_real((s.get("body") or {}).get("text"), cards[0].get("body")),
                   s.get("ctaText") or cards[0].get("ctaText"),
                   next(iter(ad_links(ad)), None), ",".join(ad.get("publisherPlatform") or []),
                   None, ad.get("collationCount"), json.dumps(slim_ad(ad), ensure_ascii=False))
            exists = con.execute("SELECT 1 FROM ads WHERE ad_id=? AND store_id=?", (ad_id, st["id"])).fetchone()
            if exists:
                con.execute("""UPDATE ads SET page_id=?, page_name=?, start_date=?, end_date=?, format=?, title=?,
                               body=?, cta=?, link_url=?, platforms=?, media_url=?, collation_count=?, data_json=?,
                               last_seen=?, is_active=1 WHERE ad_id=? AND store_id=?""",
                            row + (now, ad_id, st["id"]))
            else:
                con.execute("""INSERT INTO ads(page_id, page_name, start_date, end_date, format, title, body, cta,
                               link_url, platforms, media_url, collation_count, data_json, ad_id, store_id,
                               first_seen, last_seen, is_active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                            row + (ad_id, st["id"], now, now))
            con.execute("INSERT OR REPLACE INTO sightings VALUES(?,?,?)", (run_id, ad_id, ad.get("collationCount")))
            for link in ad_links(ad):
                k = product_key(link)
                if not k:
                    continue
                k = alias.get(k, k)
                con.execute("INSERT OR IGNORE INTO ad_products VALUES(?,?,?)", (st["id"], ad_id, k))
                con.execute("INSERT OR IGNORE INTO products(store_id, product_key, name, url, first_seen, last_seen) "
                            "VALUES(?,?,?,?,?,?)", (st["id"], k, None, link, now, now))
                con.execute("UPDATE products SET last_seen=? WHERE store_id=? AND product_key=?", (now, st["id"], k))
                seen_products.add(k)
        # anúncios que estavam ativos e sumiram nesta coleta = pausados
        if status == "ok" and ads and not capped:
            con.execute(f"""UPDATE ads SET is_active=0 WHERE store_id=? AND is_active=1
                            AND ad_id NOT IN ({','.join('?' * len(ads))})""", (st["id"], *ads.keys()))
        got, failed = save_covers(con, st, {i: ad for i, (ad, _) in ads.items()}, a)
        if got or failed:
            print(f"  capas salvas: {got}" + (f" (falharam: {failed})" if failed else ""), file=sys.stderr)
        con.commit()
        print(f"  {len(ads)} anúncios ativos, {len(seen_products)} produtos/páginas de destino "
              f"(total informado pela Ad Library: {total}) [{status}]", file=sys.stderr)
    n = slim_stored(con)
    if n:
        print(f"Formato enxuto aplicado a {n} anúncios já guardados.", file=sys.stderr)
    con.commit()
    if _SC is not None:
        print(f"Créditos ScrapeCreators usados nesta coleta: {_SC.credits_used} · restantes: "
              f"{_SC.credits_remaining}", file=sys.stderr)


# ---------------- report ----------------

def product_counts(con, store_id, run_id, rep=lambda k: k):
    """Anúncios distintos por produto numa coleta; rep() junta chaves do mesmo produto."""
    groups = defaultdict(set)
    for r in con.execute("""SELECT ap.product_key, s.ad_id FROM sightings s
                            JOIN ad_products ap ON ap.ad_id=s.ad_id AND ap.store_id=?
                            WHERE s.run_id=?""", (store_id, run_id)):
        groups[rep(r["product_key"])].add(r["ad_id"])
    return {k: len(v) for k, v in groups.items()}


def report_store(con, st, a, csv_rows):
    runs = con.execute("SELECT * FROM runs WHERE store_id=? AND status IN ('ok','partial') ORDER BY id",
                       (st["id"],)).fetchall()
    out = [f"## {st['name']}", ""]
    if not runs:
        return out + ["Ainda sem coletas. Rode `monitor.py run`.", ""]
    cur, prev = runs[-1], (runs[-2] if len(runs) > 1 else None)
    now_ts = ts_of(cur["run_at"])
    cur_ads = {r["ad_id"] for r in con.execute("SELECT ad_id FROM sightings WHERE run_id=?", (cur["id"],))}
    prev_ads = {r["ad_id"] for r in con.execute("SELECT ad_id FROM sightings WHERE run_id=?", (prev["id"],))} \
        if prev else set()
    new_ads, stopped = cur_ads - prev_ads if prev else set(), prev_ads - cur_ads
    names = {r["product_key"]: r["name"] for r in
             con.execute("SELECT product_key, name FROM products WHERE store_id=?", (st["id"],))}
    # links diferentes com o mesmo nome oficial = mesmo produto (variantes, landing pages)
    by_name, reps = {}, {}
    merged = canonical_keys(set(names), [])                   # variações de link vistas em qualquer dia
    for k in sorted(names):
        if k in merged:
            continue
        norm = re.sub(r"[®™©]|\s+", " ", (names[k] or label_from_key(k))).strip().lower()
        reps[k] = by_name.setdefault(re.sub(r"\s+", " ", norm), k)
    rep = lambda k: reps.get(merged.get(k, k), merged.get(k, k))  # noqa: E731
    pc, pp = product_counts(con, st["id"], cur["id"], rep), \
        (product_counts(con, st["id"], prev["id"], rep) if prev else {})
    ads_by_id = {r["ad_id"]: r for r in con.execute("SELECT * FROM ads WHERE store_id=?", (st["id"],))}
    ad_keys = defaultdict(list)
    for r in con.execute("SELECT ad_id, product_key FROM ad_products WHERE store_id=?", (st["id"],)):
        if rep(r["product_key"]) not in ad_keys[r["ad_id"]]:
            ad_keys[r["ad_id"]].append(rep(r["product_key"]))
    first_run_of = {}
    for r in con.execute("""SELECT ap.product_key, min(ru.id) f FROM sightings s JOIN runs ru ON ru.id=s.run_id
                            JOIN ad_products ap ON ap.ad_id=s.ad_id AND ap.store_id=ru.store_id
                            WHERE ru.store_id=? AND ru.status IN ('ok','partial') GROUP BY ap.product_key""",
                         (st["id"],)):
        k = rep(r["product_key"])
        first_run_of[k] = min(first_run_of.get(k, r["f"]), r["f"])
    run_date = {r["id"]: r["run_at"] for r in runs}
    base_dir = os.path.dirname(os.path.abspath(a.out)) if getattr(a, "out", None) else os.getcwd()

    def thumb(r, width=60):
        cov = r["cover"]
        if not cov or not os.path.exists(cov):
            return ""
        rel = os.path.relpath(os.path.abspath(cov), base_dir).replace(os.sep, "/")
        return f'<img src="{rel}" width="{width}">'
    name = lambda k: names.get(k) or label_from_key(k)  # noqa: E731
    delta = lambda n, o: "" if prev is None else (f" ({n - o:+d})" if n != o else " (=)")  # noqa: E731

    out += [f"Coleta: **{cur['run_at'][:10]}**" + (f" · comparada com {prev['run_at'][:10]}" if prev else
                                                    " · primeira coleta (sem comparação ainda)"), ""]
    out += [f"- **Anúncios ativos:** {len(cur_ads)}{delta(len(cur_ads), len(prev_ads))}",
            f"- **Produtos/páginas anunciados:** {len(pc)}{delta(len(pc), len(pp))}"]
    if prev:
        out += [f"- **Anúncios novos:** {len(new_ads)} · **pausados:** {len(stopped)}"]
    out.append("")

    # sinais
    signals = []
    for k, n in pc.items():
        o = pp.get(k, 0)
        if prev and first_run_of.get(k) == cur["id"]:
            signals.append(f"🆕 Produto novo nos anúncios: **{name(k)}** ({n} anúncio(s))")
        elif prev and (n - o >= 2 or (o and n > o and n >= 1.5 * o)):
            signals.append(f"📈 Escalando: **{name(k)}** {o} → {n} anúncios")
        elif prev and o - n >= 2:
            signals.append(f"📉 Reduzindo: **{name(k)}** {o} → {n} anúncios")
    for k in pp:
        if k not in pc:
            signals.append(f"❌ Saiu dos anúncios: **{name(k)}** (tinha {pp[k]})")
    if signals:
        out += ["### Mudanças", ""] + [f"- {s}" for s in signals] + [""]

    # tabela de produtos
    out += ["### Produtos anunciados", "",
            "| Criativo | Produto | Anúncios ativos | Anúncio mais antigo (dias) | Monitorando desde |",
            "|---|---|---:|---:|---|"]
    for k, n in sorted(pc.items(), key=lambda x: -x[1]):
        mine = [ads_by_id[i] for i in cur_ads if k in ad_keys[i]]
        starts = [r["start_date"] for r in mine if r["start_date"]]
        oldest = days_between(min(starts), now_ts) if starts else None
        # capa do anúncio mais antigo do produto que tenha imagem
        with_cover = sorted((r for r in mine if thumb(r)), key=lambda r: r["start_date"] or now_ts)
        img = thumb(with_cover[0], 60) if with_cover else ""
        out.append(f"| {img} | {name(k)} <br><sub>{k}</sub> | {n}{delta(n, pp.get(k, 0))} | "
                   f"{oldest if oldest is not None else '—'} | {run_date.get(first_run_of.get(k), '')[:10]} |")
        csv_rows["produtos"].append({"loja": st["name"], "data": cur["run_at"][:10], "produto": name(k),
                                     "chave": k, "anuncios_ativos": n,
                                     "variacao": (n - pp.get(k, 0)) if prev else "",
                                     "dias_anuncio_mais_antigo": oldest})
    no_link = [i for i in cur_ads if not ad_keys[i]]
    if no_link:
        out.append(f"| | _(sem link de destino: WhatsApp, formulário, perfil…)_ | {len(no_link)} | | |")
    out.append("")

    def ad_line(r, end_ts):
        txt = (r["title"] or r["body"] or "").replace("\n", " ")[:80]
        d = days_between(r["start_date"], end_ts)
        prods = ", ".join(name(k) for k in ad_keys[r["ad_id"]][:2]) or "—"
        img = thumb(r, 70)
        return (f"- {img + ' ' if img else ''}**{d if d is not None else '?'} dias** · {r['format'] or ''} · "
                f"{prods} · {txt} · [ver anúncio](https://www.facebook.com/ads/library/?id={r['ad_id']})")

    out += ["### Anúncios há mais tempo no ar (prováveis vencedores)", ""]
    for r in sorted((ads_by_id[i] for i in cur_ads), key=lambda r: r["start_date"] or now_ts)[:a.top]:
        out.append(ad_line(r, now_ts))
    out.append("")
    if prev and new_ads:
        out += [f"### Novos desde {prev['run_at'][:10]} ({len(new_ads)})", ""]
        out += [ad_line(ads_by_id[i], now_ts) for i in list(new_ads)[:a.top]] + [""]
    if prev and stopped:
        if cur["capped"] or prev["capped"]:
            out += [f"### Saíram da amostra desde {prev['run_at'][:10]} ({len(stopped)})", "",
                    f"_A coleta foi limitada aos {cur['n_ads']} anúncios de maior impressão: estes podem ter sido "
                    "pausados ou apenas caído no ranking._", ""]
        else:
            out += [f"### Pausados desde {prev['run_at'][:10]} ({len(stopped)})", ""]
        out += [ad_line(ads_by_id[i], ts_of(ads_by_id[i]["last_seen"])) for i in list(stopped)[:a.top]] + [""]

    # evolução
    hist = runs[-a.history:]
    if len(hist) > 1:
        cols = [r["run_at"][5:10] for r in hist]
        counts = [product_counts(con, st["id"], r["id"], rep) for r in hist]
        keys = sorted(set().union(*counts), key=lambda k: -counts[-1].get(k, 0))[:15]
        out += ["### Evolução (anúncios ativos por produto)", "",
                "| Produto | " + " | ".join(cols) + " |", "|---|" + "---:|" * len(cols),
                "| **Total de anúncios** | " + " | ".join(str(r["n_ads"]) for r in hist) + " |"]
        for k in keys:
            out.append(f"| {name(k)} | " + " | ".join(str(c.get(k, 0)) for c in counts) + " |")
        out.append("")
    for r in runs:
        for k, n in product_counts(con, st["id"], r["id"], rep).items():
            csv_rows["historico"].append({"loja": st["name"], "data": r["run_at"][:10], "produto": name(k),
                                          "chave": k, "anuncios_ativos": n})
    for i in cur_ads:
        r = ads_by_id[i]
        csv_rows["anuncios"].append({
            "loja": st["name"], "ad_id": i, "link_ad_library": f"https://www.facebook.com/ads/library/?id={i}",
            "pagina": r["page_name"], "produtos": " | ".join(name(k) for k in ad_keys[i]),
            "inicio": datetime.fromtimestamp(r["start_date"], timezone.utc).strftime("%Y-%m-%d") if r["start_date"] else "",
            "dias_no_ar": days_between(r["start_date"], now_ts), "visto_primeiro_em": r["first_seen"][:10],
            "versoes": r["collation_count"], "formato": r["format"], "plataformas": r["platforms"],
            "titulo": r["title"], "texto": r["body"], "cta": r["cta"], "link": r["link_url"],
            "capa": r["cover"] or ""})
    return out


def cmd_report(con, a):
    import csv
    stores = [get_store(con, n) for n in a.store] if a.store else con.execute("SELECT * FROM stores ORDER BY name").fetchall()
    csv_rows = {"produtos": [], "historico": [], "anuncios": []}
    lines = [f"# Monitoramento de anúncios — {datetime.now().strftime('%d/%m/%Y')}", ""]
    for st in stores:
        lines += report_store(con, st, a, csv_rows)
    md = "\n".join(lines)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(md)
        print(f"Relatório: {a.out}", file=sys.stderr)
    else:
        print(md)
    if a.csv_dir:
        os.makedirs(a.csv_dir, exist_ok=True)
        for name, rows in csv_rows.items():
            if rows:
                path = os.path.join(a.csv_dir, f"{name}.csv")
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
                print(f"CSV: {path}", file=sys.stderr)


# ---------------- CLI ----------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=os.environ.get("ADS_MONITOR_DIR", "ads-monitor"))
    sub = p.add_subparsers(dest="cmd", required=True)

    def scraping_opts(sp):
        sp.add_argument("--source", choices=["scrapecreators", "browser"],
                        help="fonte dos dados (padrão: scrapecreators se SCRAPECREATORS_API_KEY existir, "
                             "senão browser; ou $ADS_SOURCE)")
        sp.add_argument("--max-ads", type=int, default=300,
                        help="máx. de anúncios por busca (0 = todos). Na API: 30 anúncios = 1 crédito")
        sp.add_argument("--sort", choices=["impressions", "recent"], default="impressions",
                        help="com limite, mantém os de maior impressão (padrão) ou os mais recentes")
        sp.add_argument("--resolve-ads", type=int, default=60, help="anúncios lidos ao procurar páginas")
        sp.add_argument("--max-idle", type=int, default=4)
        sp.add_argument("--headful", action="store_true")
        sp.add_argument("--chromium-path", default=os.environ.get("CHROMIUM_PATH"))

    sp = sub.add_parser("add")
    sp.add_argument("--name", required=True)
    sp.add_argument("--page-id", action="append", default=[])
    sp.add_argument("--url", action="append", default=[], help="URL da Ad Library (com filtros)")
    sp.add_argument("--site", action="append", default=[], help="domínio da loja, ex.: lojax.com.br")
    sp.add_argument("--country", default="BR")
    sp.add_argument("--find-pages", action="store_true",
                    help="além do domínio, cadastrar as páginas que anunciam o site (traz também anúncios "
                         "dessas páginas que levam para outros sites)")
    scraping_opts(sp)

    sp = sub.add_parser("resolve")
    sp.add_argument("query", help="domínio ou nome da marca")
    sp.add_argument("--country", default="BR")
    scraping_opts(sp)

    sub.add_parser("list")
    sp = sub.add_parser("rekey", help="reagrupa produtos de todo o histórico com as regras atuais")
    sp.add_argument("--store", action="append", default=[])
    sp = sub.add_parser("sync")
    sp.add_argument("--file", default="lojas.json")
    sp = sub.add_parser("remove")
    sp.add_argument("--name", required=True)

    sp = sub.add_parser("run")
    sp.add_argument("--store", action="append", default=[], help="só estas lojas (padrão: todas)")
    sp.add_argument("--as-of", help=argparse.SUPPRESS)  # data simulada, para testes
    sp.add_argument("--covers-dir", default=os.environ.get("ADS_COVERS_DIR", "criativos"),
                    help="pasta das imagens de capa (padrão: criativos)")
    sp.add_argument("--no-covers", action="store_true", help="não baixar imagens de capa")
    scraping_opts(sp)

    sp = sub.add_parser("report")
    sp.add_argument("--store", action="append", default=[])
    sp.add_argument("--out", help="arquivo .md (padrão: imprime na tela)")
    sp.add_argument("--csv-dir", help="pasta para produtos.csv, historico.csv e anuncios.csv")
    sp.add_argument("--top", type=int, default=10)
    sp.add_argument("--history", type=int, default=7, help="nº de coletas na tabela de evolução")

    a = p.parse_args()
    con = db_open(a.data_dir)
    {"add": cmd_add, "resolve": cmd_resolve, "list": cmd_list, "sync": cmd_sync, "rekey": cmd_rekey, "remove": cmd_remove,
     "run": cmd_run, "report": cmd_report}[a.cmd](con, a)


if __name__ == "__main__":
    main()

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
CREATE TABLE IF NOT EXISTS store_totals (
  host TEXT, date TEXT, country TEXT, total INTEGER, PRIMARY KEY (host, date));
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
    try:  # posição do anúncio no ranking de impressões da Biblioteca (1 = mais impressões)
        con.execute("ALTER TABLE sightings ADD COLUMN rank INTEGER")
    except sqlite3.OperationalError:
        pass
    for col in ("keywords TEXT DEFAULT '[]'", "exact INTEGER DEFAULT 0"):  # buscas por palavra-chave
        try:
            con.execute(f"ALTER TABLE stores ADD COLUMN {col}")
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


LEAD_WORDS = {"news", "adv", "advertorial", "article", "listicle", "blog", "lp", "landing", "discover", "new", "hot", "sale",
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
        if tokens[-2] == "in":                                       # "2-in-1" faz parte do nome
            break
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


def product_links(ad):
    """Links que definem DE QUAL produto é o anúncio.
    Carrossel/catálogo (DPA/DCO) com cards levando a produtos diferentes: o anúncio é atribuído ao
    produto do 1º card (o que abre o anúncio e costuma ser o título). Sem isso, um catálogo com 6
    produtos contaria 1 anúncio para cada um deles e inflaria as contagens."""
    s = ad.get("snapshot") or {}
    cards = [c.get("linkUrl") for c in s.get("cards") or [] if c.get("linkUrl")]
    if len({product_key(c) for c in cards if product_key(c)}) >= 2:
        return cards[:1]
    links = [s.get("linkUrl")] + cards
    links = [x for x in links if x]
    # link principal para a página inicial + cards de produto: vale o produto
    prod = [x for x in links if product_key(x) and "/products/" in product_key(x)]
    return prod or links


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


def fetch_target(kind, value, country, a, max_ads=None, exact=False):
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
        if kind == "page":
            raw, total, capped = sc().company_ads(value, country=country, sort_by=SORT_API.get(a.sort), max_ads=max_ads)
        else:
            raw, total, capped = sc().search_ads(value, country=country, sort_by=SORT_API.get(a.sort),
                                                 exact=exact, max_ads=max_ads)
        label = f"scrapecreators:{kind}:{value}:{country}"
    else:
        url = value if kind == "url" else build_url(
            page_id=value if kind == "page" else None, keyword=value if kind == "keyword" else None,
            country=country, status="active", sort=a.sort, exact=exact)
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
        kws = l.get("palavra_chave") or l.get("palavras_chave") or l.get("keyword") or l.get("keywords") or []
        kws = [kws] if isinstance(kws, str) else kws
        exact = int(bool(l.get("frase_exata") or l.get("exact")))
        country = (l.get("pais") or l.get("country") or "US").upper()
        old = con.execute("SELECT * FROM stores WHERE lower(name)=lower(?)", (name,)).fetchone()
        if old:
            con.execute("UPDATE stores SET page_ids=?, urls=?, domains=?, country=?, keywords=?, exact=? WHERE id=?",
                        (json.dumps(page_ids), json.dumps(urls), json.dumps(domains), country,
                         json.dumps(kws), exact, old["id"]))
        else:
            con.execute("INSERT INTO stores(name,page_ids,urls,domains,country,created_at,keywords,exact) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (name, json.dumps(page_ids), json.dumps(urls), json.dumps(domains), country,
                         datetime.now(timezone.utc).isoformat(), json.dumps(kws), exact))
        what = f"palavras-chave={kws}{' (frase exata)' if exact else ''}" if kws else f"sites={domains} páginas={page_ids}"
        print(f"  {name}: {what} país={country}", file=sys.stderr)
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
        ad_links_of[r["ad_id"]] = product_links(json.loads(r["data_json"] or "{}"))
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
        kws = json.loads(r["keywords"] or "[]")
        print(f"- {r['name']} | " + (f"palavras-chave {kws} | " if kws else "") +
              f"páginas {json.loads(r['page_ids'])} | sites {json.loads(r['domains'])} | "
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
    keywords = json.loads(store["keywords"] or "[]")
    exact = bool(store["exact"])
    targets = [("keyword", d, d, False) for d in domains]
    targets += [("keyword", k, None, exact) for k in keywords]          # palavra-chave: qualquer anunciante
    targets += [("page", p, None, False) for p in page_ids] + [("url", u, None, False) for u in urls]
    ads, total, ok, capped = {}, 0, True, False
    for kind, value, dom, ex in targets:
        try:
            found, t, cap = fetch_target(kind, value, store["country"], a, exact=ex)
        except Exception as e:  # um alvo com erro não derruba a loja inteira
            print(f"  ERRO em {value}: {e}", file=sys.stderr)
            ok = False
            continue
        total += t or 0
        capped = capped or cap
        for ad in found:
            every = [k for k in map(product_key, ad_links(ad)) if k]        # filtro: qualquer link do anúncio
            if dom and not links_to(every, dom):
                continue
            keys = list(dict.fromkeys(k for k in map(product_key, product_links(ad)) if k))
            if dom:   # loja monitorada: o produto tem de ser dela
                keys = [k for k in keys if links_to([k], dom)] or [k for k in every if links_to([k], dom)][:1]
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
        for pos, (ad_id, (ad, keys)) in enumerate(ads.items(), 1):  # ordem da API = ranking de impressões
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
            con.execute("INSERT OR REPLACE INTO sightings(run_id, ad_id, collation_count, rank) VALUES(?,?,?,?)",
                        (run_id, ad_id, ad.get("collationCount"), pos))
            for link in product_links(ad):
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

def product_sets(con, store_id, run_id, rep=lambda k: k):
    """IDs dos anúncios de cada produto numa coleta; rep() junta chaves do mesmo produto."""
    groups = defaultdict(set)
    for r in con.execute("""SELECT ap.product_key, s.ad_id FROM sightings s
                            JOIN ad_products ap ON ap.ad_id=s.ad_id AND ap.store_id=?
                            WHERE s.run_id=?""", (store_id, run_id)):
        groups[rep(r["product_key"])].add(r["ad_id"])
    return dict(groups)


def product_counts(con, store_id, run_id, rep=lambda k: k):
    """Anúncios distintos por produto numa coleta."""
    return {k: len(v) for k, v in product_sets(con, store_id, run_id, rep).items()}


def scale_signal(series, window=3):
    """Possível início de escala: o produto ganhou anúncios rápido nas últimas coletas.
    series = contagens por coleta (a última é a atual). Regra: nas últimas `window` coletas
    ganhou pelo menos 2 anúncios e +50% (ou surgiu do zero com 3+), tem 3+ anúncios agora
    e não caiu na última coleta. Retorna (ganho, base) ou None."""
    if len(series) < 2:
        return None
    cur, last = series[-1], series[-2]
    base = series[max(0, len(series) - 1 - window)]
    gain = cur - base
    if cur < 3 or cur < last or gain < 2:
        return None
    if base and cur < 1.5 * base:
        return None
    return gain, base


def store_state(con, st):
    """Retrato da loja: coletas, produtos (agrupados), anúncios e histórico por produto."""
    runs = con.execute("SELECT * FROM runs WHERE store_id=? AND status IN ('ok','partial') ORDER BY id",
                       (st["id"],)).fetchall()
    if not runs:
        return None
    # comparações são sempre entre dias: vale a última coleta de cada dia
    daily = list({r["run_at"][:10]: r for r in runs}.values())
    cur, prev = daily[-1], (daily[-2] if len(daily) > 1 else None)
    names = {r["product_key"]: r["name"] for r in
             con.execute("SELECT product_key, name FROM products WHERE store_id=?", (st["id"],))}
    by_name, reps = {}, {}
    merged = canonical_keys(set(names), [])                   # variações de link vistas em qualquer dia
    for k in sorted(names):
        if k in merged:
            continue
        norm = re.sub(r"[®™©]|\s+", " ", (names[k] or label_from_key(k))).strip().lower()
        # mesmo nome só junta dentro do MESMO site: numa busca por palavra-chave, lojas diferentes
        # com o mesmo produto ficam separadas aqui (e são agrupadas depois como "N lojas")
        reps[k] = by_name.setdefault((k.split("/")[0], re.sub(r"\s+", " ", norm)), k)
    rep0 = lambda k: reps.get(merged.get(k, k), merged.get(k, k))  # noqa: E731
    # palavras quebradas de propósito no mesmo site ("ph one hol der" = "phone holder")
    fz = {}

    def fz_find(k):
        while fz.get(k, k) != k:
            k = fz[k]
        return k
    by_host = defaultdict(list)
    for k in sorted({rep0(k) for k in names}):
        c = name_core(names.get(k) or label_from_key(k), k)
        if c:
            by_host[k.split("/")[0]].append((k, c))
    for items in by_host.values():
        for i, (ka, ca) in enumerate(items):
            for kb, cb in items[i + 1:]:
                if fz_find(ka) != fz_find(kb) and core_similar(ca, cb):
                    fz[fz_find(kb)] = fz_find(ka)
    rep = lambda k: fz_find(rep0(k))  # noqa: E731
    sets = {r["id"]: product_sets(con, st["id"], r["id"], rep) for r in runs}
    counts = {rid: {k: len(v) for k, v in g.items()} for rid, g in sets.items()}
    ads_by_id = {r["ad_id"]: r for r in con.execute("SELECT * FROM ads WHERE store_id=?", (st["id"],))}
    ad_keys = defaultdict(list)
    for r in con.execute("SELECT ad_id, product_key FROM ad_products WHERE store_id=?", (st["id"],)):
        if rep(r["product_key"]) not in ad_keys[r["ad_id"]]:
            ad_keys[r["ad_id"]].append(rep(r["product_key"]))
    first_run_of = {}
    for rid in sorted(counts):
        for k in counts[rid]:
            first_run_of.setdefault(k, rid)
    sight = {r["ad_id"]: r for r in con.execute("SELECT * FROM sightings WHERE run_id=?", (cur["id"],))}
    prev_ads = {r["ad_id"] for r in con.execute("SELECT ad_id FROM sightings WHERE run_id=?", (prev["id"],))} \
        if prev else set()
    return {"runs": runs, "daily": daily, "sets": sets, "cur": cur, "prev": prev, "counts": counts, "ads": ads_by_id, "ad_keys": ad_keys,
            "first_run_of": first_run_of, "sight": sight, "prev_ads": prev_ads,
            "name": lambda k: names.get(k) or label_from_key(k), "now_ts": ts_of(cur["run_at"]),
            "run_date": {r["id"]: r["run_at"] for r in runs}}


SHORT_OK = {"in", "of", "for", "and", "the", "with", "to", "on", "car", "pro", "set", "kit", "box", "bag", "pen", "cup",
            "mat", "pad", "toy", "fan", "hat", "cap", "saw", "max", "air", "bed", "pet", "dog", "cat", "oil", "gel",
            "men", "kid", "red", "big", "mini", "rv", "atv", "utv", "x"} | ACRONYMS
GENERIC_PAGES = {"article", "listicle", "index", "home", "page", "lp", "landing", "offer", "sale", "shop", "products"}


def descriptive_slug(key):
    """O link descreve o produto? (não é código, página genérica ou uma palavra só)"""
    slug = key.split("/products/")[-1] if "/products/" in key else key.split("/", 1)[-1]
    tokens = [t for t in re.split(r"[-_/.]+", slug.lower()) if t]
    if any(len(t) >= 10 and re.search(r"\d", t) for t in tokens):          # cmq7inspk0004s561...
        return False
    words = [t for t in tokens if re.fullmatch(r"[a-z]{3,}", t) and t not in GENERIC_PAGES]
    return len(words) >= 2


def name_from_ads(ads):
    """Nome a partir do título mais comum dos anúncios (para links que não descrevem o produto)."""
    titles = [re.sub(r"\s+", " ", re.sub(r"[^\w\s&'’.,!?%$+-]", " ", (x.get("title") or ""))).strip(" .,-")
              for x in ads]
    titles = [t for t in titles if len(t) >= 6]
    if not titles:
        return None
    best = max(set(titles), key=lambda t: (titles.count(t), -len(t)))
    return best if len(best) <= 70 else best[:67].rsplit(" ", 1)[0] + "…"


def broken_words(name):
    """Quantos pedaços de palavra estranhos o nome tem ('circu it re lay' -> 3)."""
    return sum(1 for t in re.findall(r"[a-z]+", (name or "").lower()) if len(t) <= 3 and t not in SHORT_OK)


# sinais de escala além do crescimento (dados já coletados, sem custo)
DUP_MIN = 10          # 🔁 duplicações somadas dos anúncios do produto
PAGES_MIN = 3         # 📄 páginas diferentes anunciando o produto
OLD_DAYS, OLD_ADS_MIN = 30, 3   # ⏳ anúncios com 30+ dias ainda no top de impressões


def add_stats(row):
    """Calcula duplicações, páginas e anúncios antigos no top a partir dos anúncios do produto."""
    ads = row["ads"]
    pages = sorted({a["page"] for a in ads if a.get("page")})
    row["dup_total"] = sum(a["dup"] or 1 for a in ads)
    row["pages"] = pages
    row["old_ads"] = sum(1 for a in ads if (a["days"] or 0) >= OLD_DAYS)
    row["flags"] = {"dup": row["dup_total"] >= DUP_MIN, "pages": len(pages) >= PAGES_MIN,
                    "old": row["old_ads"] >= OLD_ADS_MIN}
    return row


def products_of(S):
    """Linhas da planilha 'Produtos anunciados' para uma loja."""
    cur, prev, now_ts = S["cur"], S["prev"], S["now_ts"]
    pc = S["counts"][cur["id"]]
    pp = S["counts"][prev["id"]] if prev else {}
    rows = []
    for k, n in pc.items():
        mine = [S["ads"][i] for i in S["sight"] if k in S["ad_keys"][i]]
        starts = [r["start_date"] for r in mine if r["start_date"]]
        daily = S["daily"]
        series = [S["counts"][r["id"]].get(k, 0) for r in daily]
        sig = scale_signal(series)
        ads = []
        for r in mine:
            sg = S["sight"][r["ad_id"]]
            ads.append({"id": r["ad_id"], "rank": sg["rank"], "dup": r["collation_count"] or 1,
                        "days": days_between(r["start_date"], now_ts), "page": r["page_name"],
                        "page_id": r["page_id"],
                        "title": (r["title"] or r["body"] or "")[:140], "format": r["format"],
                        "cover": r["cover"] if r["cover"] and os.path.exists(r["cover"]) else None,
                        "new": r["ad_id"] not in S["prev_ads"] if prev else False})
        ads.sort(key=lambda x: (x["rank"] if x["rank"] is not None else 10**6, -x["dup"]))
        with_cover = sorted((r for r in mine if r["cover"] and os.path.exists(r["cover"])),
                            key=lambda r: r["start_date"] or now_ts)
        first = S["first_run_of"].get(k)
        name = S["name"](k)
        if not descriptive_slug(k):
            name = name_from_ads(ads) or name
        rows.append({
            "key": k, "name": name, "active": n, "named_by_ad": not descriptive_slug(k),
            "delta": (n - pp.get(k, 0)) if prev else None,
            "oldest": days_between(min(starts), now_ts) if starts else None,
            "since": S["run_date"].get(first, "")[:10],
            "is_new": bool(prev) and first == cur["id"],
            "cover": with_cover[0]["cover"] if with_cover else None,
            "link": next((r["link_url"] for r in mine if r["link_url"]), None),
            "history": [{"date": r["run_at"][:10], "n": c} for r, c in zip(daily, series)],
            "hist_ids": {r["run_at"][:10]: set(S["sets"][r["id"]].get(k, ())) for r in daily},
            "scale": {"gain": sig[0], "base": sig[1]} if sig else None,
            "ads": ads})
    rows.sort(key=lambda r: -r["active"])
    return [add_stats(r) for r in rows]


# ---------- mesmo produto em lojas diferentes (buscas por palavra-chave) ----------

NAME_STOP = {"for", "with", "and", "the", "of", "a", "an", "to", "in", "on", "new", "set", "pro", "kit",
             "1", "2", "3", "4", "5", "upgraded", "premium", "original"}
_HASH = {}


def cover_hash(path):
    """Assinatura da capa: dHash de 64 bits (forma) + cores médias numa grade 4x4 (cor).
    Criativos iguais, mesmo com compressão/corte leve, batem nas duas."""
    if path in _HASH:
        return _HASH[path]
    h = None
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        px = list(img.convert("L").resize((9, 8)).getdata())
        v = 0
        for r in range(8):
            for c in range(8):
                v = (v << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
        if 8 <= bin(v).count("1") <= 56:        # ignora imagens "vazias" (preto, uma cor só)
            h = (v, tuple(x for rgb in img.resize((4, 4)).getdata() for x in rgb))
    except Exception:
        pass
    _HASH[path] = h
    return h


def _same_creative(x, y):
    shape = bin(x[0] ^ y[0]).count("1") <= 4
    color = sum(abs(a - b) for a, b in zip(x[1], y[1])) / len(x[1]) <= 18
    return shape and color


CORE_DROP = r"\b(saker|sakerplus|aless|new|upgraded|professional|premium|portable|the|for|with|and)\b"


def name_core(name, key):
    """Nome reduzido às letras, para reconhecer palavras quebradas de propósito
    ("Magnetic ph one hol der" = "Magnetic phone holder"). Ignora "2 in 1"/"3-in-1" e palavras de marca.
    Só vale para nomes vindos do link (nomes tirados do título do anúncio, como "FLASH SALE 67% OFF",
    ficam de fora: são ofertas, não produtos)."""
    if not name or not descriptive_slug(key):
        return None
    n = re.sub(r"\b\d+\s*-?\s*in\s*-?\s*\d+\b", " ", name.lower())
    n = re.sub(CORE_DROP, " ", n)
    n = re.sub(r"[^a-z]", "", n)
    return n if len(n) >= 10 else None


def core_similar(ca, cb):
    if not ca or not cb:
        return False
    import difflib
    m = difflib.SequenceMatcher(None, ca, cb)
    return m.real_quick_ratio() >= 0.85 and m.quick_ratio() >= 0.85 and m.ratio() >= 0.85


def core_match(a, b):
    if a.get("named_by_ad") or b.get("named_by_ad"):
        return False
    return core_similar(name_core(a["name"], a["key"]), name_core(b["name"], b["key"]))


def same_product(a, b):
    """Dois produtos de lojas diferentes são o mesmo? Criativo quase idêntico, ou nomes com 60%+ das
    palavras em comum (e pelo menos 3). Validado em dados reais: junta shawl × scarf (mesmo vídeo),
    não junta 'floor drain filter' × 'funnel with filter'."""
    ha = {h for h in (cover_hash(x["cover"]) for x in a["ads"] if x["cover"]) if h is not None}
    hb = {h for h in (cover_hash(x["cover"]) for x in b["ads"] if x["cover"]) if h is not None}
    if any(_same_creative(x, y) for x in ha for y in hb):
        return True
    ta = {t for t in a["key"].split("/products/")[-1].split("-") if t and t not in NAME_STOP}
    tb = {t for t in b["key"].split("/products/")[-1].split("-") if t and t not in NAME_STOP}
    inter = ta & tb
    if len(inter) >= 3 and len(inter) / len(ta | tb) >= 0.6:
        return True
    return core_match(a, b)


def merge_across_stores(rows):
    """Agrupa linhas de produto de sites diferentes que são o mesmo produto."""
    n = len(rows)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    hosts = [r["key"].split("/")[0] for r in rows]
    for i in range(n):
        for j in range(i + 1, n):
            if hosts[i] != hosts[j] and find(i) != find(j) and same_product(rows[i], rows[j]):
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    out = []
    for idx in groups.values():
        mem = sorted((rows[i] for i in idx), key=lambda r: -r["active"])
        top = dict(mem[0])
        if len(mem) > 1:   # nome mais "limpo": link descritivo, sem palavras quebradas
            best = min(mem, key=lambda r: (r.get("named_by_ad", False), broken_words(r["name"]), -r["active"]))
            top["name"] = best["name"]
        lojas = defaultdict(int)
        for r in mem:
            lojas[r["key"].split("/")[0]] += r["active"]
        top["lojas"] = [{"host": h, "active": c} for h, c in sorted(lojas.items(), key=lambda x: -x[1])]
        if len(mem) > 1:
            ads, seen = [], set()
            for r in mem:
                for ad in r["ads"]:
                    if ad["id"] not in seen:
                        seen.add(ad["id"])
                        ads.append(dict(ad, loja=r["key"].split("/")[0]))
            ads.sort(key=lambda x: (x["rank"] if x["rank"] is not None else 10**6, -x["dup"]))
            hist_ids = union_hist(mem)
            hist = {d: len(v) for d, v in hist_ids.items()}
            series = [hist[d] for d in sorted(hist)]
            sig = scale_signal(series)
            deltas = [r["delta"] for r in mem if r["delta"] is not None]
            olds = [r["oldest"] for r in mem if r["oldest"] is not None]
            top.update({"active": len(ads), "ads": ads, "delta": sum(deltas) if deltas else None,
                        "oldest": max(olds) if olds else None,
                        "since": min(r["since"] for r in mem if r["since"]) if any(r["since"] for r in mem) else "",
                        "is_new": all(r["is_new"] for r in mem),
                        "history": [{"date": d, "n": hist[d]} for d in sorted(hist)],
                        "hist_ids": hist_ids,
                        "scale": {"gain": sig[0], "base": sig[1]} if sig else None,
                        "keys": [r["key"] for r in mem]})
        else:
            top["ads"] = [dict(ad, loja=top["key"].split("/")[0]) for ad in top["ads"]]
        out.append(add_stats(top))
    out.sort(key=lambda r: -r["active"])
    return out


def union_hist(rows):
    """Une os anúncios de cada dia de várias linhas (um anúncio visto em duas buscas conta uma vez)."""
    out = defaultdict(set)
    for r in rows:
        for d, ids in (r.get("hist_ids") or {}).items():
            out[d] |= ids
    return dict(out)


def merge_all(rows):
    """Visão "Todas": junta o mesmo produto vindo de entradas diferentes (lojas monitoradas e
    palavras-chave) numa linha só, contando cada anúncio uma vez. Junta quando há link ou anúncio
    em comum, palavras quebradas de propósito (core_match) e, entre palavras-chave, também pelo
    criativo/nome (same_product)."""
    rows = [dict(r, ads=[a if a.get("loja") else dict(a, loja=r["key"].split("/")[0]) for a in r["ads"]])
            for r in rows]
    n = len(rows)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    keysets = keysets_of(rows)
    adsets = [{a["id"] for a in r["ads"]} for r in rows]
    for i in range(n):
        for j in range(i + 1, n):
            if rows[i]["store"] == rows[j]["store"] or find(i) == find(j):
                continue
            both_kw = rows[i]["kind"] == "keyword" and rows[j]["kind"] == "keyword"
            if keysets[i] & keysets[j] or adsets[i] & adsets[j] or core_match(rows[i], rows[j]) \
                    or (both_kw and same_product(rows[i], rows[j])):
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    out = []
    for idx in groups.values():
        mem = sorted((rows[i] for i in idx), key=lambda r: -r["active"])
        top = dict(mem[0])
        keywords = sorted({r["store"] for r in mem if r["kind"] == "keyword"})
        stores = sorted({r["store"] for r in mem if r["kind"] != "keyword"})
        if len(mem) > 1:
            best = min(mem, key=lambda r: (r.get("named_by_ad", False), broken_words(r["name"]), -r["active"]))
            top["name"] = best["name"]
            ads = {}
            for r in mem:
                for ad in r["ads"]:
                    ads.setdefault(ad["id"], ad)
            ads = sorted(ads.values(), key=lambda x: (x["rank"] if x["rank"] is not None else 10**6, -x["dup"]))
            lojas = defaultdict(int)
            for ad in ads:
                lojas[ad["loja"]] += 1
            hist_ids = union_hist(mem)
            dates = sorted(hist_ids)
            series = [len(hist_ids[d]) for d in dates]
            sig = scale_signal(series)
            olds = [r["oldest"] for r in mem if r["oldest"] is not None]
            top.update({"active": len(ads), "ads": ads,
                        "lojas": [{"host": h, "active": c} for h, c in sorted(lojas.items(), key=lambda x: -x[1])],
                        "delta": (series[-1] - series[-2]) if len(series) > 1 else None,
                        "oldest": max(olds) if olds else None,
                        "since": min((r["since"] for r in mem if r["since"]), default=""),
                        "is_new": all(r["is_new"] for r in mem),
                        "history": [{"date": d, "n": c} for d, c in zip(dates, series)],
                        "hist_ids": hist_ids,
                        "scale": {"gain": sig[0], "base": sig[1]} if sig else None,
                        "keys": sorted(set().union(*keysets_of(mem)))})
            with_cover = [r for r in mem if r.get("cover")]
            top["cover"] = with_cover[0]["cover"] if with_cover else None
        lj = top.get("lojas") or [{"host": top["key"].split("/")[0], "active": top["active"]}]
        top["lojas"] = lj
        top["keywords"], top["sources"] = keywords, stores + keywords
        top["loja"] = f"{len(lj)} lojas" if len(lj) > 1 else (stores[0] if stores else lj[0]["host"])
        top["kind"] = "store" if not keywords and len(lj) == 1 else "keyword"
        out.append(add_stats(top))
    out.sort(key=lambda r: -r["active"])
    return out


def keysets_of(rows):
    return [set(r.get("keys") or [r["key"]]) for r in rows]


def is_keyword_source(st):
    return bool(json.loads(st["keywords"] or "[]")) and not json.loads(st["domains"] or "[]") \
        and not json.loads(st["page_ids"] or "[]")


def rows_for(st, S):
    rows = products_of(S)
    return merge_across_stores(rows) if is_keyword_source(st) else rows


def report_store(con, st, a, csv_rows):
    """Resumo curto em Markdown (o detalhe fica na planilha interativa)."""
    S = store_state(con, st)
    kws = json.loads(st["keywords"] or "[]")
    out = [f"## {'🔎 ' if kws else ''}{st['name']}" + (f" (palavra-chave: {', '.join(kws)})" if kws else ""), ""]
    if not S:
        return out + ["Ainda sem coletas.", ""]
    cur, prev = S["cur"], S["prev"]
    rows = rows_for(st, S)
    n_prev = len(S["counts"][prev["id"]]) if prev else None
    out += [f"Coleta: **{cur['run_at'][:10]}**" + (f" · comparada com {prev['run_at'][:10]}" if prev else ""), "",
            f"- **Anúncios na amostra:** {cur['n_ads']} (a Biblioteca indica ~{cur['total_reported']} ativos)",
            f"- **Produtos anunciados:** {len(rows)}" + (f" ({len(rows) - n_prev:+d})" if prev else ""), ""]
    hot = sorted((r for r in rows if r["scale"]), key=lambda r: -r["scale"]["gain"])
    if hot:
        out += ["**🚀 Possível início de escala:**", ""]
        out += [f"- {r['name']}: {r['scale']['base']} → {r['active']} anúncios" for r in hot] + [""]
    cons = sorted((r for r in rows if r["flags"]["old"]), key=lambda r: (-r["old_ads"], -r["active"]))[:5]
    if cons:
        out += ["**⏳ Vencedores consolidados** (3+ anúncios com 30+ dias ainda no top):", ""]
        out += [f"- {r['name']}: {r['old_ads']} de {r['active']} anúncios com 30+ dias · {len(r['pages'])} página(s) · "
                f"{r['dup_total']} duplicações" for r in cons] + [""]
    for r in rows:
        csv_rows["produtos"].append({"loja": st["name"], "data": cur["run_at"][:10], "produto": r["name"],
                                     "chave": r["key"], "anuncios_ativos": r["active"],
                                     "variacao": r["delta"] if r["delta"] is not None else "",
                                     "dias_anuncio_mais_antigo": r["oldest"], "monitorando_desde": r["since"],
                                     "possivel_escala": "sim" if r["scale"] else "",
                                     "duplicacoes": r["dup_total"], "paginas": len(r["pages"]),
                                     "anuncios_30d_no_top": r["old_ads"],
                                     "sites": ", ".join(f"{l['host']} ({l['active']})" for l in r.get("lojas", []))})
        for ad in r["ads"]:
            csv_rows["anuncios"].append({"loja": st["name"], "produto": r["name"], "ad_id": ad["id"],
                                         "posicao_impressoes": ad["rank"], "duplicacoes": ad["dup"],
                                         "dias_no_ar": ad["days"], "pagina": ad["page"], "formato": ad["format"],
                                         "titulo": ad["title"],
                                         "link_ad_library": f"https://www.facebook.com/ads/library/?id={ad['id']}",
                                         "capa": ad["cover"] or ""})
    for run in S["daily"]:
        for k, n in S["counts"][run["id"]].items():
            csv_rows["historico"].append({"loja": st["name"], "data": run["run_at"][:10], "produto": S["name"](k),
                                          "chave": k, "anuncios_ativos": n})
    return out


# ---------------- sugestões de lojas (a partir das palavras-chave) ----------------

NOT_STORES = ("amazon.", "walmart.", "temu.", "aliexpress.", "alibaba.", "ebay.", "etsy.", "target.com", "bestbuy.",
              "facebook.", "fb.me", "fb.com", "instagram.", "messenger.", "whatsapp.", "wa.me", "threads.", "youtube.",
              "youtu.be", "tiktok.", "google.", "apple.com", "apps.apple", "play.google", "linktr.ee", "bit.ly",
              "shein.", "wish.com", "costco.", "homedepot.", "lowes.", "kickstarter.", "indiegogo.", "gofundme.")


def suggestion_name(host):
    base = re.sub(r"^(www|m|shop|store|us|try|get|go|buy)\.", "", host).split(".")[0]
    return base[:1].upper() + base[1:]


def compute_suggestions(con, top=10):
    """Lojas que aparecem nas buscas por palavra-chave e não são monitoradas, ranqueadas por sinais
    de escala. Não gasta créditos: usa só os dados já coletados."""
    monitored = set()
    for st in con.execute("SELECT * FROM stores").fetchall():
        monitored.update(json.loads(st["domains"] or "[]"))
    H = defaultdict(lambda: {"keywords": set(), "countries": [], "hist": defaultdict(set), "ads": {},
                             "products": [], "oldest": None})
    for st in con.execute("SELECT * FROM stores ORDER BY name").fetchall():
        if not is_keyword_source(st):
            continue
        S = store_state(con, st)
        if not S:
            continue
        # anúncios por site em cada dia (última coleta do dia)
        for run in S["daily"]:
            for r in con.execute("""SELECT s.ad_id, ap.product_key FROM sightings s
                                    JOIN ad_products ap ON ap.ad_id=s.ad_id AND ap.store_id=?
                                    WHERE s.run_id=?""", (st["id"], run["id"])):
                H[r["product_key"].split("/")[0]]["hist"][run["run_at"][:10]].add(r["ad_id"])
        for row in products_of(S):
            host = row["key"].split("/")[0]
            h = H[host]
            h["keywords"].add(st["name"])
            if st["country"] not in h["countries"]:
                h["countries"].append(st["country"])
            h["products"].append(row)
            for ad in row["ads"]:
                h["ads"][ad["id"]] = dict(ad, loja=host)
            if row["oldest"] is not None:
                h["oldest"] = max(h["oldest"] or 0, row["oldest"])
    out = []
    for host, h in H.items():
        if not h["ads"] or any(host == d or host.endswith("." + d) for d in monitored) \
                or any(x in host for x in NOT_STORES):
            continue
        dates = sorted(h["hist"])
        series = [len(h["hist"][d]) for d in dates]
        cur = len(h["ads"])
        k = min(3, len(series) - 1)
        base = series[-1 - k] if k > 0 else None
        growth = (cur - base) if base is not None else 0
        scaling = [p for p in h["products"] if p["scale"]]
        # penaliza só sites que não levam a nenhuma página além da inicial, ou subdomínios de conteúdo
        content = re.match(r"(info|information|news|blog|read|article|articles|stories|magazine)\.", host)
        has_products = not content and any(p["key"].split("/", 1)[1].strip("/") for p in h["products"])
        if cur < 2:
            continue
        host_pages = sorted({a["page"] for a in h["ads"].values() if a.get("page")})
        host_dup = sum(a["dup"] or 1 for a in h["ads"].values())
        n_dup = sum(1 for p in h["products"] if p["flags"]["dup"])
        n_pg = sum(1 for p in h["products"] if p["flags"]["pages"])
        n_old = sum(1 for p in h["products"] if p["flags"]["old"])
        score = 10 * len(scaling) + 2 * max(growth, 0) + 5 * (len(h["keywords"]) - 1) + cur + (3 if has_products else -5) \
            + 3 * n_dup + 3 * n_pg + 4 * n_old + (3 if len(host_pages) >= PAGES_MIN else 0)
        reasons = []
        if scaling:
            reasons.append(f"{len(scaling)} produto{'s' if len(scaling) > 1 else ''} escalando")
        if base is not None and growth > 0:
            reasons.append(f"anúncios {base} → {cur} em {k} coleta{'s' if k > 1 else ''}")
        if n_old:
            reasons.append(f"⏳ {n_old} produto{'s' if n_old > 1 else ''} consolidado{'s' if n_old > 1 else ''}")
        if len(host_pages) >= PAGES_MIN:
            reasons.append(f"📄 anuncia por {len(host_pages)} páginas")
        if host_dup >= DUP_MIN:
            reasons.append(f"🔁 criativos duplicados {host_dup}×")
        if len(h["keywords"]) > 1:
            reasons.append(f"aparece em {len(h['keywords'])} palavras-chave")
        reasons.append(f"{cur} anúncios na amostra")
        if h["oldest"]:
            reasons.append(f"anúncio mais antigo: {h['oldest']} dia{'s' if h['oldest'] != 1 else ''}")
        prods = sorted(h["products"], key=lambda p: (-(p["scale"] is not None), -p["active"]))
        ads = sorted(h["ads"].values(), key=lambda x: (x["rank"] if x["rank"] is not None else 10**6, -x["dup"]))
        out.append({"host": host, "name": suggestion_name(host), "keywords": sorted(h["keywords"]),
                    "country": h["countries"][0], "score": score, "reasons": reasons, "active": cur,
                    "growth": growth if base is not None else None, "scaling": len(scaling),
                    "n_products": len(h["products"]), "oldest": h["oldest"], "has_products": has_products,
                    "history": [{"date": d, "n": n} for d, n in zip(dates, series)],
                    "products": [{"name": p["name"], "active": p["active"], "cover": p["cover"],
                                  "scale": bool(p["scale"])} for p in prods[:4]],
                    "ads": ads})
    out.sort(key=lambda x: (-x["score"], -x["active"]))
    out = out[:top]
    for x in out:   # total de anúncios ativos na Biblioteca (último verificado)
        t = con.execute("SELECT date, total FROM store_totals WHERE host=? ORDER BY date DESC LIMIT 1",
                        (x["host"],)).fetchone()
        x["total"], x["total_date"] = (t["total"], t["date"]) if t else (None, None)
        if x["total"]:
            i = next((n + 1 for n, r in enumerate(x["reasons"]) if "na amostra" in r), len(x["reasons"]))
            x["reasons"].insert(i, f"~{x['total']:,} ativos na Biblioteca".replace(",", "."))
    return out


def cmd_suggest(con, a):
    """Calcula as sugestões e, com --verify, consulta o total de anúncios ativos de cada loja sugerida
    na Biblioteca (1 crédito por loja, só o campo de total da 1ª página)."""
    sug = compute_suggestions(con, a.top)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if a.verify and sug:
        if get_source(a) != "scrapecreators":
            print("Verificação do total exige a fonte scrapecreators; pulando.", file=sys.stderr)
        else:
            for x in sug:
                if con.execute("SELECT 1 FROM store_totals WHERE host=? AND date=?", (x["host"], today)).fetchone():
                    continue                                   # já verificada hoje
                try:
                    d = sc()._call("search/ads", {"query": x["host"], "country": x["country"], "status": "ACTIVE"})
                    total = d.get("searchResultsCount")
                    con.execute("INSERT OR REPLACE INTO store_totals VALUES(?,?,?,?)",
                                (x["host"], today, x["country"], total))
                    con.commit()
                except Exception as e:
                    print(f"  não consegui verificar {x['host']}: {e}", file=sys.stderr)
            sug = compute_suggestions(con, a.top)
            print(f"Créditos ScrapeCreators usados na verificação: {_SC.credits_used if _SC else 0}", file=sys.stderr)
    for i, x in enumerate(sug, 1):
        print(f"{i:>2}. {x['host']} · pontos {x['score']} · {' · '.join(x['reasons'])}", file=sys.stderr)


def cmd_report(con, a):
    import csv
    stores = [get_store(con, n) for n in a.store] if a.store else con.execute("SELECT * FROM stores ORDER BY name").fetchall()
    csv_rows = {"produtos": [], "historico": [], "anuncios": []}
    lines = [f"# Monitoramento de anúncios — {datetime.now().strftime('%d/%m/%Y')}", ""]
    if os.environ.get("PAINEL_URL"):
        lines += [f"📊 **Planilha interativa:** {os.environ['PAINEL_URL']}", ""]
    for st in stores:
        lines += report_store(con, st, a, csv_rows)
    sug = compute_suggestions(con, 10)
    if sug:
        lines += ["## 💡 Lojas sugeridas para monitorar", "",
                  "_Lojas encontradas nas buscas por palavra-chave que ainda não são monitoradas._", ""]
        lines += [f"{i}. **{x['host']}** — {' · '.join(x['reasons'])}" for i, x in enumerate(sug, 1)] + [""]
    md = "\n".join(lines)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
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


def cmd_painel(con, a):
    """Gera a planilha interativa (site estático: index.html + capas) para o GitHub Pages."""
    import shutil
    stores = con.execute("SELECT * FROM stores ORDER BY name").fetchall()
    data = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stores": [], "products": []}
    covers = set()
    for st in stores:
        S = store_state(con, st)
        if not S:
            continue
        rows = rows_for(st, S)
        kws = json.loads(st["keywords"] or "[]")
        is_kw = is_keyword_source(st)
        data["stores"].append({"name": st["name"], "kind": "keyword" if is_kw else "store", "query": ", ".join(kws),
                               "date": S["cur"]["run_at"][:10],
                               "prev": S["prev"]["run_at"][:10] if S["prev"] else None,
                               "sample": S["cur"]["n_ads"], "total": S["cur"]["total_reported"],
                               "products": len(rows), "runs": len(S["daily"])})
        for r in rows:
            r["store"] = st["name"]
            r["kind"] = "keyword" if is_kw else "store"
            # coluna "Loja": numa busca por palavra-chave mostra o site de quem anuncia
            if is_kw:
                lj = r.get("lojas") or [{"host": r["key"].split("/")[0], "active": r["active"]}]
                r["loja"] = lj[0]["host"] if len(lj) == 1 else f"{len(lj)} lojas"
            else:
                r["loja"] = st["name"]
            covers.update(x for x in [r["cover"]] + [ad["cover"] for ad in r["ads"]] if x)
            data["products"].append(r)
    data["stores"].sort(key=lambda x: (x["kind"] == "keyword", x["name"].lower()))   # lojas primeiro
    data["all"] = merge_all(data["products"])
    for r in data["all"]:
        r["store"] = "__all__"             # identifica a visão "Todas" (sources = entradas de origem)
    for r in data["products"] + data["all"]:
        r.pop("hist_ids", None)
    data["suggestions"] = compute_suggestions(con, 10)
    data["repo"] = os.environ.get("GITHUB_REPOSITORY", "")
    for x in data["suggestions"]:
        covers.update(c for c in [p["cover"] for p in x["products"]] + [ad["cover"] for ad in x["ads"]] if c)
    os.makedirs(a.out, exist_ok=True)
    for c in covers:                                          # só as capas usadas na planilha
        dst = os.path.join(a.out, c)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(c, dst)
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "painel_template.html"),
               encoding="utf-8").read()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    open(os.path.join(a.out, "index.html"), "w", encoding="utf-8").write(tpl.replace("/*__DATA__*/null", payload))
    open(os.path.join(a.out, ".nojekyll"), "w").close()
    print(f"Planilha: {a.out}/index.html ({len(data['products'])} produtos, {len(covers)} capas)", file=sys.stderr)


# ---------------- CLI ----------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=os.environ.get("ADS_MONITOR_DIR", "ads-monitor"))
    sub = p.add_subparsers(dest="cmd", required=True)

    def scraping_opts(sp):
        sp.add_argument("--source", choices=["scrapecreators", "browser"],
                        help="fonte dos dados (padrão: scrapecreators se SCRAPECREATORS_API_KEY existir, "
                             "senão browser; ou $ADS_SOURCE)")
        sp.add_argument("--max-ads", type=int, default=150,
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
    sp = sub.add_parser("suggest", help="sugere lojas vistas nas palavras-chave (--verify: total na Biblioteca)")
    sp.add_argument("--top", type=int, default=10)
    sp.add_argument("--verify", action="store_true", help="1 crédito por loja: total de anúncios ativos")
    sp.add_argument("--source", choices=["scrapecreators", "browser"])
    sp = sub.add_parser("painel", help="gera a planilha interativa (site para o GitHub Pages)")
    sp.add_argument("--out", default="site")
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
    {"add": cmd_add, "resolve": cmd_resolve, "list": cmd_list, "sync": cmd_sync, "rekey": cmd_rekey, "painel": cmd_painel, "suggest": cmd_suggest, "remove": cmd_remove,
     "run": cmd_run, "report": cmd_report}[a.cmd](con, a)


if __name__ == "__main__":
    main()

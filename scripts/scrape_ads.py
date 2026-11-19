#!/usr/bin/env python3
"""Coleta anúncios da Meta Ad Library direto do site público, com um navegador (Playwright).

Não usa Apify nem token. Abre a URL da Ad Library, lê os dados que a própria página
carrega (JSON embutido no HTML + respostas /api/graphql/ ao rolar) e salva em JSON
no mesmo formato da skill do Apify (campos camelCase: adArchiveID, snapshot, isActive...).

Instalação (uma vez):
  pip install playwright && python -m playwright install chromium

Exemplos:
  python scrape_ads.py --url "https://www.facebook.com/ads/library/?...&view_all_page_id=16453004404" --limit 50
  python scrape_ads.py --keyword "curso de inglês" --country BR --limit 100 --out ads.json
  python scrape_ads.py --page-id 16453004404 --sort impressions --only-total
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_url import build_url  # noqa: E402

COOKIE_BUTTONS = [
    "Allow all cookies", "Decline optional cookies", "Only allow essential cookies",
    "Permitir todos os cookies", "Recusar cookies opcionais", "Permitir apenas cookies essenciais",
    "Aceptar todas las cookies", "Rechazar cookies opcionales",
]
SCRIPT_JSON = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


# ---------- extração ----------

def walk(obj, ads, totals):
    """Percorre qualquer JSON e coleta anúncios (dicts com ad_archive_id + snapshot)."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if "ad_archive_id" in o and "snapshot" in o:
                ads.append(o)
                continue
            src = o.get("search_results_connection")
            if isinstance(src, dict) and isinstance(src.get("count"), int):
                totals.append(src["count"])
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)


def parse_text(text, ads, totals):
    """Respostas do Facebook podem vir com 'for (;;);' e/ou várias linhas JSON."""
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[9:]
    for chunk in text.split("\n"):
        chunk = chunk.strip()
        if not chunk or chunk[0] not in "[{":
            continue
        try:
            walk(json.loads(chunk), ads, totals)
        except json.JSONDecodeError:
            pass


def parse_html(html, ads, totals):
    for blob in SCRIPT_JSON.findall(html):
        if "ad_archive_id" in blob or "search_results_connection" in blob:
            parse_text(blob, ads, totals)


# ---------- normalização (snake_case -> camelCase, igual à saída do Apify) ----------

def camel(k):
    head, *rest = k.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def to_camel(o):
    if isinstance(o, dict):
        return {camel(k): to_camel(v) for k, v in o.items()}
    if isinstance(o, list):
        return [to_camel(v) for v in o]
    return o


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def normalize(raw, input_url):
    ad = to_camel(raw)
    ad["adArchiveID"] = ad.get("adArchiveId")
    ad["pageID"] = ad.get("pageId")
    ad["inputUrl"] = input_url
    ad["startDateFormatted"] = iso(ad.get("startDate"))
    ad["endDateFormatted"] = iso(ad.get("endDate"))
    ad["adLibraryUrl"] = f"https://www.facebook.com/ads/library/?id={ad['adArchiveID']}"
    return ad


# ---------- navegação ----------

def dismiss_cookies(page):
    for label in COOKIE_BUTTONS:
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(800)
                return
        except Exception:
            pass


def scrape(url, limit, only_total, headful, max_idle, chromium_path):
    from playwright.sync_api import sync_playwright

    ads, totals = [], []
    with sync_playwright() as p:
        kw = {"headless": not headful}
        if chromium_path:
            kw["executable_path"] = chromium_path
        browser = p.chromium.launch(**kw)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900},
                                  user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"))
        page = ctx.new_page()

        def on_response(resp):
            if "/api/graphql" in resp.url:
                try:
                    parse_text(resp.text(), ads, totals)
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # A Meta costuma responder 403 com um desafio JS e recarregar a página sozinha;
        # os anúncios só chegam depois, via JavaScript (o HTML inicial vem com count=0).
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        dismiss_cookies(page)
        parse_html(page.content(), ads, totals)

        if "login" in page.url or "checkpoint" in page.url:
            print("AVISO: o Facebook redirecionou para login/checkpoint. Tente --headful, "
                  "outra rede, ou espere alguns minutos.", file=sys.stderr)

        if not only_total:
            idle, last = 0, -1
            while idle < max_idle:
                unique = len({a["ad_archive_id"] for a in ads})
                if limit and unique >= limit:
                    break
                if unique == last:
                    idle += 1
                else:
                    idle, last = 0, unique
                    print(f"  {unique} anúncios coletados...", file=sys.stderr)
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(2000 + 500 * idle)

        if not ads and not only_total:
            shot = "adlibrary_debug.png"
            page.screenshot(path=shot, full_page=False)
            print(f"Nenhum anúncio encontrado. Captura de tela salva em {shot} para diagnóstico. "
                  "Página em branco = scripts do Facebook (static.xx.fbcdn.net) bloqueados pela rede/firewall.",
                  file=sys.stderr)
        browser.close()
    return ads, (max(totals) if totals else None)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("alvo (use --url OU --keyword/--page-id)")
    src.add_argument("--url", action="append", default=[], help="URL da Ad Library (repetível)")
    src.add_argument("--keyword")
    src.add_argument("--page-id", action="append", default=[])
    src.add_argument("--exact", action="store_true")
    f = p.add_argument_group("filtros para --keyword/--page-id")
    f.add_argument("--country", default="ALL")
    f.add_argument("--status", default="active", choices=["active", "inactive", "all"])
    f.add_argument("--media", default="all", choices=["all", "image", "video", "meme", "image_and_meme", "none"])
    f.add_argument("--platform", action="append", default=[])
    f.add_argument("--language", action="append", default=[])
    f.add_argument("--sort", choices=["impressions", "recent"])
    f.add_argument("--start-min")
    f.add_argument("--start-max")
    p.add_argument("--limit", type=int, default=50, help="máx. de anúncios por URL (0 = até acabar)")
    p.add_argument("--only-total", action="store_true", help="só o total de anúncios exibido pela Ad Library")
    p.add_argument("--max-idle", type=int, default=4, help="rolagens sem novidade antes de parar")
    p.add_argument("--headful", action="store_true", help="abre o navegador visível (ajuda a passar bloqueios)")
    p.add_argument("--chromium-path", default=os.environ.get("CHROMIUM_PATH"))
    p.add_argument("--out", default="ads.json")
    a = p.parse_args()

    urls = list(a.url)
    common = dict(country=a.country, status=a.status, media=a.media, platforms=a.platform,
                  languages=a.language, sort=a.sort, start_min=a.start_min, start_max=a.start_max)
    if a.keyword:
        urls.append(build_url(keyword=a.keyword, exact=a.exact, **common))
    for pid in a.page_id:
        urls.append(build_url(page_id=pid, **common))
    if not urls:
        p.error("informe --url, --keyword ou --page-id")
    for u in urls:
        if "/ads/library" not in u:
            p.error(f"'{u}' não é uma URL da Ad Library. Para páginas do Facebook, use --page-id "
                    "(o ID aparece na aba 'Transparência da Página' ou na própria Ad Library).")

    results = []
    for url in urls:
        print(f"Abrindo: {url}", file=sys.stderr)
        raw, total = scrape(url, a.limit, a.only_total, a.headful, a.max_idle, a.chromium_path)
        if a.only_total:
            results.append({"inputUrl": url, "totalCount": total})
            print(f"  total: {total}", file=sys.stderr)
            continue
        seen, ads = set(), []
        for r in raw:
            if r["ad_archive_id"] not in seen:
                seen.add(r["ad_archive_id"])
                ads.append(normalize(r, url))
        if a.limit:
            ads = ads[:a.limit]
        print(f"  {len(ads)} anúncios (total informado pela Ad Library: {total})", file=sys.stderr)
        results.extend(ads)

    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print(f"Salvo em {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Cliente mínimo da API da ScrapeCreators (Facebook Ad Library).

Docs: https://docs.scrapecreators.com/v1/facebook/adLibrary/search/ads
Chave: variável SCRAPECREATORS_API_KEY (header x-api-key). Cada chamada custa 1 crédito e traz ~30 anúncios.
Os anúncios vêm no mesmo formato do site (snake_case: ad_archive_id, snapshot...), então
scrape_ads.normalize() funciona igual.

Uso direto (teste):
  python sc_client.py search "minhaloja.com" --country US --max-ads 60
  python sc_client.py company 123456789 --max-ads 60
  python sc_client.py companies "nome da loja"
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("SCRAPECREATORS_BASE", "https://api.scrapecreators.com") + "/v1/facebook/adLibrary"


class ScrapeCreators:
    def __init__(self, api_key=None, verbose=True):
        self.key = api_key or os.environ.get("SCRAPECREATORS_API_KEY")
        if not self.key:
            sys.exit("SCRAPECREATORS_API_KEY não definido. Crie uma chave em https://app.scrapecreators.com "
                     "e exporte como variável de ambiente.")
        self.credits_used = 0
        self.credits_remaining = None
        self.verbose = verbose

    def _call(self, path, params):
        params = {k: v for k, v in params.items() if v not in (None, "")}
        url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
        headers = {"x-api-key": self.key, "Accept": "application/json"}
        if len(url) > 6000:  # cursor muito grande: a API aceita os mesmos parâmetros via POST
            req = urllib.request.Request(f"{BASE}/{path}",
                                         data=json.dumps(params).encode(), method="POST",
                                         headers={**headers, "Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url, headers=headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:300]
                if e.code in (401, 403):
                    sys.exit(f"ScrapeCreators recusou a chave ({e.code}): {body}")
                if e.code == 402:
                    sys.exit(f"Créditos da ScrapeCreators esgotados: {body}")
                if e.code < 500 or attempt == 3:
                    raise RuntimeError(f"ScrapeCreators {e.code}: {body}")
            except urllib.error.URLError as e:
                if attempt == 3:
                    raise RuntimeError(f"Falha de rede na ScrapeCreators: {e}")
            time.sleep(3 * (attempt + 1))
        self.credits_used += data.get("credits_charged") or 0
        if data.get("credits_remaining") is not None:
            self.credits_remaining = data["credits_remaining"]
        if data.get("success") is False:
            raise RuntimeError(f"ScrapeCreators retornou erro: {str(data)[:300]}")
        return data

    def _paginate(self, path, params, results_key, max_ads):
        ads, cursor, total, pages = [], None, None, 0
        while True:
            data = self._call(path, {**params, "cursor": cursor})
            pages += 1
            batch = data.get(results_key) or []
            ads += batch
            total = data.get("searchResultsCount", total)
            cursor = data.get("cursor")
            if self.verbose:
                print(f"    página {pages}: +{len(batch)} (total {len(ads)}"
                      f"{f' de ~{total}' if total else ''}) · créditos restantes {self.credits_remaining}",
                      file=sys.stderr)
            if not batch or not cursor or (max_ads and len(ads) >= max_ads):
                break
        capped = bool(cursor) and bool(max_ads) and len(ads) >= max_ads
        return (ads[:max_ads] if max_ads else ads), total, capped

    def search_ads(self, query, country="ALL", status="ACTIVE", sort_by=None, media_type=None,
                   language=None, exact=False, max_ads=0):
        params = {"query": query, "country": country, "status": status, "sort_by": sort_by,
                  "media_type": media_type, "language": language,
                  "search_type": "keyword_exact_phrase" if exact else None}
        return self._paginate("search/ads", params, "searchResults", max_ads)

    def company_ads(self, page_id, country="ALL", status="ACTIVE", sort_by=None, media_type=None,
                    language=None, max_ads=0):
        params = {"pageId": page_id, "country": country, "status": status, "sort_by": sort_by,
                  "media_type": media_type, "language": language}
        return self._paginate("company/ads", params, "results", max_ads)

    def search_companies(self, query):
        return self._call("search/companies", {"query": query}).get("searchResults") or []


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["search", "company", "companies"])
    p.add_argument("query")
    p.add_argument("--country", default="ALL")
    p.add_argument("--status", default="ACTIVE")
    p.add_argument("--max-ads", type=int, default=30)
    p.add_argument("--out")
    a = p.parse_args()
    sc = ScrapeCreators()
    if a.cmd == "companies":
        res = sc.search_companies(a.query)
    elif a.cmd == "search":
        res = sc.search_ads(a.query, a.country, a.status, max_ads=a.max_ads)[0]
    else:
        res = sc.company_ads(a.query, a.country, a.status, max_ads=a.max_ads)[0]
    out = json.dumps(res, ensure_ascii=False, indent=2)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(out)
    else:
        print(out)
    print(f"créditos usados: {sc.credits_used} · restantes: {sc.credits_remaining}", file=sys.stderr)

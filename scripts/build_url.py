#!/usr/bin/env python3
"""Monta uma URL da Meta Ad Library a partir de filtros.

Exemplos:
  python build_url.py --keyword "curso de inglês" --country BR
  python build_url.py --page-id 16453004404 --country ALL --sort impressions
  python build_url.py --keyword skincare --country US --media video --platform instagram --language en
"""
import argparse
from urllib.parse import urlencode

BASE = "https://www.facebook.com/ads/library/"


def build_url(keyword=None, page_id=None, country="ALL", status="active", ad_type="all",
              media="all", platforms=(), languages=(), sort=None, exact=False,
              start_min=None, start_max=None):
    params = [
        ("active_status", status),
        ("ad_type", ad_type),
        ("country", country.upper()),
        ("is_targeted_country", "false"),
        ("media_type", media),
    ]
    for i, lang in enumerate(languages):
        params.append((f"content_languages[{i}]", lang))
    for i, plat in enumerate(platforms):
        params.append((f"publisher_platforms[{i}]", plat))
    if start_min:
        params.append(("start_date[min]", start_min))
    if start_max:
        params.append(("start_date[max]", start_max))
    if page_id:
        params += [("search_type", "page"), ("view_all_page_id", str(page_id))]
    elif keyword:
        params += [("q", keyword), ("search_type", "keyword_exact_phrase" if exact else "keyword_unordered")]
    else:
        raise SystemExit("Informe --keyword ou --page-id")
    if sort == "impressions":
        params += [("sort_data[mode]", "total_impressions"), ("sort_data[direction]", "desc")]
    elif sort == "recent":
        params += [("sort_data[mode]", "relevancy_monthly_grouped"), ("sort_data[direction]", "desc")]
    return BASE + "?" + urlencode(params, safe="[]")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--keyword")
    p.add_argument("--page-id")
    p.add_argument("--exact", action="store_true", help="busca pela frase exata")
    p.add_argument("--country", default="ALL", help="ISO-2 (BR, US, PT...) ou ALL")
    p.add_argument("--status", default="active", choices=["active", "inactive", "all"])
    p.add_argument("--ad-type", default="all",
                   choices=["all", "political_and_issue_ads", "housing_ads", "employment_ads", "credit_ads"])
    p.add_argument("--media", default="all", choices=["all", "image", "video", "meme", "image_and_meme", "none"])
    p.add_argument("--platform", action="append", default=[],
                   choices=["facebook", "instagram", "messenger", "audience_network", "whatsapp", "threads"])
    p.add_argument("--language", action="append", default=[], help="código ISO-639-1 (pt, en, es...)")
    p.add_argument("--sort", choices=["impressions", "recent"])
    p.add_argument("--start-min", help="YYYY-MM-DD")
    p.add_argument("--start-max", help="YYYY-MM-DD")
    a = p.parse_args()
    print(build_url(a.keyword, a.page_id, a.country, a.status, a.ad_type, a.media,
                    a.platform, a.language, a.sort, a.exact, a.start_min, a.start_max))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Fetch a list of RSS/Atom feeds and write a single aggregated feed.json
Run on a schedule (see .github/workflows/update-feed.yml) — not in the browser.
"""

import json
import re
import sys
from datetime import datetime, timezone
from html import unescape
from time import mktime

import feedparser
import requests

# ---- Edit this list to add/remove blogs (RSS/Atom feeds) ----
FEEDS = [
    "https://blog.janestreet.com/feed.xml",
    "https://www.twosigma.com/articles/feed/",
    "https://www.databricks.com/feed",
    "https://netflixtechblog.com/feed",
    "https://engineering.fb.com/feed/",
    # Add more as you find them. Not every quant shop has one —
    # check each site for /feed, /rss.xml, /atom.xml before adding.
]

# ---- Sources with no RSS/Atom feed but a usable JSON API instead ----
# HRT retired their RSS feed (hrtbeat/feed/ 301-redirects to the plain blog
# page), but the site is WordPress and still exposes its REST API with real
# publish dates, so we pull from there instead of scraping HTML.
WORDPRESS_API_FEEDS = [
    {
        "source": "Hudson River Trading (HRTBeat)",
        "url": "https://www.hudsonrivertrading.com/wp-json/wp/v2/posts?per_page=20",
    },
]

TIMEOUT_SECONDS = 10
OUTPUT_PATH = "feed.json"


def parse_entry_date(entry):
    """Return an ISO timestamp, falling back gracefully if missing."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            dt = datetime.fromtimestamp(mktime(struct), tz=timezone.utc)
            return dt.isoformat()
    return datetime.now(tz=timezone.utc).isoformat()


def fetch_one(url):
    items = []
    try:
        resp = requests.get(
            url,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": "personal-rss-aggregator/1.0"},
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)

        source_name = parsed.feed.get("title", url)

        for entry in parsed.entries:
            items.append(
                {
                    "title": entry.get("title", "Untitled"),
                    "link": entry.get("link", url),
                    "source": source_name,
                    "published": parse_entry_date(entry),
                }
            )
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def fetch_wordpress_api(url, source_name):
    """Pull posts from a WordPress REST API endpoint (wp-json/wp/v2/posts)."""
    items = []
    try:
        resp = requests.get(
            url,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": "personal-rss-aggregator/1.0"},
        )
        resp.raise_for_status()
        for post in resp.json():
            title = unescape(re.sub("<[^>]+>", "", post.get("title", {}).get("rendered", "Untitled")))
            date_gmt = post.get("date_gmt")
            if date_gmt:
                published = datetime.fromisoformat(date_gmt).replace(tzinfo=timezone.utc).isoformat()
            else:
                published = datetime.now(tz=timezone.utc).isoformat()
            items.append(
                {
                    "title": title,
                    "link": post.get("link", url),
                    "source": source_name,
                    "published": published,
                }
            )
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def main():
    all_items = []
    for url in FEEDS:
        all_items.extend(fetch_one(url))
    for feed in WORDPRESS_API_FEEDS:
        all_items.extend(fetch_wordpress_api(feed["url"], feed["source"]))

    all_items.sort(key=lambda x: x["published"], reverse=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "items": all_items,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"Wrote {len(all_items)} items from {len(FEEDS)} feeds to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

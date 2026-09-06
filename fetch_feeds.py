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
from bs4 import BeautifulSoup

# ---- Edit this list to add/remove blogs (RSS/Atom feeds) ----
# Quant/systematic-fund sources only. Not every firm has a real feed — check
# each site for /feed, /rss.xml, /atom.xml before adding (and verify it
# actually has entries: some return HTTP 200 with an empty feed shell).
FEEDS = [
    "https://blog.janestreet.com/feed.xml",
    "https://www.twosigma.com/articles/feed/",
    "https://www.gresearch.com/news/feed/",
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

# ---- Sources with no feed or API at all: scraped from their HTML ----
# These are fragile by nature — they'll silently return 0 items (not an
# error) if the site redesigns its markup. Some have real publish dates in
# the page; others don't, so we stamp those with a "first seen" date the
# first time we encounter a given link and reuse it on later runs (see
# SCRAPE_STATE_PATH) so items don't keep resorting to the top every hour.
TIMEOUT_SECONDS = 10
OUTPUT_PATH = "feed.json"
SCRAPE_STATE_PATH = "scrape_state.json"
SCRAPE_USER_AGENT = "personal-rss-aggregator/1.0"


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


def load_scrape_state():
    try:
        with open(SCRAPE_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def first_seen_published(link, scrape_state):
    if link not in scrape_state:
        scrape_state[link] = datetime.now(tz=timezone.utc).isoformat()
    return scrape_state[link]


def scrape_get(url):
    resp = requests.get(url, timeout=TIMEOUT_SECONDS, headers={"User-Agent": SCRAPE_USER_AGENT})
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def fetch_optiver():
    """Real per-post dates live in <time datetime="..."> inside each <li> card."""
    url = "https://www.optiver.com/insights/technology-blog/"
    source_name = "Optiver Technology Blog"
    items = []
    try:
        soup = scrape_get(url)
        seen = set()
        for li in soup.select("ul li"):
            time_tag = li.find("time")
            a = li.find("a", href=True)
            if not (time_tag and time_tag.get("datetime") and a):
                continue
            link = a["href"]
            if link.startswith("/"):
                link = "https://www.optiver.com" + link
            if link in seen:
                continue
            seen.add(link)
            title_tag = li.find(["h1", "h2", "h3", "h4"])
            title = title_tag.get_text(strip=True) if title_tag else a.get_text(strip=True)
            published = datetime.fromisoformat(time_tag["datetime"]).replace(tzinfo=timezone.utc).isoformat()
            items.append({"title": title, "link": link, "source": source_name, "published": published})
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def fetch_aqr():
    """Real per-post dates live in a <p class="...date..."> next to each title link."""
    base = "https://www.aqr.com"
    url = f"{base}/Insights/Perspectives"
    source_name = "AQR Perspectives"
    items = []
    try:
        soup = scrape_get(url)
        date_tags = soup.find_all(
            lambda tag: tag.name == "p" and tag.get("class") and any("date" in c for c in tag.get("class"))
        )
        seen = set()
        for date_tag in date_tags:
            card = date_tag.find_parent(["div", "article", "li"])
            a = card.find("a", href=True) if card else None
            if not a:
                continue
            link = a["href"]
            if link.startswith("/"):
                link = base + link
            if link in seen:
                continue
            seen.add(link)
            title = a.get_text(strip=True)
            try:
                published = (
                    datetime.strptime(date_tag.get_text(strip=True), "%B %d, %Y")
                    .replace(tzinfo=timezone.utc)
                    .isoformat()
                )
            except ValueError:
                published = datetime.now(tz=timezone.utc).isoformat()
            items.append({"title": title, "link": link, "source": source_name, "published": published})
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def fetch_imc(scrape_state):
    """No publish dates anywhere on the page or in article meta tags — stamp first-seen."""
    base = "https://www.imc.com"
    url = f"{base}/us/blogs"
    source_name = "IMC Trading Blog"
    items = []
    try:
        soup = scrape_get(url)
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("/us/articles/") or href in seen:
                continue
            seen.add(href)
            heading = a.find("h3")
            title = heading.get_text(strip=True) if heading else a.get_text(strip=True)
            if not title:
                continue
            link = base + href
            published = first_seen_published(link, scrape_state)
            items.append({"title": title, "link": link, "source": source_name, "published": published})
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def fetch_xtx(scrape_state):
    """No publish dates on the page — stamp first-seen. Own /news/ posts only
    (the page also links out to third-party press coverage, which we skip)."""
    base = "https://www.xtxmarkets.com"
    url = f"{base}/news/"
    source_name = "XTX Markets News"
    items = []
    try:
        soup = scrape_get(url)
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("/news/") or href in seen:
                continue
            seen.add(href)
            title = a.get_text(strip=True)
            if not title:
                continue
            link = base + href
            published = first_seen_published(link, scrape_state)
            items.append({"title": title, "link": link, "source": source_name, "published": published})
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def main():
    scrape_state = load_scrape_state()

    all_items = []
    for url in FEEDS:
        all_items.extend(fetch_one(url))
    for feed in WORDPRESS_API_FEEDS:
        all_items.extend(fetch_wordpress_api(feed["url"], feed["source"]))
    all_items.extend(fetch_optiver())
    all_items.extend(fetch_aqr())
    all_items.extend(fetch_imc(scrape_state))
    all_items.extend(fetch_xtx(scrape_state))

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

    with open(SCRAPE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(scrape_state, f, indent=2)

    print(f"Wrote {len(all_items)} items to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

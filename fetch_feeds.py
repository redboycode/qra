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
    {"source": "Jane Street", "url": "https://blog.janestreet.com/feed.xml"},
    {"source": "Two Sigma", "url": "https://www.twosigma.com/articles/feed/"},
    {"source": "G-Research", "url": "https://www.gresearch.com/news/feed/"},
    {"source": "Tower Research", "url": "https://tower-research.com/feed/"},
]

# ---- Sources with no RSS/Atom feed but a usable JSON API instead ----
# HRT retired their RSS feed (hrtbeat/feed/ 301-redirects to the plain blog
# page), but the site is WordPress and still exposes its REST API with real
# publish dates, so we pull from there instead of scraping HTML.
WORDPRESS_API_FEEDS = [
    {
        "source": "HRT",
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
# A custom UA ("personal-rss-aggregator/1.0") got silently capped to ~2
# pages of real content by Tower Research's CDN before falling back to
# repeating page-1 — no error, just identical entries past that depth. A
# standard browser UA paginates correctly. This is fetching each site's own
# public RSS feed, nothing gated behind auth.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
MAX_ITEMS_PER_SOURCE = 200
MAX_BACKFILL_PAGES = 50


def parse_entry_date(entry):
    """Return an ISO timestamp, falling back gracefully if missing."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            dt = datetime.fromtimestamp(mktime(struct), tz=timezone.utc)
            return dt.isoformat()
    return datetime.now(tz=timezone.utc).isoformat()


def fetch_one(url, source_name, deep=False):
    """deep=True paginates via WordPress's ?paged=N convention until a page
    comes back with nothing new — used for one-time backfills. Regular runs
    only fetch page 1; accumulation in main() is what keeps older items
    around after that, not re-fetching deep every hour."""
    items = []
    seen_links = set()
    for page in range(1, MAX_BACKFILL_PAGES + 1) if deep else [1]:
        sep = "&" if "?" in url else "?"
        page_url = url if page == 1 else f"{url}{sep}paged={page}"
        try:
            resp = requests.get(
                page_url,
                timeout=TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
            if deep and resp.status_code == 404:
                break  # WordPress convention for "past the last page" on some sites
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                break
            new_on_page = 0
            for entry in parsed.entries:
                link = entry.get("link", url)
                if link in seen_links:
                    continue
                seen_links.add(link)
                new_on_page += 1
                items.append(
                    {
                        "title": entry.get("title", "Untitled"),
                        "link": link,
                        "source": source_name,
                        "published": parse_entry_date(entry),
                    }
                )
            if deep and new_on_page == 0:
                break  # site doesn't actually support ?paged= — stop instead of looping
        except Exception as exc:
            print(f"[WARN] failed to fetch {page_url}: {exc}", file=sys.stderr)
            break
    return items


def fetch_wordpress_api(url, source_name, deep=False):
    """Pull posts from a WordPress REST API endpoint (wp-json/wp/v2/posts).
    deep=True pages via ?page=N until the API 400s past the last page."""
    items = []
    for page in range(1, MAX_BACKFILL_PAGES + 1) if deep else [1]:
        sep = "&" if "?" in url else "?"
        page_url = url if page == 1 else f"{url}{sep}page={page}"
        try:
            resp = requests.get(
                page_url,
                timeout=TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code == 400:
                break  # past the last page
            resp.raise_for_status()
            posts = resp.json()
            if not posts:
                break
            for post in posts:
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
            print(f"[WARN] failed to fetch {page_url}: {exc}", file=sys.stderr)
            break
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
    resp = requests.get(url, timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def fetch_optiver():
    """Real per-post dates live in <time datetime="..."> inside each <li> card."""
    url = "https://www.optiver.com/insights/technology-blog/"
    source_name = "Optiver"
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
    source_name = "AQR"
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
    source_name = "IMC"
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


def fetch_man_group():
    """Real dates exist but only at month/year granularity (a <span> next to
    the title, e.g. "Jul 2026") — day is stamped as the 1st. Note this is
    Man Group's general insights page across all strategies (credit, ESG,
    podcasts, macro), not Man AHL specifically — AHL doesn't have its own
    separate, scrapable research page."""
    url = "https://www.man.com/insights"
    source_name = "Man Group"
    items = []
    try:
        soup = scrape_get(url)
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/insights/" not in href or href.rstrip("/").endswith("/insights") or href in seen:
                continue
            title_tag = a.find("h5")
            date_tag = a.find("span", class_=lambda c: c and "whitespace-nowrap" in c)
            if not (title_tag and date_tag):
                continue
            try:
                published = datetime.strptime(date_tag.get_text(strip=True), "%b %Y").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
            seen.add(href)
            link = href if href.startswith("http") else "https://www.man.com" + href
            items.append({"title": title_tag.get_text(strip=True), "link": link, "source": source_name, "published": published})
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def fetch_deshaw():
    """Real dates but year-only granularity (a <span class="year">) — day is
    stamped as Jan 1 of that year. Their library is a client-rendered React
    grid, but the full list is present in the initial HTML (no JS execution
    needed)."""
    base = "https://www.deshaw.com"
    url = f"{base}/library"
    source_name = "DE Shaw"
    items = []
    try:
        soup = scrape_get(url)
        for art in soup.find_all("article", class_="library-item"):
            year_tag = art.find("span", class_="year")
            title_tag = art.find("span", class_="accordionTitle")
            link_tag = art.find("a", href=lambda h: h and h.startswith("/library/"))
            if not (year_tag and title_tag and link_tag):
                continue
            title = title_tag.get_text(strip=True)
            try:
                published = datetime.strptime(year_tag.get_text(strip=True), "%Y").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
            link = base + link_tag["href"]
            items.append({"title": title, "link": link, "source": source_name, "published": published})
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def fetch_sig():
    """Real per-post dates in <time datetime="...">. Small, fairly new blog —
    low volume is expected, not a scraper bug."""
    base = "https://sig.com"
    url = f"{base}/waves/"
    source_name = "SIG"
    skip_titles = {"Categories", "Waves | Susquehanna's Technical Blog"}
    items = []
    try:
        soup = scrape_get(url)
        seen = set()
        for container in soup.find_all(["article", "section"]):
            hrefs = set(a["href"] for a in container.find_all("a", href=True) if "/waves/posts/" in a["href"])
            if len(hrefs) != 1:
                continue
            href = next(iter(hrefs))
            if href in seen:
                continue
            title_tag = container.find(["h1", "h2", "h3", "h4"])
            time_tag = container.find("time")
            if not (title_tag and time_tag and time_tag.get("datetime")):
                continue
            title = title_tag.get_text(strip=True)
            if title in skip_titles:
                continue
            seen.add(href)
            link = base + href
            published = datetime.fromisoformat(time_tag["datetime"]).replace(tzinfo=timezone.utc).isoformat()
            items.append({"title": title, "link": link, "source": source_name, "published": published})
    except Exception as exc:
        print(f"[WARN] failed to fetch {url}: {exc}", file=sys.stderr)
    return items


def fetch_xtx(scrape_state):
    """No publish dates on the page — stamp first-seen. Own /news/ posts only
    (the page also links out to third-party press coverage, which we skip)."""
    base = "https://www.xtxmarkets.com"
    url = f"{base}/news/"
    source_name = "XTX Markets"
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


def load_existing_items():
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except FileNotFoundError:
        return []


def merge_and_cap(existing_items, new_items, max_per_source=MAX_ITEMS_PER_SOURCE):
    """Accumulate rather than overwrite: feed.json is the union of everything
    ever fetched, deduped by link, capped per source (oldest dropped first)
    so the file and git history don't grow forever. Without this, an item
    that scrolls off a source's own front page/feed window would vanish from
    feed.json the next run even though nothing about it changed."""
    by_link = {}
    for item in existing_items:
        by_link[item["link"]] = item
    for item in new_items:
        by_link[item["link"]] = item

    by_source = {}
    for item in by_link.values():
        by_source.setdefault(item["source"], []).append(item)

    capped = []
    for items in by_source.values():
        items.sort(key=lambda x: x["published"], reverse=True)
        capped.extend(items[:max_per_source])

    capped.sort(key=lambda x: x["published"], reverse=True)
    return capped


def main():
    deep = "--backfill" in sys.argv
    scrape_state = load_scrape_state()

    all_items = []
    for feed in FEEDS:
        all_items.extend(fetch_one(feed["url"], feed["source"], deep=deep))
    for feed in WORDPRESS_API_FEEDS:
        all_items.extend(fetch_wordpress_api(feed["url"], feed["source"], deep=deep))
    all_items.extend(fetch_optiver())
    all_items.extend(fetch_aqr())
    all_items.extend(fetch_man_group())
    all_items.extend(fetch_deshaw())
    all_items.extend(fetch_sig())
    all_items.extend(fetch_imc(scrape_state))
    all_items.extend(fetch_xtx(scrape_state))

    merged_items = merge_and_cap(load_existing_items(), all_items)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "items": merged_items,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    with open(SCRAPE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(scrape_state, f, indent=2)

    print(f"Fetched {len(all_items)} items this run, {len(merged_items)} total in {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

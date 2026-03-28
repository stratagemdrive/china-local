"""
fetch_china_news.py
────────────────────────────────────────────────────────────────────────────────
Fetches RSS headlines from a curated set of China-focused English-language
sources, categorizes each story, optionally translates non-English text,
and maintains a rolling JSON file (docs/china_news.json) published via
GitHub Pages at:
    https://stratagemdrive.github.io/china-local/china_news.json

Rules:
  - 5 categories: Diplomacy, Military, Energy, Economy, Local Events
  - Target 20 stories per category (100 total max)
  - No story older than 7 days
  - When fewer than 20 new stories exist for a category, keep the newest
    existing ones, replacing only the oldest entries
  - All stories have: title, source, url, published_date, category
"""

import json
import os
import re
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from dateutil import parser as dateparser
from deep_translator import GoogleTranslator

# ── Configuration ─────────────────────────────────────────────────────────────

OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "china_news.json"

MAX_PER_CATEGORY = 20
MAX_AGE_DAYS = 7
CATEGORIES = ["Diplomacy", "Military", "Energy", "Economy", "Local Events"]

# RSS sources — all publish primarily in English
# chinaopensourceobservatory.org has no RSS feed → replaced with CGTN
# caixinglobal.com has no public RSS (paywalled) → replaced with China.org.cn
SOURCES = [
    {
        "name": "China Daily",
        "feeds": [
            "http://www.chinadaily.com.cn/rss/china_rss.xml",
            "http://www.chinadaily.com.cn/rss/bizchina_rss.xml",
            "http://www.chinadaily.com.cn/rss/world_rss.xml",
        ],
    },
    {
        "name": "People's Daily",
        "feeds": [
            "http://en.people.cn/rss/90001.xml",   # China
            "http://en.people.cn/rss/90777.xml",   # World
            "http://en.people.cn/rss/90778.xml",   # Business
        ],
    },
    {
        "name": "Xinhua",
        "feeds": [
            "http://www.xinhuanet.com/english/rss/chinalatestnews.xml",
            "http://www.xinhuanet.com/english/rss/worldnews.xml",
        ],
    },
    {
        "name": "Global Times",
        "feeds": [
            "https://www.globaltimes.cn/rss/outbrain.xml",
        ],
    },
    {
        "name": "Sixth Tone",
        "feeds": [
            "https://www.sixthtone.com/rss/index.xml",
        ],
    },
    {
        "name": "SCMP",
        "feeds": [
            "https://www.scmp.com/rss/91/feed",    # China
            "https://www.scmp.com/rss/2/feed",     # Business
            "https://www.scmp.com/rss/4/feed",     # Asia
        ],
    },
    {
        # Replaces chinaopensourceobservatory.org (no RSS)
        "name": "CGTN",
        "feeds": [
            "https://www.cgtn.com/subscribe/rss/section/china.xml",
            "https://www.cgtn.com/subscribe/rss/section/world.xml",
            "https://www.cgtn.com/subscribe/rss/section/business.xml",
        ],
    },
    {
        # Replaces Caixin Global (paywalled, no public RSS)
        "name": "China.org.cn",
        "feeds": [
            "http://china.org.cn/rss/1201719.xml",
        ],
    },
]

# ── Keyword-based categorisation ──────────────────────────────────────────────

CATEGORY_KEYWORDS = {
    "Military": [
        r"\bmilitary\b", r"\bdefense\b", r"\bdefence\b", r"\barmy\b",
        r"\bnavy\b", r"\bair force\b", r"\bpla\b", r"\bweapon\b",
        r"\bmissile\b", r"\bwarship\b", r"\bdrills?\b", r"\bexercise\b",
        r"\btroops?\b", r"\bsoldier\b", r"\bsecurity force\b",
        r"\bnuclear\b", r"\bcombat\b", r"\bwarfare\b", r"\bsouth china sea\b",
        r"\btaiwan strait\b", r"\barms\b",
    ],
    "Energy": [
        r"\benergy\b", r"\boil\b", r"\bgas\b", r"\bcoal\b",
        r"\bpetroleum\b", r"\brenewable\b", r"\bsolar\b", r"\bwind power\b",
        r"\bnuclear power\b", r"\bpower plant\b", r"\bgrid\b",
        r"\belectricity\b", r"\bhydropower\b", r"\bcarbon\b",
        r"\bclimate\b", r"\bemissions?\b", r"\brefinery\b",
        r"\blng\b", r"\bpipeline\b",
    ],
    "Diplomacy": [
        r"\bdiplomat\w*\b", r"\bforeign minister\b", r"\bforeign policy\b",
        r"\bembassy\b", r"\bconsulate\b", r"\bsummit\b", r"\bbilateral\b",
        r"\btreaty\b", r"\bsanction\b", r"\brelations\b",
        r"\bnegotiat\w*\b", r"\bagreement\b", r"\bunited nations\b",
        r"\bun\b", r"\bwto\b", r"\bimf\b", r"\bworldbank\b",
        r"\bbelt and road\b", r"\bbri\b", r"\basean\b", r"\bschengen\b",
        r"\bvisit\b.*\bofficial\b", r"\bforeign affairs\b",
    ],
    "Economy": [
        r"\beconom\w*\b", r"\bgdp\b", r"\btrade\b", r"\btariff\b",
        r"\bmarket\b", r"\bstock\b", r"\bcurrency\b", r"\byuan\b",
        r"\brenminbi\b", r"\bexport\b", r"\bimport\b", r"\binflation\b",
        r"\binterest rate\b", r"\bcentral bank\b", r"\bpboc\b",
        r"\bbank\b", r"\bfinance\b", r"\binvest\w*\b", r"\bmanufactur\w*\b",
        r"\bsupply chain\b", r"\btech\w*\b", r"\bstartup\b",
        r"\bipo\b", r"\bprofit\b", r"\bdebt\b", r"\bgrowth\b",
    ],
    "Local Events": [
        r"\bprovince\b", r"\bcity\b", r"\bmunicipality\b",
        r"\bearthquake\b", r"\bflood\b", r"\bdisaster\b",
        r"\bfestival\b", r"\bculture\b", r"\btourism\b",
        r"\beducation\b", r"\bhealth\b", r"\bhospital\b",
        r"\bcovid\b", r"\bpandemic\b", r"\bpollution\b",
        r"\bbeijing\b", r"\bshanghai\b", r"\bguangzhou\b",
        r"\bshenzhen\b", r"\bchengdu\b", r"\bwuhan\b",
        r"\bhong kong\b", r"\bmacao\b", r"\bxinjiang\b",
        r"\btibet\b", r"\binner mongolia\b",
    ],
}

# Compile all patterns once
_COMPILED_KEYWORDS: dict[str, list[re.Pattern]] = {
    cat: [re.compile(kw, re.IGNORECASE) for kw in kwlist]
    for cat, kwlist in CATEGORY_KEYWORDS.items()
}


def categorise(title: str, summary: str = "") -> str:
    """Return the best-matching category for a story, defaulting to 'Local Events'."""
    text = f"{title} {summary}"
    scores: dict[str, int] = {cat: 0 for cat in CATEGORIES}
    for cat, patterns in _COMPILED_KEYWORDS.items():
        for pattern in patterns:
            if pattern.search(text):
                scores[cat] += 1
    best = max(scores, key=lambda c: scores[c])
    return best if scores[best] > 0 else "Local Events"


# ── Translation helper ────────────────────────────────────────────────────────

def looks_chinese(text: str) -> bool:
    """Heuristic: does the string contain CJK characters?"""
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def translate_if_needed(text: str) -> str:
    """Translate text to English if it contains Chinese characters."""
    if not text or not looks_chinese(text):
        return text
    try:
        translated = GoogleTranslator(source="auto", target="en").translate(text)
        return translated or text
    except Exception:
        return text


# ── RSS fetching ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StratagemDrive-NewsBot/1.0; "
        "+https://stratagemdrive.github.io/china-local/)"
    )
}

CUTOFF = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)


def story_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def parse_date(entry) -> datetime | None:
    """Return a timezone-aware datetime from a feedparser entry, or None."""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                import time
                ts = time.mktime(val)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass
    for attr in ("published", "updated"):
        val = getattr(entry, attr, None)
        if val:
            try:
                dt = dateparser.parse(val)
                if dt:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
            except Exception:
                pass
    return None


def fetch_feed(url: str, source_name: str) -> list[dict]:
    """Fetch and parse a single RSS feed URL into a list of story dicts."""
    stories = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        print(f"  [WARN] Could not fetch {url}: {exc}")
        return stories

    for entry in feed.entries:
        link = getattr(entry, "link", "") or ""
        if not link:
            continue

        pub_dt = parse_date(entry)
        if pub_dt and pub_dt < CUTOFF:
            continue  # too old

        raw_title = getattr(entry, "title", "") or ""
        raw_summary = getattr(entry, "summary", "") or ""

        title = translate_if_needed(raw_title.strip())
        summary = translate_if_needed(raw_summary.strip())

        if not title:
            continue

        published_date = pub_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if pub_dt else ""

        stories.append(
            {
                "id": story_id(link),
                "title": title,
                "source": source_name,
                "url": link,
                "published_date": published_date,
                "category": categorise(title, summary),
            }
        )

    return stories


# ── JSON management ───────────────────────────────────────────────────────────

def load_existing() -> dict[str, list[dict]]:
    """Load the current JSON grouped by category, or return empty buckets."""
    if OUTPUT_FILE.exists():
        try:
            with OUTPUT_FILE.open() as f:
                data = json.load(f)
            if isinstance(data, list):
                # Legacy flat list — migrate to bucketed form
                bucketed: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}
                for story in data:
                    cat = story.get("category", "Local Events")
                    if cat in bucketed:
                        bucketed[cat].append(story)
                return bucketed
            if isinstance(data, dict):
                return {cat: data.get(cat, []) for cat in CATEGORIES}
        except Exception:
            pass
    return {cat: [] for cat in CATEGORIES}


def merge_stories(
    existing: dict[str, list[dict]],
    incoming: list[dict],
) -> dict[str, list[dict]]:
    """
    Merge new stories into existing buckets, enforcing:
      - No duplicates (by id / url)
      - No stories older than MAX_AGE_DAYS
      - At most MAX_PER_CATEGORY per bucket (oldest replaced first)
    """
    # Remove stale entries from existing
    for cat in CATEGORIES:
        existing[cat] = [
            s for s in existing[cat]
            if s.get("published_date", "") >= CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ")
        ]

    # Build a set of known story ids to avoid duplicates
    known_ids: set[str] = set()
    for cat in CATEGORIES:
        for s in existing[cat]:
            known_ids.add(s.get("id") or story_id(s.get("url", "")))

    # Add new stories
    for story in incoming:
        sid = story.get("id") or story_id(story.get("url", ""))
        if sid in known_ids:
            continue
        cat = story["category"]
        existing[cat].append(story)
        known_ids.add(sid)

    # Trim each bucket: sort by date descending, keep newest MAX_PER_CATEGORY
    for cat in CATEGORIES:
        existing[cat].sort(
            key=lambda s: s.get("published_date", ""),
            reverse=True,
        )
        existing[cat] = existing[cat][:MAX_PER_CATEGORY]

    return existing


def save(bucketed: dict[str, list[dict]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Flatten to a list for the final JSON (drop internal 'id' field)
    flat = []
    for cat in CATEGORIES:
        for story in bucketed[cat]:
            out = {k: v for k, v in story.items() if k != "id"}
            flat.append(out)
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(flat, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(flat)} stories → {OUTPUT_FILE}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Run started at {datetime.now(timezone.utc).isoformat()}")

    existing = load_existing()
    all_incoming: list[dict] = []

    for source in SOURCES:
        print(f"Fetching from: {source['name']}")
        for feed_url in source["feeds"]:
            stories = fetch_feed(feed_url, source["name"])
            print(f"  {feed_url} → {len(stories)} stories")
            all_incoming.extend(stories)

    print(f"\nTotal incoming stories: {len(all_incoming)}")

    merged = merge_stories(existing, all_incoming)

    for cat in CATEGORIES:
        print(f"  {cat}: {len(merged[cat])} stories")

    save(merged)


if __name__ == "__main__":
    main()

"""Social presence detection — scrapes the brand's own website for social links,
then attempts to fetch follower counts from the discovered profiles.

Primary source: brand website footer / about page (most reliable).
Fallback: web mention URLs and handle guesses.
"""

from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

logger = logging.getLogger("apps")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

PLATFORMS = ["instagram", "facebook", "youtube", "twitter", "linkedin"]

# Regex patterns to find social links in brand website HTML
SOCIAL_LINK_PATTERNS: dict[str, list[re.Pattern]] = {
    "instagram": [
        re.compile(r'href=["\']([^"\']*instagram\.com/[^"\'?#]+)', re.I),
    ],
    "facebook": [
        re.compile(r'href=["\']([^"\']*facebook\.com/[^"\'?#]+)', re.I),
    ],
    "youtube": [
        re.compile(r'href=["\']([^"\']*youtube\.com/(?:@|c/|channel/|user/)[^"\'?#]+)', re.I),
        re.compile(r'href=["\']([^"\']*youtube\.com/[^"\'?#]+)', re.I),
    ],
    "twitter": [
        re.compile(r'href=["\']([^"\']*(?:twitter|x)\.com/[^"\'?#]+)', re.I),
    ],
    "linkedin": [
        re.compile(r'href=["\']([^"\']*linkedin\.com/(?:company|in)/[^"\'?#]+)', re.I),
    ],
}

# Paths on the brand's site where social links are commonly found
SOCIAL_PAGES = ["", "/about", "/about-us", "/contact", "/contact-us"]


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


# ── Scrape social links from brand website ───────────────────────────────

def _extract_social_links_from_website(session: requests.Session, brand_url: str) -> dict[str, str | None]:
    """Crawl brand homepage + about/contact pages for social media links."""
    urls_found: dict[str, str | None] = {p: None for p in PLATFORMS}

    parsed = urlparse(brand_url if "://" in brand_url else "https://" + brand_url)
    base = f"{parsed.scheme or 'https'}://{parsed.hostname}"

    for page_path in SOCIAL_PAGES:
        page_url = base + page_path
        try:
            r = session.get(page_url, timeout=10, allow_redirects=True)
            if r.status_code != 200:
                continue
            html = r.text or ""

            for platform, patterns in SOCIAL_LINK_PATTERNS.items():
                if urls_found[platform]:
                    continue
                for pat in patterns:
                    m = pat.search(html)
                    if m:
                        raw_url = m.group(1).strip()
                        # Skip share/intent links
                        if any(skip in raw_url.lower() for skip in (
                            "intent/", "sharer", "share?", "/share",
                            "hashtag/", "/explore", "/search",
                        )):
                            continue
                        normalizer = NORMALIZERS.get(platform)
                        urls_found[platform] = normalizer(raw_url) if normalizer else raw_url
                        break

            if all(v is not None for v in urls_found.values()):
                break
        except Exception as exc:
            logger.debug("social link crawl %s: %s", page_url, exc)
            continue

    return urls_found


# ── URL discovery from web mentions (fallback) ───────────────────────────

def _extract_profile_urls_from_mentions(web_details: dict) -> dict[str, str | None]:
    urls: dict[str, str | None] = {p: None for p in PLATFORMS}
    mentions = web_details.get("mentions") or []
    if not isinstance(mentions, list):
        return urls
    for m in mentions:
        if not isinstance(m, dict):
            continue
        url = str(m.get("url") or "").strip()
        if not url:
            continue
        low = url.lower()
        if "instagram.com/" in low and urls["instagram"] is None:
            urls["instagram"] = _normalize_instagram_url(url)
        if "facebook.com/" in low and urls["facebook"] is None:
            urls["facebook"] = _normalize_facebook_url(url)
        if "youtube.com/" in low and urls["youtube"] is None:
            urls["youtube"] = _normalize_youtube_url(url)
        if ("twitter.com/" in low or "x.com/" in low) and urls["twitter"] is None:
            urls["twitter"] = _normalize_twitter_url(url)
        if "linkedin.com/" in low and urls["linkedin"] is None:
            urls["linkedin"] = _normalize_linkedin_url(url)
        if all(v is not None for v in urls.values()):
            break
    return urls


# ── URL normalizers ──────────────────────────────────────────────────────

def _normalize_instagram_url(url: str) -> str:
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        path = (p.path or "").strip("/").split("/")
        if not path or not path[0]:
            return url
        user = path[0]
        if user in ("p", "reel", "reels", "stories", "explore"):
            return url
        return f"https://www.instagram.com/{user}/"
    except Exception:
        return url


def _normalize_facebook_url(url: str) -> str:
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        return f"{p.scheme}://{p.netloc}{p.path.split('?')[0]}"
    except Exception:
        return url


def _normalize_youtube_url(url: str) -> str:
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        path = (p.path or "").strip("/").split("/")
        if not path or not path[0]:
            return url
        first = path[0]
        if first.startswith("@"):
            return f"https://www.youtube.com/{first}"
        if first in ("c", "channel", "user") and len(path) > 1:
            return f"https://www.youtube.com/{first}/{path[1]}"
        if first in ("watch", "shorts", "playlist", "feed"):
            return url
        return f"https://www.youtube.com/@{first}"
    except Exception:
        return url


def _normalize_twitter_url(url: str) -> str:
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        path = (p.path or "").strip("/").split("/")
        if not path or not path[0]:
            return url
        user = path[0]
        if user in ("i", "search", "hashtag", "explore", "home"):
            return url
        return f"https://x.com/{user}"
    except Exception:
        return url


def _normalize_linkedin_url(url: str) -> str:
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        return f"https://www.linkedin.com{p.path.split('?')[0].rstrip('/')}/"
    except Exception:
        return url


NORMALIZERS = {
    "instagram": _normalize_instagram_url,
    "facebook": _normalize_facebook_url,
    "youtube": _normalize_youtube_url,
    "twitter": _normalize_twitter_url,
    "linkedin": _normalize_linkedin_url,
}


# ── Follower parsers ─────────────────────────────────────────────────────

def _parse_instagram_followers(html: str) -> int | None:
    for pattern in (
        r'"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)',
        r'"follower_count"\s*:\s*(\d+)',
        r'"edge_followed_by":\{"count":(\d+)\}',
    ):
        m = re.search(pattern, html)
        if m:
            return int(m.group(1))
    return None


def _parse_facebook_followers(html: str) -> int | None:
    for pattern in (
        r'"followers_count"\s*:\s*(\d+)',
        r'"follower_count"\s*:\s*(\d+)',
        r'(\d[\d,]*)\s+followers',
    ):
        m = re.search(pattern, html, re.I)
        if m:
            raw = m.group(1).replace(",", "")
            if raw.isdigit():
                return int(raw)
    return None


def _parse_youtube_subscribers(html: str) -> int | None:
    for pattern in (
        r'"subscriberCountText":\{"simpleText":"([\d.]+[KMBkmb]?)\s*subscribers?"',
        r'"subscriberCountText":\{[^}]*"content":"([\d.]+[KMBkmb]?)\s*subscribers?"',
        r'(\d[\d,.]*)\s*subscribers?',
    ):
        m = re.search(pattern, html, re.I)
        if m:
            return _parse_human_number(m.group(1))
    return None


def _parse_twitter_followers(html: str) -> int | None:
    for pattern in (
        r'"followers_count"\s*:\s*(\d+)',
        r'"followersCount"\s*:\s*(\d+)',
        r'(\d[\d,]*)\s+Followers',
    ):
        m = re.search(pattern, html, re.I)
        if m:
            raw = m.group(1).replace(",", "")
            if raw.isdigit():
                return int(raw)
    return None


def _parse_linkedin_followers(html: str) -> int | None:
    for pattern in (
        r'(\d[\d,]*)\s+followers',
        r'"followersCount"\s*:\s*(\d+)',
    ):
        m = re.search(pattern, html, re.I)
        if m:
            raw = m.group(1).replace(",", "")
            if raw.isdigit():
                return int(raw)
    return None


def _parse_human_number(s: str) -> int | None:
    s = s.strip().replace(",", "")
    multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    if s and s[-1].lower() in multipliers:
        try:
            return int(float(s[:-1]) * multipliers[s[-1].lower()])
        except ValueError:
            return None
    try:
        return int(float(s))
    except ValueError:
        return None


PARSER_MAP = {
    "instagram": _parse_instagram_followers,
    "facebook": _parse_facebook_followers,
    "youtube": _parse_youtube_subscribers,
    "twitter": _parse_twitter_followers,
    "linkedin": _parse_linkedin_followers,
}


# ── Fetch logic ──────────────────────────────────────────────────────────

def _fetch_followers(session: requests.Session, url: str, platform: str) -> tuple[int | None, str | None]:
    if not url:
        return None, "no_url"
    try:
        r = session.get(url, timeout=12, allow_redirects=True)
        if r.status_code != 200:
            return None, f"http_{r.status_code}"
        html = r.text or ""
        parser = PARSER_MAP.get(platform)
        n = parser(html) if parser else None
        if n is None and ("login" in html.lower() or "Log in" in html):
            return None, "login_wall"
        return n, None if n is not None else "not_found_in_html"
    except Exception as exc:
        logger.debug("social fetch %s: %s", url, exc)
        return None, "fetch_error"


# ── Scoring ──────────────────────────────────────────────────────────────

def _platform_slice(url: str | None, followers: int | None, from_website: bool) -> float:
    if not url:
        return 0.0
    n = followers if followers and followers > 0 else None
    if n:
        return 28 + min(22, 22 * math.log10(n + 1) / math.log10(100_001))
    if from_website:
        return 15.0  # confirmed link on brand site is strong signal
    return 5.0


def _score_presence(platform_data: dict) -> float:
    total = 0.0
    for plat in PLATFORMS:
        data = platform_data.get(plat, {})
        total += _platform_slice(
            data.get("url"),
            data.get("followers"),
            data.get("source") == "website",
        )
    return round(min(100, total), 1)


def _score_market_capture(platform_data: dict) -> float:
    total_followers = sum(
        (platform_data.get(p, {}).get("followers") or 0) for p in PLATFORMS
    )
    if total_followers <= 0:
        return 0.0
    cap = 100 * math.log10(total_followers + 1) / math.log10(1_000_001)
    return round(min(100, max(0, cap)), 1)


# ── Main entry point ─────────────────────────────────────────────────────

def run_social_presence(
    brand_name: str,
    brand_url: str,
    web_mentions_details: dict,
) -> dict:
    session = _session()

    # 1) Primary: scrape brand's own website for social links
    website_urls = _extract_social_links_from_website(session, brand_url)

    # 2) Fallback: web mention URLs
    mention_urls = _extract_profile_urls_from_mentions(web_mentions_details)

    # Merge: website > mentions > (no guess — only real links)
    final_urls: dict[str, str | None] = {}
    source: dict[str, str] = {}
    for plat in PLATFORMS:
        if website_urls.get(plat):
            final_urls[plat] = website_urls[plat]
            source[plat] = "website"
        elif mention_urls.get(plat):
            final_urls[plat] = mention_urls[plat]
            source[plat] = "mention"
        else:
            final_urls[plat] = None
            source[plat] = "none"

    # 3) Try to fetch follower counts in parallel
    platform_data: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for plat in PLATFORMS:
            futures[plat] = executor.submit(
                _fetch_followers, session, final_urls.get(plat) or "", plat
            )

        for plat in PLATFORMS:
            followers, err = futures[plat].result()
            platform_data[plat] = {
                "url": final_urls.get(plat),
                "followers": followers,
                "error": err,
                "source": source[plat],
            }

    brand_presence = _score_presence(platform_data)
    market_capture = _score_market_capture(platform_data)

    platforms_linked = sum(1 for p in PLATFORMS if final_urls.get(p))

    return {
        **platform_data,
        "brand_presence_score": brand_presence,
        "market_capture_score": market_capture,
        "platforms_linked": platforms_linked,
        "method": "website_crawl",
        "interpretation": (
            f"Found social links on {platforms_linked} of {len(PLATFORMS)} platforms. "
            "Links discovered from the brand's own website are the strongest signal."
        ),
    }

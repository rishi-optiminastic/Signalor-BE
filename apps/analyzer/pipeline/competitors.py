import json
import logging
import re

import requests

from .crawler import CrawlResult, crawl_page
from .content import score_content
from .schema import score_schema
from .eeat import score_eeat
from .technical import score_technical
from .aggregator import compute_static_composite
from .utils import extract_brand_name

logger = logging.getLogger("apps")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _discover_competitors_llm(brand_name: str, site_context: str) -> list[dict]:
    """Use LLM (via OpenRouter) to discover competitors."""
    try:
        from .llm import ask_llm

        prompt = (
            f"Identify 5-8 competitors for '{brand_name}'. "
            f"Site context: {site_context}\n\n"
            f"Reply ONLY with a JSON array of objects with 'name', 'url', and 'industry' fields. "
            f"The URL should be the homepage. Example:\n"
            f'[{{"name": "Competitor", "url": "https://competitor.com", "industry": "SaaS"}}]'
        )
        text = ask_llm(prompt, preferred_provider="gemini", max_tokens=1024, purpose="Competitor Discovery")

        # Extract JSON array
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return [
                {
                    "name": item.get("name", ""),
                    "url": item.get("url", ""),
                    "industry": item.get("industry", ""),
                }
                for item in data
                if item.get("name") and item.get("url")
            ]
    except Exception as exc:
        logger.warning("Competitor discovery failed: %s", exc)
    return []


def _clean_site_context(text: str) -> str:
    """Keep context readable for LLM and safe for logs."""
    compact = re.sub(r"\s+", " ", text or "").strip()
    # Drop control chars and keep standard printable range.
    compact = re.sub(r"[^\x20-\x7E]", " ", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact[:800]


def _validate_url(url: str) -> bool:
    """Validate a URL is reachable."""
    try:
        resp = requests.head(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=3,
            allow_redirects=True,
        )
        return resp.status_code < 400
    except requests.RequestException:
        return False


def discover_competitors(crawl: CrawlResult) -> list[dict]:
    if not crawl.ok:
        return []

    soup = crawl.soup
    brand_name = extract_brand_name(soup, crawl.url)

    # Build site context
    title = soup.find("title")
    meta_desc = soup.find("meta", attrs={"name": "description"})
    h1 = soup.find("h1")
    site_context = " | ".join(
        filter(None, [
            title.string.strip() if title and title.string else "",
            meta_desc.get("content", "").strip() if meta_desc else "",
            h1.get_text(strip=True) if h1 else "",
            crawl.text[:300],
        ])
    )
    site_context = _clean_site_context(site_context)

    competitors = _discover_competitors_llm(brand_name, site_context)

    # Validate URLs
    validated = []
    for comp in competitors:
        if _validate_url(comp["url"]):
            validated.append(comp)
        elif comp["url"].startswith("http://"):
            https_url = comp["url"].replace("http://", "https://", 1)
            if _validate_url(https_url):
                comp["url"] = https_url
                validated.append(comp)

    return validated[:8]  # Cap at 8


def score_competitor(url: str) -> tuple[dict | None, float]:
    """Crawl and score a competitor using static-only pillars. Returns (page_score_data, composite)."""
    crawl = crawl_page(url)
    if not crawl.ok:
        return None, 0.0

    content_score, content_details = score_content(crawl)
    schema_score_val, schema_details = score_schema(crawl)
    eeat_score_val, eeat_details = score_eeat(crawl)
    technical_score_val, technical_details = score_technical(crawl)

    composite = compute_static_composite(
        content_score, schema_score_val, eeat_score_val, technical_score_val
    )

    page_data = {
        "url": url,
        "content_score": content_score,
        "content_details": content_details,
        "schema_score": schema_score_val,
        "schema_details": schema_details,
        "eeat_score": eeat_score_val,
        "eeat_details": eeat_details,
        "technical_score": technical_score_val,
        "technical_details": technical_details,
        "composite_score": composite,
    }

    return page_data, composite

"""Thin client for the spider.cloud hosted crawl API.

Point spider.cloud at a URL and it recursively discovers pages on the domain,
returning each page's content. We request ``return_format="raw"`` so the
response is the unmodified HTML — the analyzer pipeline parses that HTML with
BeautifulSoup and extracts JSON-LD / schema, so any "cleaned" format would drop
the ``<script type="application/ld+json">`` blocks the schema scorer needs.

The API key is read from the ``SPIDER_API_KEY`` environment variable (managed
outside the codebase). When it is unset, ``is_configured()`` returns False and
callers fall back to the direct crawler.
"""

import logging
import os

import requests

logger = logging.getLogger("apps")

CRAWL_ENDPOINT = "https://api.spider.cloud/crawl"
# Site crawls (multiple pages) take longer than a single request, but must stay
# well under the analysis task budget so a slow crawl degrades to the fallback.
DEFAULT_TIMEOUT = 90


class SpiderError(Exception):
    """Raised when a spider.cloud crawl cannot be completed."""


def _api_key() -> str:
    return os.getenv("SPIDER_API_KEY", "").strip()


def is_configured() -> bool:
    """True when a spider.cloud API key is present in the environment."""
    return bool(_api_key())


def crawl(
    url: str,
    limit: int = 15,
    return_format: str = "raw",
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Crawl ``url`` (and discovered pages) via spider.cloud.

    Returns a list of page dicts, each shaped like
    ``{"url": str, "content": str, "status": int, "error": ... }``.

    Raises :class:`SpiderError` on any failure (not configured, transport error,
    non-200, or an unexpected payload) so the caller can fall back cleanly.
    """
    key = _api_key()
    if not key:
        raise SpiderError("SPIDER_API_KEY not configured")

    payload = {
        "url": url,
        "limit": max(1, int(limit)),
        "return_format": return_format,
    }

    try:
        resp = requests.post(
            CRAWL_ENDPOINT,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SpiderError(f"spider.cloud request failed: {exc}") from exc

    if resp.status_code != 200:
        raise SpiderError(f"spider.cloud HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise SpiderError(f"spider.cloud returned non-JSON response: {exc}") from exc

    # Success responses are an array of page objects; error responses are a dict.
    if isinstance(data, dict):
        raise SpiderError(f"spider.cloud error response: {str(data)[:300]}")
    if not isinstance(data, list):
        raise SpiderError(f"spider.cloud unexpected response type: {type(data).__name__}")

    return data

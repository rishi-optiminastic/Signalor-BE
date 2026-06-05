"""Scraping-API fallback for the crawler.

When a direct ``requests`` crawl is hard-blocked (typically HTTP 403 from a
Cloudflare/WAF in front of a site, triggered by our datacenter egress IPs), we
re-fetch the same URL through a third-party scraping API that egresses from
residential IPs. The fallback is OFF until ``SCRAPER_API_KEY`` is set, so default
crawl behavior is unchanged.

Stealth mode (``SCRAPER_STEALTH``, default on): a plain residential GET clears
IP-reputation 403s but NOT a Cloudflare *managed challenge* / Turnstile
interstitial (the "Just a moment…" page). Stealth routes through the provider's
anti-bot proxy with JS rendering (ScrapingBee ``stealth_proxy``, ScraperAPI
``ultra_premium``) so the challenge is solved server-side. Because the fallback
only fires on a confirmed block, escalating straight to stealth is the right
trade-off (it costs more provider credits, but only on sites that need it).

Challenge guard: providers may still hand back the interstitial HTML with a 200.
``_is_challenge_page`` rejects those so we never parse/score a challenge page as
if it were real content.

Mirrors the credential/exception pattern in
``apps/integrations/services/dataforseo.py`` and reuses
``apps/integrations/_http.request_with_retry`` for transport retries.

Providers (``SCRAPER_API_PROVIDER``):
  - ``scrapingbee`` (default): https://app.scrapingbee.com/api/v1/
  - ``scraperapi``:            https://api.scraperapi.com/
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.conf import settings

from apps.integrations._http import request_with_retry

logger = logging.getLogger("apps")

# Scraping APIs route through residential proxies (and optionally render JS), so
# they are slow — give them a generous timeout.
TIMEOUT_SECONDS = 70


class ScraperNotConfigured(RuntimeError):
    """Raised when SCRAPER_API_KEY is not set — fallback is disabled."""


class ScraperError(RuntimeError):
    """Raised when the scraping API call fails, or returns non-200 / empty body."""


def is_configured() -> bool:
    """True when a scraping-API key is configured (i.e. the fallback is active)."""
    return bool(getattr(settings, "SCRAPER_API_KEY", "") or "")


# Strict markers for a Cloudflare managed-challenge / Turnstile interstitial. These
# only appear on the "Just a moment…" page itself — NOT on a legit page that merely
# embeds the /cdn-cgi/challenge-platform/ bot-detection script (e.g. signalor.ai's
# real 200), so matching any of these means the body is a challenge, not content.
_CHALLENGE_MARKERS = (
    "window._cf_chl_opt",
    "cf-chl-bypass",
    "<title>just a moment",
    "attention required! | cloudflare",
    "checking if the site connection is secure",
    "enable javascript and cookies to continue",
)


def _is_challenge_page(html: str) -> bool:
    """True if ``html`` is a Cloudflare challenge interstitial rather than content."""
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _build_request(url: str, render_js: bool, stealth: bool) -> tuple[str, dict]:
    """Return (endpoint, query_params) for the configured provider.

    When ``stealth`` is set, route through the provider's anti-bot proxy with JS
    rendering so Cloudflare managed challenges / Turnstile are solved server-side
    (ScrapingBee ``stealth_proxy``, ScraperAPI ``ultra_premium``). Stealth implies
    JS render, so ``render_js`` is forced on by the caller in that case.
    """
    api_key = getattr(settings, "SCRAPER_API_KEY", "") or ""
    if not api_key:
        raise ScraperNotConfigured("SCRAPER_API_KEY is not set.")

    provider = (getattr(settings, "SCRAPER_API_PROVIDER", "") or "scrapingbee").strip().lower()
    if provider == "scraperapi":
        params = {"api_key": api_key, "url": url, "render": "true" if render_js else "false"}
        if stealth:
            params["ultra_premium"] = "true"
        return "https://api.scraperapi.com/", params
    if provider == "scrapingbee":
        params = {"api_key": api_key, "url": url, "render_js": "true" if render_js else "false"}
        if stealth:
            params["stealth_proxy"] = "true"
        return "https://app.scrapingbee.com/api/v1/", params
    raise ScraperError(f"Unsupported SCRAPER_API_PROVIDER: {provider!r}")


def fetch_via_scraper(
    url: str,
    *,
    render_js: bool | None = None,
    stealth: bool | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """Re-fetch ``url`` through the configured scraping API.

    Returns ``(status_code, html)`` where a 200 means the target page was served.
    Raises :class:`ScraperNotConfigured` when no key is set, or
    :class:`ScraperError` on transport failure / non-200 / empty body / a Cloudflare
    challenge interstitial (so the caller never scores a "Just a moment…" page).

    ``stealth`` defaults from ``SCRAPER_STEALTH`` (on) and forces JS rendering, since
    the provider's anti-bot proxy needs to run the challenge.
    """
    if stealth is None:
        stealth = bool(getattr(settings, "SCRAPER_STEALTH", True))
    if render_js is None:
        render_js = bool(getattr(settings, "SCRAPER_RENDER_JS", False))
    if stealth:
        render_js = True  # stealth proxies must render JS to clear the challenge

    endpoint, params = _build_request(url, render_js, stealth)
    provider = getattr(settings, "SCRAPER_API_PROVIDER", "scrapingbee")
    # Never log the assembled URL — it embeds the API key. Log only the target.
    logger.info(
        "scraper fallback: fetching %s via %s (render_js=%s stealth=%s)",
        url,
        provider,
        render_js,
        stealth,
    )

    full_url = f"{endpoint}?{urlencode(params)}"
    try:
        resp = request_with_retry("GET", full_url, timeout=timeout, max_retries=2)
    except Exception as exc:  # noqa: BLE001 — requests.RequestException & friends
        raise ScraperError(f"scraper transport error for {url}: {exc}") from exc

    html = resp.text or ""
    if resp.status_code != 200 or not html.strip():
        raise ScraperError(f"scraper returned {resp.status_code} / empty body for {url}")
    if _is_challenge_page(html):
        raise ScraperError(f"scraper returned a Cloudflare challenge page for {url}")
    return 200, html

"""Shopify theme-asset editing.

Why: a Shopify storefront's visible content is split between two surfaces:
  - Page/Product/Blog `body_html` (editable via the standard Pages/Products
    Admin API) — covered elsewhere in this codebase.
  - Theme content (sections/*.json, templates/*.json, *.liquid) — settings,
    hero text, section blocks. The Pages API can't touch this; only the
    Theme Assets API can. Most homepage / nav / footer / hero text lives
    here.

This module bridges the gap. Given a connected Shopify Integration and a
piece of text the merchant wants to edit, it locates the asset that
contains that text and updates it.

v1 scope:
  - Active (published) theme only.
  - sections/*.json + templates/*.json + *.liquid assets.
  - First-match replacement (same semantics as the existing body_html
    replace; merchants almost never have the exact same string in two
    different sections).
  - No locale-string indirection — translation keys (`t:...`) are passed
    through unchanged; a future iteration can resolve them via locales/.

Public entrypoint:
    find_and_replace_text(integration, needle, replacement)
        -> {"ok": True, "asset_key": str, "preview": str} on success
        -> {"ok": False, "reason": str} on failure
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from .shopify import API_VERSION, normalize_shop_domain

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20

# Asset key prefixes we consider for text editing. Order matters —
# `templates/` first because templates layout per-page sections AND
# override section settings, so when the same text appears in both a
# template and a section, the template is the per-page authority that
# the merchant sees. Then sections (defaults), then snippets/config.
# `locales/` is the **last** fallback for translation-keyed strings
# (`{{ 'key' | t }}` in Liquid or `"heading": "t:..."` in section
# settings): if the visible text isn't a hardcoded value anywhere, it
# came from the default locale's strings file. We restrict to the
# default locale (filename ends `.default.json`) so we never edit
# translations into other languages by accident.
_CANDIDATE_PREFIXES = ("templates/", "sections/", "snippets/", "config/", "locales/")


def _template_key_for_url(url: str) -> str | None:
    """Infer the Shopify template file for a given storefront URL. Best-effort
    so callers can hint the search at the most-likely asset.

        /                       → templates/index.json
        /products/<handle>      → templates/product.json
        /collections/<handle>   → templates/collection.json
        /pages/<handle>         → templates/page.json
        /blogs/<blog>/<handle>  → templates/article.json
        /cart                   → templates/cart.json
        /search                 → templates/search.json

    Returns None if no template can be confidently inferred.
    """
    if not url:
        return None
    try:
        from urllib.parse import urlparse

        path = urlparse(url).path or "/"
    except Exception:
        return None
    if path == "/" or path == "":
        return "templates/index.json"
    if path.startswith("/products/"):
        return "templates/product.json"
    if path.startswith("/collections/"):
        return "templates/collection.json"
    if path.startswith("/pages/"):
        return "templates/page.json"
    if path.startswith("/blogs/") and path.count("/") >= 3:
        return "templates/article.json"
    if path == "/cart" or path.startswith("/cart"):
        return "templates/cart.json"
    if path == "/search" or path.startswith("/search"):
        return "templates/search.json"
    return None


class ThemeEditError(Exception):
    """Raised when a theme edit can't proceed for a recoverable reason
    (no active theme, unreachable shop, etc.). Surfaceable to the client."""


# ─── Low-level Asset API ────────────────────────────────────────────────────


def _shop_url(integration) -> str:
    md = integration.metadata or {}
    shop_domain = md.get("shop_domain", "")
    if not shop_domain:
        raise ThemeEditError("Shopify integration is missing shop_domain metadata.")
    return normalize_shop_domain(shop_domain)


def _headers(integration) -> dict[str, str]:
    token = integration.get_access_token()
    if not token:
        raise ThemeEditError("Shopify integration has no access token.")
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


def _get(integration, path: str, params: dict[str, Any] | None = None) -> dict:
    shop = _shop_url(integration)
    url = f"https://{shop}/admin/api/{API_VERSION}{path}"
    try:
        resp = requests.get(url, headers=_headers(integration), params=params, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ThemeEditError(f"Couldn't reach Shopify: {exc}") from exc
    if resp.status_code == 401:
        raise ThemeEditError("Shopify access token rejected. Reconnect the store.")
    if resp.status_code == 404:
        raise ThemeEditError("Shopify asset not found.")
    if resp.status_code >= 400:
        raise ThemeEditError(f"Shopify Admin API returned HTTP {resp.status_code}.")
    return resp.json()


def _put(integration, path: str, payload: dict) -> dict:
    shop = _shop_url(integration)
    url = f"https://{shop}/admin/api/{API_VERSION}{path}"
    try:
        resp = requests.put(
            url, headers=_headers(integration), data=json.dumps(payload), timeout=TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise ThemeEditError(f"Couldn't reach Shopify: {exc}") from exc
    if resp.status_code == 401:
        raise ThemeEditError("Shopify access token rejected. Reconnect the store.")
    if resp.status_code == 403:
        raise ThemeEditError(
            "Shopify rejected the asset write. The store may have theme editing "
            "locked or the granted scopes don't include write_themes."
        )
    if resp.status_code >= 400:
        raise ThemeEditError(f"Shopify Admin API returned HTTP {resp.status_code}.")
    return resp.json()


def _get_active_theme_id(integration) -> int:
    """Return the published theme's numeric id."""
    data = _get(integration, "/themes.json")
    themes = data.get("themes", []) or []
    for t in themes:
        if t.get("role") == "main":
            return int(t["id"])
    raise ThemeEditError("No main theme found on this Shopify store.")


def _list_asset_keys(integration, theme_id: int) -> list[str]:
    """List asset keys we consider for text editing. The Assets endpoint
    returns metadata only (no `value`), so this is cheap."""
    data = _get(integration, f"/themes/{theme_id}/assets.json")
    assets = data.get("assets", []) or []
    keys: list[str] = []
    for a in assets:
        k = a.get("key", "")
        if not k:
            continue
        if not k.startswith(_CANDIDATE_PREFIXES):
            continue
        # Editable text formats only — ignore binary images / fonts.
        if not k.endswith((".json", ".liquid", ".js")):
            continue
        # locales/ has many language variants; only edit the shop's
        # default-locale strings file to avoid silently translating into
        # other languages. The default locale's filename ends in
        # `.default.json` (e.g. `locales/en.default.json`,
        # `locales/fr.default.json`).
        if k.startswith("locales/") and not k.endswith(".default.json"):
            continue
        # Schema translations (locales/en.default.schema.json) drive the
        # Theme Editor's UI labels — editing them changes admin labels,
        # not storefront content. Skip.
        if k.startswith("locales/") and ".schema." in k:
            continue
        keys.append(k)
    # Order by prefix priority defined in _CANDIDATE_PREFIXES.
    keys.sort(
        key=lambda k: (
            _CANDIDATE_PREFIXES.index(next(p for p in _CANDIDATE_PREFIXES if k.startswith(p))),
            k,
        )
    )
    return keys


def _read_asset(integration, theme_id: int, key: str) -> str:
    """Fetch the value of one asset. Returns empty string on missing
    `value` (Shopify returns null for binary assets)."""
    data = _get(integration, f"/themes/{theme_id}/assets.json", params={"asset[key]": key})
    asset = data.get("asset") or {}
    return asset.get("value") or ""


def _write_asset(integration, theme_id: int, key: str, value: str) -> None:
    """Write an asset value back. Shopify versions assets internally; this
    creates a new editor revision."""
    _put(
        integration,
        f"/themes/{theme_id}/assets.json",
        {"asset": {"key": key, "value": value}},
    )


# ─── JSON-aware replace ────────────────────────────────────────────────────


def _replace_in_json_string_values(obj: Any, needle: str, replacement: str) -> tuple[Any, bool]:
    """Walk a parsed-JSON tree. Replace the first occurrence of `needle`
    found inside any string value. Returns (new_tree, replaced_bool)."""
    if isinstance(obj, str):
        if needle in obj:
            return obj.replace(needle, replacement, 1), True
        return obj, False
    if isinstance(obj, list):
        out: list = []
        replaced = False
        for item in obj:
            if not replaced:
                new_item, did = _replace_in_json_string_values(item, needle, replacement)
                if did:
                    replaced = True
                out.append(new_item)
            else:
                out.append(item)
        return out, replaced
    if isinstance(obj, dict):
        out_d: dict = {}
        replaced = False
        for k, v in obj.items():
            if not replaced:
                new_v, did = _replace_in_json_string_values(v, needle, replacement)
                if did:
                    replaced = True
                out_d[k] = new_v
            else:
                out_d[k] = v
        return out_d, replaced
    return obj, False


def _try_replace_in_asset(value: str, key: str, needle: str, replacement: str) -> tuple[str, bool]:
    """Replace inside one asset. JSON assets get a structured walk so we
    don't accidentally corrupt the file by hitting a substring inside an
    identifier or property name. Liquid/JS assets get a raw replace."""
    if not needle or needle not in value:
        # Fast bail — if the raw bytes don't contain the needle, nothing to do.
        # (For JSON this is sound because string values are stored verbatim
        # except for backslash/quote escapes, which are rare in user-facing
        # copy.)
        return value, False

    if key.endswith(".json"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            # Malformed JSON shouldn't happen on a published Shopify theme,
            # but if it does fall back to raw replace.
            return value.replace(needle, replacement, 1), True
        new_tree, replaced = _replace_in_json_string_values(parsed, needle, replacement)
        if not replaced:
            return value, False
        # Preserve formatting Shopify expects — pretty-printed with 2-space
        # indent matches what the Theme Editor emits.
        return json.dumps(new_tree, indent=2, ensure_ascii=False), True

    # Liquid / JS — raw replace. Includes embedded HTML text inside Liquid
    # templates, which is the other place hero/CTA strings hide.
    return value.replace(needle, replacement, 1), True


# ─── Public entrypoint ────────────────────────────────────────────────────


def find_and_replace_text(integration, needle: str, replacement: str, *, url: str = "") -> dict:
    """Locate `needle` somewhere in the active theme's text assets and
    replace its first occurrence with `replacement`. Returns:

        {"ok": True,  "asset_key": "sections/image-banner.json", "preview": "..."}
        {"ok": False, "reason": "Text not found in any theme asset."}

    If `url` is provided, the template file matching that URL is checked
    first (e.g. `templates/index.json` for `/`). This avoids accidentally
    editing the wrong asset when the same text appears in two places
    (e.g. a hero heading and an unrelated footer link).

    Raises ThemeEditError for unrecoverable problems (auth, network, no
    theme), so the caller can surface them with a specific message.
    """
    needle = (needle or "").strip()
    replacement = (replacement or "").strip()
    if not needle:
        return {"ok": False, "reason": "Empty original text."}
    if needle == replacement:
        return {"ok": False, "reason": "Original and replacement are identical — nothing to do."}

    theme_id = _get_active_theme_id(integration)
    keys = _list_asset_keys(integration, theme_id)

    # Hoist the URL's most-likely template to the front so it's checked
    # before any sibling sections that happen to share the same text.
    hint = _template_key_for_url(url) if url else None
    if hint and hint in keys:
        keys = [hint] + [k for k in keys if k != hint]

    logger.info(
        "shopify_theme: scanning %d candidate assets on theme=%s (hint=%s)",
        len(keys),
        theme_id,
        hint,
    )

    for key in keys:
        try:
            value = _read_asset(integration, theme_id, key)
        except ThemeEditError as exc:
            logger.info("shopify_theme: skipped %s (%s)", key, exc)
            continue
        if not value:
            continue
        new_value, replaced = _try_replace_in_asset(value, key, needle, replacement)
        if not replaced:
            continue
        _write_asset(integration, theme_id, key, new_value)
        logger.info(
            "shopify_theme: replaced text in %s (needle=%r, replacement=%r)",
            key,
            needle[:60],
            replacement[:60],
        )
        # Preview = the line that changed (first match, with surrounding
        # context). Useful so the FE can confirm the right thing was edited.
        idx = new_value.find(replacement)
        start = max(0, idx - 40)
        end = min(len(new_value), idx + len(replacement) + 40)
        preview = new_value[start:end].replace("\n", " ")
        return {"ok": True, "asset_key": key, "preview": preview}

    return {"ok": False, "reason": "Text not found in any theme asset."}

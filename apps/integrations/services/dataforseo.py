"""
DataForSEO Backlinks API client.

Provides batch enrichment of domain authority and backlink metrics for the
Citation Authority panel.

Auth: HTTP Basic with DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD env vars.
Endpoints used (live mode = no queue, immediate response):
    POST /v3/backlinks/bulk_ranks/live              -> rank (0-1000)
    POST /v3/backlinks/bulk_referring_domains/live  -> referring_domains, backlinks
"""
from __future__ import annotations

import logging
from typing import Iterable

import requests
from django.conf import settings

logger = logging.getLogger("apps")

API_BASE = "https://api.dataforseo.com/v3"
TIMEOUT_SECONDS = 30
DATAFORSEO_OK_STATUS = 20000


class DataForSEONotConfigured(RuntimeError):
    """Raised when DataForSEO credentials are missing from settings."""


class DataForSEOError(RuntimeError):
    """Raised when DataForSEO returns a non-success status code."""


def _auth() -> tuple[str, str]:
    login = getattr(settings, "DATAFORSEO_LOGIN", "") or ""
    password = getattr(settings, "DATAFORSEO_PASSWORD", "") or ""
    if not login or not password:
        raise DataForSEONotConfigured(
            "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD env vars are not set."
        )
    return (login, password)


def _post(path: str, payload: list[dict]) -> dict:
    from apps.integrations._http import request_with_retry

    resp = request_with_retry(
        "POST",
        f"{API_BASE}{path}",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status_code") != DATAFORSEO_OK_STATUS:
        raise DataForSEOError(
            f"{path}: {body.get('status_code')} {body.get('status_message')}"
        )
    # DataForSEO returns 200/20000 at the envelope level even when individual
    # tasks fail (auth scope, missing subscription, malformed target). Surface
    # the first per-task failure so callers see the real problem.
    for task in body.get("tasks") or []:
        task_status = task.get("status_code")
        if task_status and task_status != DATAFORSEO_OK_STATUS:
            raise DataForSEOError(
                f"{path}: {task_status} {task.get('status_message')}"
            )
    return body


def _extract_items(body: dict) -> list[dict]:
    """Flatten DataForSEO's tasks -> result -> items envelope into a single list."""
    items: list[dict] = []
    for task in body.get("tasks") or []:
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                items.append(item)
    return items


def fetch_domain_metrics(domains: Iterable[str]) -> dict[str, dict]:
    """
    Batch-fetch backlink metrics for a set of bare domains (no scheme/path).

    Returns {domain: {"referring_domains": int, "backlinks": int, "rank": int}}.
    Domains with no data appear with zero values.
    """
    targets = sorted({d.strip().lower() for d in domains if d and d.strip()})
    if not targets:
        return {}

    rd_body = _post(
        "/backlinks/bulk_referring_domains/live",
        [{"targets": targets}],
    )
    rank_body = _post(
        "/backlinks/bulk_ranks/live",
        [{"targets": targets}],
    )

    rd_by_target = {
        (row.get("target") or "").lower(): row
        for row in _extract_items(rd_body)
        if row.get("target")
    }
    rank_by_target = {
        (row.get("target") or "").lower(): row
        for row in _extract_items(rank_body)
        if row.get("target")
    }

    out: dict[str, dict] = {}
    for d in targets:
        rd_row = rd_by_target.get(d, {})
        rank_row = rank_by_target.get(d, {})
        out[d] = {
            "referring_domains": int(rd_row.get("referring_domains") or 0),
            "backlinks": int(rd_row.get("backlinks") or 0),
            "rank": int(rank_row.get("rank") or 0),
        }
    return out

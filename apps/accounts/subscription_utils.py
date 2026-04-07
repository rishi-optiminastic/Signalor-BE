"""
Subscription checks for paid features (e.g. GEO analysis).

Default: enforcement is OFF. Set SUBSCRIPTION_REQUIRED=true to require an active
active subscription before /api/analyzer/analyze/ (and related reanalyze paths).
"""

from __future__ import annotations

import os

from django.conf import settings

from .models import Subscription, PLAN_LIMITS

# ── Internal / Free Emails ────────────────────────────────────────────────
INTERNAL_DOMAINS = {"optiminastic.com"}


def is_internal_email(email: str | None) -> bool:
    """@optiminastic.com emails get free unlimited access."""
    raw = (email or "").strip().lower()
    if not raw or "@" not in raw:
        return False
    domain = raw.split("@", 1)[1]
    return domain in INTERNAL_DOMAINS


def _integration_subscription_required() -> bool:
    """
    Whether Shopify/WordPress OAuth must have an active active subscription.

    - DISABLE_PAYMENT=true → never enforce (local dev shortcut)
    - REQUIRE_SUBSCRIPTION_FOR_INTEGRATIONS=true  → always enforce
    - REQUIRE_SUBSCRIPTION_FOR_INTEGRATIONS=false → never enforce
    - unset → enforce only when DEBUG is False (production); allow on local DEBUG
    """
    if os.environ.get("DISABLE_PAYMENT", "").strip().lower() in ("1", "true", "yes"):
        return False
    raw = os.environ.get("REQUIRE_SUBSCRIPTION_FOR_INTEGRATIONS", "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    return not getattr(settings, "DEBUG", False)


def is_subscription_enforcement_enabled() -> bool:
    return os.environ.get("SUBSCRIPTION_REQUIRED", "false").lower() in (
        "1",
        "true",
        "yes",
    )


def integration_connect_allowed_for_email(email: str | None) -> tuple[bool, str]:
    """
    Gate Shopify / WordPress OAuth on an active subscription.
    @optiminastic.com emails always allowed.
    """
    if is_internal_email(email):
        return True, ""

    if not _integration_subscription_required():
        return True, ""

    raw = (email or "").strip()
    if not raw:
        return False, "Email is required."

    normalized = raw.lower()
    try:
        sub = Subscription.objects.get(email=normalized)
    except Subscription.DoesNotExist:
        return (
            False,
            "Active subscription required to connect your store.",
        )
    if not sub.is_active:
        return (
            False,
            "Active subscription required to connect your store.",
        )
    return True, ""


def analysis_allowed_for_email(email: str | None) -> tuple[bool, str]:
    """
    Returns (True, "") if this email may start analysis, else (False, error_message).
    @optiminastic.com emails always allowed.
    """
    if is_internal_email(email):
        return True, ""

    if not is_subscription_enforcement_enabled():
        return True, ""

    raw = (email or "").strip()
    if not raw:
        return False, "Email is required. Sign in to run analysis."

    normalized = raw.lower()
    try:
        sub = Subscription.objects.get(email=normalized)
    except Subscription.DoesNotExist:
        return (
            False,
            "Active subscription required. Complete checkout to run analysis.",
        )
    if not sub.is_active:
        return (
            False,
            "Your subscription is not active. Update billing to run analysis.",
        )
    return True, ""


# ── Plan Limit Helpers ────────────────────────────────────────────────────

def _get_sub(email: str | None) -> Subscription | None:
    raw = (email or "").strip().lower()
    if not raw:
        return None
    try:
        return Subscription.objects.get(email=raw)
    except Subscription.DoesNotExist:
        return None


def get_plan_limits(email: str | None) -> dict:
    """Return the plan limits dict for a user (defaults to starter).
    Internal emails get unlimited (business) limits."""
    if is_internal_email(email):
        return PLAN_LIMITS["business"]
    sub = _get_sub(email)
    if sub and sub.is_active:
        return sub.limits
    return PLAN_LIMITS["starter"]


def project_limit_reached(email: str | None) -> tuple[bool, str]:
    """Check if user has reached their project (organization) limit."""
    if is_internal_email(email):
        return False, ""
    if not is_subscription_enforcement_enabled():
        return False, ""

    sub = _get_sub(email)
    if not sub or not sub.is_active:
        return True, "Active subscription required."

    from apps.organizations.models import Organization
    count = Organization.objects.filter(owner_email=sub.email).count()
    max_projects = sub.limits["max_projects"]
    if count >= max_projects:
        return True, f"Your {sub.limits['label']} plan allows {max_projects} project(s). Upgrade to add more."
    return False, ""


def prompt_limit_reached(email: str | None, run_id: int | None = None) -> tuple[bool, str]:
    """Check if user has reached their prompt tracking limit."""
    if is_internal_email(email):
        return False, ""
    if not is_subscription_enforcement_enabled():
        return False, ""

    sub = _get_sub(email)
    if not sub or not sub.is_active:
        return True, "Active subscription required."

    from apps.analyzer.models import PromptTrack
    count = PromptTrack.objects.filter(analysis_run__email=sub.email).count()
    max_prompts = sub.limits["max_prompts"]
    if count >= max_prompts:
        return True, f"Your {sub.limits['label']} plan allows {max_prompts} prompts. Upgrade to add more."
    return False, ""


def engine_allowed(email: str | None, engine: str) -> tuple[bool, str]:
    """Check if the user's plan allows a specific AI engine."""
    if is_internal_email(email):
        return True, ""
    if not is_subscription_enforcement_enabled():
        return True, ""

    sub = _get_sub(email)
    if not sub or not sub.is_active:
        return False, "Active subscription required."

    allowed = sub.limits["engines"]
    if engine not in allowed:
        return False, f"The {engine} engine is not available on your {sub.limits['label']} plan. Upgrade to access it."
    return True, ""

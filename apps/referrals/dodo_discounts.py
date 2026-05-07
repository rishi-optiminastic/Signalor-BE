"""
Dodo Payments discount integration for referrals.

These functions are **stubs** — they record the intent and return a placeholder
discount ID. Replace the bodies with real Dodo API calls (POST /v1/discounts +
subscription update) once the exact endpoint shape is locked in.

The webhook flow that calls these is already wired in views.py, so swapping the
stubs for real calls is the only step needed to go live.
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

logger = logging.getLogger(__name__)


def _stub_id(prefix: str) -> str:
    return f"stub_{prefix}_{secrets.token_hex(6)}"


def create_referee_discount(referee_email: str) -> Optional[str]:
    """10% off the referee's first invoice.

    TODO(dodo): POST /v1/discounts with type=percentage, amount=10,
    duration=once, max_uses=1, customer=<referee_email>.
    Return the discount_id so the checkout link can include it.
    """
    discount_id = _stub_id("referee")
    logger.info(
        "referrals.dodo: STUB create_referee_discount referee=%s discount_id=%s",
        referee_email, discount_id,
    )
    return discount_id


def create_referrer_discount(referrer_email: str, subscription_id: str = "") -> Optional[str]:
    """20% off the referrer's next renewal — one cycle only.

    TODO(dodo): POST /v1/discounts with type=percentage, amount=20,
    duration=once, then attach to the referrer's existing subscription via
    subscription update / coupon application.
    """
    discount_id = _stub_id("referrer")
    logger.info(
        "referrals.dodo: STUB create_referrer_discount referrer=%s sub=%s discount_id=%s",
        referrer_email, subscription_id or "<unknown>", discount_id,
    )
    return discount_id


def revoke_discount(discount_id: str) -> bool:
    """Revoke a previously-staged discount (e.g. referee cancelled before referrer's renewal)."""
    if not discount_id:
        return False
    logger.info("referrals.dodo: STUB revoke_discount discount_id=%s", discount_id)
    return True

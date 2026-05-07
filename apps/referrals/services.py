"""
High-level referral flow used by views and webhooks. Pure orchestration —
delegates the data layer to models and the payment side to dodo_discounts.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.utils import timezone

from .models import Referral, ReferralCode, ReferralReward
from .dodo_discounts import (
    create_referee_discount,
    create_referrer_discount,
    revoke_discount,
)

logger = logging.getLogger(__name__)


def get_or_create_code(email: str) -> ReferralCode:
    """Idempotent: return the user's referral code, creating it on first call."""
    return ReferralCode.for_email(email)


def link_referee(code: str, referee_email: str) -> Optional[Referral]:
    """Called at sign-up when ?ref=CODE is present. Idempotent.

    Returns the Referral row (existing or new) or None if the code is invalid
    or the referee is the same person as the referrer.
    """
    code = (code or "").strip().upper()
    if not code or not referee_email:
        return None

    code_row = ReferralCode.objects.filter(code=code).first()
    if not code_row:
        logger.info("referrals: invalid code attempted code=%s", code)
        return None

    if code_row.owner_email.lower() == referee_email.lower():
        logger.info("referrals: self-referral blocked email=%s", referee_email)
        return None

    existing = Referral.objects.filter(referee_email=referee_email).first()
    if existing:
        return existing  # already linked — don't re-link to a different referrer

    referral = Referral.objects.create(
        referrer_email=code_row.owner_email,
        referee_email=referee_email,
        code_used=code,
        status=Referral.Status.PENDING,
    )

    # Stage the referee-side 10% discount so the next checkout can apply it.
    discount_id = create_referee_discount(referee_email)
    if discount_id:
        referral.referee_discount_id = discount_id
        referral.save(update_fields=["referee_discount_id"])

    logger.info(
        "referrals: linked referrer=%s referee=%s code=%s",
        code_row.owner_email, referee_email, code,
    )
    return referral


def on_referee_first_payment(referee_email: str, referrer_subscription_id: str = "") -> None:
    """Webhook hook: the referee's first paid invoice cleared.

    Marks the Referral as `paid` and stages the 20% off-one-cycle reward for
    the referrer. No-op if the referee wasn't referred or already triggered.
    """
    referral = Referral.objects.filter(referee_email=referee_email).first()
    if not referral or referral.status != Referral.Status.PENDING:
        return

    referral.status = Referral.Status.PAID
    referral.paid_at = timezone.now()
    referral.save(update_fields=["status", "paid_at"])

    # No stacking — if referrer already has a pending reward, leave it alone.
    if ReferralReward.objects.filter(
        referrer_email=referral.referrer_email,
        status=ReferralReward.Status.PENDING,
    ).exists():
        logger.info(
            "referrals: referrer=%s already has pending reward — skipping new one",
            referral.referrer_email,
        )
        return

    discount_id = create_referrer_discount(
        referral.referrer_email, subscription_id=referrer_subscription_id
    )

    ReferralReward.objects.create(
        referral=referral,
        referrer_email=referral.referrer_email,
        percent_off=20,
        dodo_discount_id=discount_id or "",
    )
    if discount_id:
        referral.referrer_discount_id = discount_id
        referral.save(update_fields=["referrer_discount_id"])

    logger.info(
        "referrals: staged 20%% reward referrer=%s from referee=%s",
        referral.referrer_email, referee_email,
    )


def on_referee_cancelled(referee_email: str) -> None:
    """Webhook hook: the referee cancelled.

    If the referrer's reward is still PENDING (i.e. their next renewal hasn't
    fired yet), revoke it. If it's already APPLIED, leave it — we don't claw
    back applied discounts.
    """
    referral = Referral.objects.filter(referee_email=referee_email).first()
    if not referral:
        return

    referral.status = Referral.Status.CANCELLED
    referral.save(update_fields=["status"])

    reward = getattr(referral, "reward", None)
    if not reward or reward.status != ReferralReward.Status.PENDING:
        return

    revoke_discount(reward.dodo_discount_id)
    reward.status = ReferralReward.Status.REVOKED
    reward.revoked_at = timezone.now()
    reward.save(update_fields=["status", "revoked_at"])

    logger.info(
        "referrals: revoked pending reward for referrer=%s (referee %s cancelled)",
        referral.referrer_email, referee_email,
    )


def on_referrer_renewal(referrer_email: str) -> None:
    """Webhook hook: the referrer's renewal invoice was processed.

    If a PENDING reward exists, mark it APPLIED — it has now been used and
    can't be re-applied (one cycle only).
    """
    reward = (
        ReferralReward.objects
        .filter(referrer_email=referrer_email, status=ReferralReward.Status.PENDING)
        .order_by("created_at")
        .first()
    )
    if not reward:
        return

    reward.status = ReferralReward.Status.APPLIED
    reward.applied_at = timezone.now()
    reward.save(update_fields=["status", "applied_at"])
    logger.info("referrals: applied reward for referrer=%s", referrer_email)

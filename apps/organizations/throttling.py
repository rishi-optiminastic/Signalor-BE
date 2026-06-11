"""Per-email throttle for the onboarding write endpoint.

Per-IP limits (global middleware + DRF anon) catch script-kiddie attacks
from one box. A rotating-IP botnet can still hammer ``/organizations/onboard/``
by minting a fresh IP per request. Keying the throttle on the request-body
email forces an attacker to also rotate emails, which collapses the unique
org count they can create per email — exactly the abuse the bug report
describes.
"""

from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle


class OnboardEmailThrottle(SimpleRateThrottle):
    """5 attempts / hour, keyed on the normalized email in the POST body.

    Falls back to per-IP if no email is supplied so a missing field can't
    silently bypass the throttle.
    """

    scope = "onboard_email"
    rate = "5/hour"

    def get_cache_key(self, request, view):
        email = ""
        try:
            email = (request.data.get("email") or "").strip().lower()
        except Exception:
            email = ""
        if not email:
            ident = self.get_ident(request)
            return self.cache_format % {"scope": self.scope, "ident": f"ip:{ident}"}
        return self.cache_format % {"scope": self.scope, "ident": f"email:{email}"}

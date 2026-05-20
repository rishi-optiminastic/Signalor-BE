"""Single source of truth for "get me the PDF bytes for this payment_id".

Encapsulates the full resolution chain so the DownloadInvoiceView and the
post-payment email sender share one path:

    B2 cache  →  Dodo's invoice PDF  →  our generated $0 invoice fallback

Caller gets back ``(pdf_bytes, error_tag)``. PDF bytes are populated for
any successful resolution; the error tag is set only when *all three*
layers fail (and the caller wants to know why).
"""

from __future__ import annotations

import logging

from .dodo_invoice import (
    fetch_payment_invoice_pdf,
    retrieve_payment,
    retrieve_product,
    retrieve_subscription,
)
from .invoice_storage import cache_invoice, is_b2_enabled
from .zero_invoice import render_zero_invoice_pdf

logger = logging.getLogger("apps")


def resolve_invoice_pdf(payment_id: str) -> tuple[bytes | None, str | None]:
    """Return ``(pdf_bytes, error_tag)`` for the given Dodo payment_id.

    Order:
      1. ``fetch_payment_invoice_pdf`` — handles B2 cache + Dodo PDF endpoint.
      2. If Dodo 404s **and** the payment is $0, render the Signalor-branded
         zero invoice and cache it in B2 so subsequent calls are O(1).
      3. Otherwise return ``(None, err)``.
    """
    if not payment_id:
        return None, "not_configured"

    pdf, err = fetch_payment_invoice_pdf(payment_id)
    if pdf:
        return pdf, None

    # Only the upstream_404 path can be filled by our generator — every
    # other error (network, 5xx, 401) is a Dodo problem we can't paper over.
    if err != "upstream_404":
        return None, err

    payment_obj, _ = retrieve_payment(payment_id)
    if not payment_obj or (payment_obj.get("total_amount") or 0) != 0:
        # Either we couldn't read the payment back, or it's a real non-zero
        # payment without a PDF (truly missing on Dodo's side). Don't fake
        # an invoice for it.
        return None, err

    # Look up the product so the invoice shows the listed price (and the
    # discount math that brings it to zero). Subscription payments have
    # ``product_cart`` = None — fall back to the subscription's product_id.
    product = None
    product_lookup_id = ""
    cart = payment_obj.get("product_cart") or []
    if isinstance(cart, list) and cart:
        product_lookup_id = (cart[0] or {}).get("product_id") or ""
    if not product_lookup_id and payment_obj.get("subscription_id"):
        sub_obj, _ = retrieve_subscription(payment_obj["subscription_id"])
        if sub_obj:
            product_lookup_id = sub_obj.get("product_id") or ""
    if product_lookup_id:
        product, _ = retrieve_product(product_lookup_id)

    generated = render_zero_invoice_pdf(payment_obj, product)
    if not generated:
        return None, "render_failed"

    if is_b2_enabled():
        cache_invoice(payment_id, generated)
    return generated, None

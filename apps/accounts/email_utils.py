import logging
import os

from django.conf import settings
from django.core.mail import EmailMessage, send_mail
from django.template.loader import render_to_string

logger = logging.getLogger("apps")

FRONTEND_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")


def send_payment_confirmation_email(to_email: str, payment_id: str, plan: str, currency: str):
    """Send payment receipt email with invoice PDF attached via SendGrid."""
    from apps.accounts.dodo_invoice import fetch_payment_invoice_pdf

    plan_label = {"starter": "Starter", "pro": "Pro", "business": "Max"}.get(plan, plan.title())
    context = {
        "plan_label": plan_label,
        "currency": currency.upper(),
        "dashboard_url": f"{FRONTEND_URL}/dashboard",
        "billing_url": f"{FRONTEND_URL}/settings/billing",
        "frontend_url": FRONTEND_URL,
        "logo_url": settings.SIGNALOR_LOGO_URL,
    }

    try:
        html_body = render_to_string("accounts/payment_email.html", context)
    except Exception:
        logger.exception("Failed to render payment email template for %s", to_email)
        return

    msg = EmailMessage(
        subject=f"Your Signalor {plan_label} payment was successful",
        body=html_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
    )
    msg.content_subtype = "html"

    if payment_id:
        pdf, err = fetch_payment_invoice_pdf(payment_id)
        if pdf:
            safe_name = payment_id.replace("/", "_")[:80]
            msg.attach(f"signalor-invoice-{safe_name}.pdf", pdf, "application/pdf")
        else:
            logger.warning("Could not attach invoice PDF for %s: %s", to_email, err)

    try:
        msg.send(fail_silently=False)
        logger.info("Payment confirmation email sent to %s (plan=%s)", to_email, plan)
    except Exception:
        logger.exception("Failed to send payment confirmation email to %s", to_email)


def send_welcome_email(to_email: str, company_name: str):
    """Send welcome email on first org creation."""
    dashboard_url = f"{FRONTEND_URL}/dashboard"
    context = {
        "company_name": company_name,
        "dashboard_url": dashboard_url,
        "frontend_url": FRONTEND_URL,
        "logo_url": settings.SIGNALOR_LOGO_URL,
    }

    try:
        html_body = render_to_string("accounts/welcome_email.html", context)
    except Exception:
        logger.exception("Failed to render welcome email template for %s", to_email)
        return

    try:
        send_mail(
            subject="Welcome to Signalor!",
            message=f"Welcome to Signalor, {company_name}! Visit your dashboard: {dashboard_url}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to_email],
            html_message=html_body,
            fail_silently=False,
        )
        logger.info("Welcome email sent to %s", to_email)
    except Exception:
        logger.exception("Failed to send welcome email to %s", to_email)

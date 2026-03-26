from django.urls import path

from .views import (
    CreateCheckoutSessionView,
    SubscriptionStatusView,
    StripeWebhookView,
    TerminateAccountView,
    CancelTerminationView,
    DeleteAccountView,
)

app_name = "accounts"

urlpatterns = [
    path("payments/create-checkout/", CreateCheckoutSessionView.as_view(), name="create-checkout"),
    path("payments/status/", SubscriptionStatusView.as_view(), name="subscription-status"),
    path("payments/webhook/", StripeWebhookView.as_view(), name="stripe-webhook"),
    path("account/terminate/", TerminateAccountView.as_view(), name="terminate-account"),
    path("account/cancel-termination/", CancelTerminationView.as_view(), name="cancel-termination"),
    path("account/delete/", DeleteAccountView.as_view(), name="delete-account"),
]

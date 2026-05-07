from django.urls import path

from .views import ReferralMeView, ReferralRedeemView

urlpatterns = [
    path("me/", ReferralMeView.as_view(), name="referrals-me"),
    path("redeem/", ReferralRedeemView.as_view(), name="referrals-redeem"),
]

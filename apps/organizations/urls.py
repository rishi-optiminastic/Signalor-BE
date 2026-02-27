from django.urls import path

from .views import CheckOrganizationView, OnboardView

app_name = "organizations"

urlpatterns = [
    path("organizations/onboard/", OnboardView.as_view(), name="onboard"),
    path("organizations/check/", CheckOrganizationView.as_view(), name="check"),
]

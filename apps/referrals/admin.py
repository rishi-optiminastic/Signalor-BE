from django.contrib import admin

from .models import Referral, ReferralCode, ReferralReward


@admin.register(ReferralCode)
class ReferralCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "owner_email", "created_at")
    search_fields = ("code", "owner_email")


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = ("referee_email", "referrer_email", "status", "created_at", "paid_at")
    list_filter = ("status",)
    search_fields = ("referee_email", "referrer_email", "code_used")


@admin.register(ReferralReward)
class ReferralRewardAdmin(admin.ModelAdmin):
    list_display = ("referrer_email", "percent_off", "status", "created_at", "applied_at")
    list_filter = ("status",)
    search_fields = ("referrer_email",)

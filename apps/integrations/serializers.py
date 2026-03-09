from rest_framework import serializers

from .models import (
    GADataSnapshot,
    Integration,
    ShopifyDataSnapshot,
    WordPressDataSnapshot,
)


class IntegrationSerializer(serializers.ModelSerializer):
    provider_display = serializers.CharField(
        source="get_provider_display", read_only=True
    )

    class Meta:
        model = Integration
        fields = [
            "id", "provider", "provider_display", "is_active",
            "metadata", "created_at", "updated_at",
        ]


class GADataSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = GADataSnapshot
        fields = [
            "id", "date_start", "date_end", "sessions", "organic_sessions",
            "bounce_rate", "avg_session_duration", "top_pages",
            "traffic_sources", "daily_trend", "sync_status",
            "error_message", "created_at",
        ]


class SelectPropertySerializer(serializers.Serializer):
    email = serializers.EmailField()
    property_id = serializers.CharField(max_length=50)
    property_name = serializers.CharField(max_length=255, required=False, default="")

    def validate_email(self, value):
        return value.lower().strip()


class ShopifyConnectSerializer(serializers.Serializer):
    email = serializers.EmailField()
    shop_domain = serializers.CharField(max_length=255)
    access_token = serializers.CharField(max_length=500)

    def validate_email(self, value):
        return value.lower().strip()

    def validate_shop_domain(self, value):
        domain = value.strip().lower()
        # Strip protocol if provided
        domain = domain.replace("https://", "").replace("http://", "")
        # Strip trailing slash
        domain = domain.rstrip("/")
        # Normalize to .myshopify.com
        if not domain.endswith(".myshopify.com"):
            domain = domain.split(".")[0] + ".myshopify.com"
        return domain


class ShopifyDataSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShopifyDataSnapshot
        fields = [
            "id", "date_start", "date_end", "total_orders", "total_revenue",
            "average_order_value", "total_customers", "top_products",
            "daily_orders", "sync_status", "error_message", "created_at",
        ]


class WordPressConnectSerializer(serializers.Serializer):
    email = serializers.EmailField()
    site_url = serializers.CharField(max_length=500)
    username = serializers.CharField(max_length=255, required=False, default="", allow_blank=True)
    app_password = serializers.CharField(max_length=255, required=False, default="", allow_blank=True)

    def validate_email(self, value):
        return value.lower().strip()

    def validate_site_url(self, value):
        import re
        site_url = value.strip().rstrip("/")
        site_url = re.sub(r'^https?s+://', 'https://', site_url)
        if not site_url.startswith(("http://", "https://")):
            site_url = f"https://{site_url}"
        from urllib.parse import urlparse
        parsed = urlparse(site_url)
        if not parsed.netloc or "." not in parsed.netloc:
            raise serializers.ValidationError(
                "Invalid site URL. Please provide a valid WordPress site address."
            )
        return site_url

    def validate(self, data):
        site_url = data.get("site_url", "")
        is_wpcom = ".wordpress.com" in site_url or ".wp.com" in site_url
        if not is_wpcom:
            username = data.get("username", "").strip()
            app_password = data.get("app_password", "").strip()
            
            if not username:
                raise serializers.ValidationError({
                    "username": "Required for self-hosted WordPress."
                })
            if not app_password:
                raise serializers.ValidationError({
                    "app_password": "Required for self-hosted WordPress."
                })
        
        return data


class WordPressDataSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = WordPressDataSnapshot
        fields = [
            "id",
            "date_start",
            "date_end",
            "total_posts",
            "total_pages",
            "published_posts_30d",
            "updated_posts_30d",
            "top_posts",
            "daily_publishing",
            "sync_status",
            "error_message",
            "created_at",
        ]

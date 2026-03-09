import hashlib
import hmac
import json
import logging
import os
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponseRedirect
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google.analytics.admin import AnalyticsAdminServiceClient
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import Organization

from .models import (
    GADataSnapshot,
    Integration,
    ShopifyDataSnapshot,
    WordPressDataSnapshot,
)
from .serializers import (
    GADataSnapshotSerializer,
    IntegrationSerializer,
    SelectPropertySerializer,
    ShopifyConnectSerializer,
    ShopifyDataSnapshotSerializer,
    WordPressConnectSerializer,
    WordPressDataSnapshotSerializer,
)

logger = logging.getLogger("apps")

GA_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
]

# ---------- helpers ----------

def _get_org_or_400(email):
    """Look up organization by email, return (org, None) or (None, Response)."""
    org = Organization.objects.filter(owner_email=email).first()
    if not org:
        return None, Response(
            {"error": "Organization not found for this email."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return org, None


def _resolve_org(email: str, org_id: int | None = None):
    """Resolve org by id (preferred) or fall back to email lookup."""
    if org_id:
        try:
            org = Organization.objects.get(pk=org_id)
            return org, None
        except Organization.DoesNotExist:
            return None, Response(
                {"error": "Organization not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
    return _get_org_or_400(email)


def _sign_state(payload: dict) -> str:
    """HMAC-sign a JSON state payload."""
    raw = json.dumps(payload, sort_keys=True)
    sig = hmac.new(
        settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256
    ).hexdigest()
    return json.dumps({"data": payload, "sig": sig})


def _verify_state(state_str: str) -> dict | None:
    """Verify HMAC signature and return payload, or None if invalid."""
    try:
        state = json.loads(state_str)
        raw = json.dumps(state["data"], sort_keys=True)
        expected = hmac.new(
            settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, state["sig"]):
            return state["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def _build_credentials(integration: Integration) -> Credentials:
    """Build google.oauth2.credentials.Credentials from an Integration."""
    return Credentials(
        token=integration.get_access_token(),
        refresh_token=integration.get_refresh_token(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=GA_SCOPES,
    )


def _refresh_if_needed(integration: Integration, creds: Credentials) -> Credentials:
    """Refresh credentials if expired, persist new tokens.

    Google access tokens live ~1 hour.  The Credentials object built from
    stored tokens has no ``expiry``, so ``creds.expired`` is always False.
    We therefore *always* attempt a refresh when a refresh_token is present
    and catch the "already valid" case silently.
    """
    if not creds.refresh_token:
        return creds

    # If expiry is unknown (None) or the token is expired, force refresh
    needs_refresh = creds.expiry is None or creds.expired
    if needs_refresh:
        try:
            creds.refresh(GoogleRequest())
            integration.set_access_token(creds.token)
            if creds.refresh_token:
                integration.set_refresh_token(creds.refresh_token)
            integration.save(update_fields=[
                "access_token_encrypted", "refresh_token_encrypted", "updated_at",
            ])
        except Exception as exc:
            logger.warning("Token refresh failed: %s", exc)
            raise
    return creds


# ---------- OAuth endpoints ----------

class GAAuthURLView(APIView):
    """GET /api/integrations/google-analytics/auth-url/?email="""
    permission_classes = [AllowAny]

    def get(self, request):
        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        state = _sign_state({"org_id": org.id, "email": email})

        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_ANALYTICS_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(GA_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

        return Response({"auth_url": auth_url})


class GACallbackView(APIView):
    """POST /api/integrations/google-analytics/callback/"""
    permission_classes = [AllowAny]

    def post(self, request):
        code = request.data.get("code")
        state_str = request.data.get("state")

        if not code or not state_str:
            return Response(
                {"error": "Both 'code' and 'state' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = _verify_state(state_str)
        if not payload:
            return Response(
                {"error": "Invalid or tampered state parameter."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org_id = payload.get("org_id")
        try:
            org = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist:
            return Response(
                {"error": "Organization not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Exchange code for tokens
        import requests as http_requests

        token_resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_ANALYTICS_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

        if token_resp.status_code != 200:
            logger.error("GA4 token exchange failed: %s", token_resp.text)
            return Response(
                {"error": "Failed to exchange authorization code."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tokens = token_resp.json()

        integration, created = Integration.objects.update_or_create(
            organization=org,
            provider=Integration.Provider.GOOGLE_ANALYTICS,
            defaults={"is_active": True},
        )
        integration.set_access_token(tokens["access_token"])
        if tokens.get("refresh_token"):
            integration.set_refresh_token(tokens["refresh_token"])
        integration.save()

        return Response(
            {
                "message": "Google Analytics connected successfully.",
                "integration": IntegrationSerializer(integration).data,
            },
            status=status.HTTP_200_OK,
        )


class IntegrationStatusView(APIView):
    """GET /api/integrations/status/?email=&org_id="""
    permission_classes = [AllowAny]
    throttle_classes = []  # high-frequency read for dashboard/sidebar state

    def get(self, request):
        email = request.query_params.get("email", "").lower().strip()
        org_id = request.query_params.get("org_id")
        org_id = int(org_id) if org_id and org_id.isdigit() else None

        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _resolve_org(email, org_id)
        if err:
            return err

        integrations = Integration.objects.filter(organization=org)
        serializer = IntegrationSerializer(integrations, many=True)
        return Response(serializer.data)


class GADisconnectView(APIView):
    """DELETE /api/integrations/google-analytics/disconnect/?email="""
    permission_classes = [AllowAny]

    def delete(self, request):
        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.GOOGLE_ANALYTICS,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "Google Analytics integration not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Try to revoke the token at Google
        try:
            import requests as http_requests

            http_requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": integration.get_access_token()},
            )
        except Exception:
            logger.warning("Failed to revoke Google token, deleting anyway")

        # Delete snapshots and integration
        integration.ga_snapshots.all().delete()
        integration.delete()

        return Response({"message": "Google Analytics disconnected."})


# ---------- Property selection ----------

class GAPropertiesListView(APIView):
    """GET /api/integrations/google-analytics/properties/?email="""
    permission_classes = [AllowAny]

    def get(self, request):
        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.GOOGLE_ANALYTICS,
                is_active=True,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "Google Analytics not connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        creds = _build_credentials(integration)
        creds = _refresh_if_needed(integration, creds)

        try:
            client = AnalyticsAdminServiceClient(credentials=creds)
            accounts = list(client.list_account_summaries())

            properties = []
            for account in accounts:
                for prop in account.property_summaries:
                    properties.append({
                        "property_id": prop.property.split("/")[-1],
                        "display_name": prop.display_name,
                        "account_name": account.display_name,
                    })

            return Response({"properties": properties})

        except Exception as e:
            logger.error("Failed to list GA4 properties: %s", str(e))
            return Response(
                {"error": f"Failed to list properties: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class GASelectPropertyView(APIView):
    """POST /api/integrations/google-analytics/select-property/"""
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SelectPropertySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        org, err = _get_org_or_400(data["email"])
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.GOOGLE_ANALYTICS,
                is_active=True,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "Google Analytics not connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        integration.metadata = {
            **integration.metadata,
            "property_id": data["property_id"],
            "property_name": data.get("property_name", ""),
        }
        integration.save(update_fields=["metadata", "updated_at"])

        return Response({
            "message": "Property selected successfully.",
            "integration": IntegrationSerializer(integration).data,
        })


# ---------- Data sync ----------

class GASyncView(APIView):
    """POST /api/integrations/google-analytics/sync/?email="""
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.GOOGLE_ANALYTICS,
                is_active=True,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "Google Analytics not connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not integration.metadata.get("property_id"):
            return Response(
                {"error": "No GA4 property selected."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .tasks import start_ga4_sync
        start_ga4_sync(integration.id)

        return Response({"message": "Sync started."}, status=status.HTTP_202_ACCEPTED)


class GADataView(APIView):
    """GET /api/integrations/google-analytics/data/?email="""
    permission_classes = [AllowAny]

    def get(self, request):
        from datetime import timedelta
        from django.utils import timezone

        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.GOOGLE_ANALYTICS,
                is_active=True,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "Google Analytics not connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Cleanup: delete snapshots older than 90 days
        cutoff = timezone.now() - timedelta(days=90)
        integration.ga_snapshots.filter(created_at__lt=cutoff).delete()

        snapshot = integration.ga_snapshots.first()  # latest by -created_at
        if not snapshot:
            return Response(
                {"error": "No data available. Trigger a sync first."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Auto-sync if snapshot is stale (>24h) and not currently syncing
        stale_threshold = timezone.now() - timedelta(hours=24)
        if (
            snapshot.created_at < stale_threshold
            and snapshot.sync_status == "complete"
            and not integration.ga_snapshots.filter(sync_status="syncing").exists()
        ):
            from .tasks import start_ga4_sync
            start_ga4_sync(integration.id)

        serializer = GADataSnapshotSerializer(snapshot)
        payload = serializer.data

        analyzed_url = request.query_params.get("analyzed_url", "").strip()
        if analyzed_url:
            try:
                from .services.ga4 import fetch_ga4_page_metrics
                payload["page_match"] = fetch_ga4_page_metrics(integration, analyzed_url)
            except Exception as exc:
                logger.warning("Failed GA page match lookup: %s", exc)
                payload["page_match"] = {
                    "found": False,
                    "page_path": "",
                    "sessions": 0,
                    "bounce_rate": 0.0,
                    "avg_session_duration": 0.0,
                }

        return Response(payload)


# ---------- Score vs Traffic Correlation ----------

class ScoreTrafficCorrelationView(APIView):
    """GET /api/integrations/score-traffic-correlation/?email="""
    permission_classes = [AllowAny]

    def get(self, request):
        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        from apps.analyzer.models import AnalysisRun

        # Get completed analysis runs for this email (last 30)
        runs = AnalysisRun.objects.filter(
            email=email,
            status=AnalysisRun.Status.COMPLETE,
            composite_score__isnull=False,
        ).order_by("created_at")[:30]

        # Get latest GA snapshot
        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.GOOGLE_ANALYTICS,
                is_active=True,
            )
            snapshot = integration.ga_snapshots.filter(
                sync_status="complete",
            ).first()
        except Integration.DoesNotExist:
            snapshot = None

        # Build daily trend lookup from GA data
        ga_daily = {}
        if snapshot and snapshot.daily_trend:
            for day in snapshot.daily_trend:
                ga_daily[day["date"]] = day

        # Build correlation data: pair each analysis run with nearest GA day
        data_points = []
        for run in runs:
            run_date = run.created_at.strftime("%Y-%m-%d")
            ga_day = ga_daily.get(run_date, {})
            data_points.append({
                "date": run_date,
                "geo_score": round(run.composite_score, 1),
                "sessions": ga_day.get("sessions", None),
                "organic_sessions": ga_day.get("organic_sessions", None),
                "url": run.url,
            })

        return Response({
            "data_points": data_points,
            "has_ga_data": bool(snapshot),
        })


# ---------- Shopify endpoints ----------

class ShopifyConnectView(APIView):
    """POST /api/integrations/shopify/connect/"""
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ShopifyConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        org, err = _get_org_or_400(data["email"])
        if err:
            return err

        # Validate against Shopify API
        from .services.shopify import validate_shopify_connection

        try:
            shop_info = validate_shopify_connection(
                data["shop_domain"], data["access_token"]
            )
        except ValueError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create or update integration
        integration, _ = Integration.objects.update_or_create(
            organization=org,
            provider=Integration.Provider.SHOPIFY,
            defaults={"is_active": True},
        )
        integration.set_access_token(data["access_token"])
        integration.metadata = {
            "shop_domain": data["shop_domain"],
            "shop_name": shop_info.get("name", data["shop_domain"]),
        }
        integration.save()

        return Response({
            "message": "Shopify connected successfully.",
            "integration": IntegrationSerializer(integration).data,
        })


class ShopifyAuthURLView(APIView):
    """GET /api/integrations/shopify/auth-url/?email=&shop=&org_id=&return_to="""
    permission_classes = [AllowAny]

    def get(self, request):
        email = request.query_params.get("email", "").lower().strip()
        shop = request.query_params.get("shop", "").strip()
        return_to = request.query_params.get("return_to", "").strip() or "/settings/integrations"
        org_id = request.query_params.get("org_id")
        org_id = int(org_id) if org_id and org_id.isdigit() else None

        if not email or not shop:
            return Response(
                {"error": "Both email and shop parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _resolve_org(email, org_id)
        if err:
            return err

        from .services.shopify import build_shopify_oauth_url, normalize_shop_domain

        shop_domain = normalize_shop_domain(shop)
        nonce = secrets.token_urlsafe(24)
        payload = {
            "org_id": org.id,
            "email": email,
            "shop_domain": shop_domain,
            "nonce": nonce,
            "return_to": return_to,
        }
        cache.set(f"shopify_oauth_state:{nonce}", payload, timeout=15 * 60)
        state = _sign_state(payload)

        client_id = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
        redirect_uri = os.getenv("SHOPIFY_REDIRECT_URI", "").strip()
        scopes = os.getenv("SHOPIFY_SCOPES", "read_products,read_orders,read_customers")

        if not client_id or not redirect_uri:
            return Response(
                {"error": "Shopify OAuth env is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        auth_url = build_shopify_oauth_url(
            shop_domain=shop_domain,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scopes=[s.strip() for s in scopes.split(",") if s.strip()],
        )
        return Response({"auth_url": auth_url})


class ShopifyCallbackView(APIView):
    """GET /api/integrations/shopify/callback/"""
    permission_classes = [AllowAny]

    def get(self, request):
        from .services.shopify import (
            exchange_shopify_oauth_code,
            normalize_shop_domain,
            register_app_uninstalled_webhook,
            validate_shopify_connection,
            verify_shopify_oauth_hmac,
        )

        query_string = request.META.get("QUERY_STRING", "")
        shop = request.query_params.get("shop", "").strip()
        code = request.query_params.get("code", "").strip()
        state_str = request.query_params.get("state", "").strip()

        frontend_base = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
        default_return_to = "/settings/integrations"

        def _redirect_with_status(ok: bool, reason: str = "", return_to: str = default_return_to):
            target = return_to if return_to.startswith("/") and not return_to.startswith("//") else default_return_to
            sep = "&" if "?" in target else "?"
            status_q = "connected" if ok else "error"
            url = f"{frontend_base}{target}{sep}{urlencode({'shopify': status_q, 'reason': reason})}"
            return HttpResponseRedirect(url)

        if not shop or not code or not state_str:
            return _redirect_with_status(False, "missing_params")

        payload = _verify_state(state_str)
        if not payload:
            return _redirect_with_status(False, "invalid_state")

        return_to = payload.get("return_to", default_return_to)
        nonce = payload.get("nonce", "")
        cached_payload = cache.get(f"shopify_oauth_state:{nonce}") if nonce else None
        if not nonce or not cached_payload:
            return _redirect_with_status(False, "expired_state", return_to=return_to)
        cache.delete(f"shopify_oauth_state:{nonce}")

        client_secret = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
        client_id = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
        if not client_id or not client_secret:
            return _redirect_with_status(False, "oauth_not_configured", return_to=return_to)

        if not verify_shopify_oauth_hmac(query_string, client_secret):
            return _redirect_with_status(False, "invalid_hmac", return_to=return_to)

        shop_domain = normalize_shop_domain(shop)
        if cached_payload.get("shop_domain") != shop_domain:
            return _redirect_with_status(False, "shop_mismatch", return_to=return_to)

        org_id = payload.get("org_id")
        try:
            org = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist:
            return _redirect_with_status(False, "org_not_found", return_to=return_to)

        try:
            tokens = exchange_shopify_oauth_code(
                shop_domain=shop_domain,
                client_id=client_id,
                client_secret=client_secret,
                code=code,
            )
            access_token = tokens.get("access_token", "")
            if not access_token:
                return _redirect_with_status(False, "missing_access_token", return_to=return_to)

            shop_info = validate_shopify_connection(shop_domain, access_token)

            integration, _ = Integration.objects.update_or_create(
                organization=org,
                provider=Integration.Provider.SHOPIFY,
                defaults={"is_active": True},
            )
            integration.set_access_token(access_token)
            integration.metadata = {
                "shop_domain": shop_domain,
                "shop_name": shop_info.get("name", shop_domain),
                "scope": tokens.get("scope", ""),
            }
            integration.save()

            webhook_url = os.getenv("SHOPIFY_APP_UNINSTALLED_WEBHOOK_URL", "").strip()
            if webhook_url:
                register_app_uninstalled_webhook(shop_domain, access_token, webhook_url)

        except Exception as exc:
            logger.error("Shopify callback failed: %s", exc)
            return _redirect_with_status(False, "callback_failed", return_to=return_to)

        return _redirect_with_status(True, return_to=return_to)


class ShopifyAppUninstalledWebhookView(APIView):
    """POST /api/integrations/shopify/webhooks/app-uninstalled/"""
    permission_classes = [AllowAny]

    def post(self, request):
        from .services.shopify import normalize_shop_domain, verify_shopify_webhook_hmac

        secret = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
        hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
        shop_header = request.headers.get("X-Shopify-Shop-Domain", "")
        if not secret or not hmac_header or not shop_header:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if not verify_shopify_webhook_hmac(request.body, hmac_header, secret):
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        shop_domain = normalize_shop_domain(shop_header)
        integration = Integration.objects.filter(
            provider=Integration.Provider.SHOPIFY,
            metadata__shop_domain=shop_domain,
        ).first()

        if integration:
            integration.shopify_snapshots.all().delete()
            integration.delete()

        return Response({"message": "Processed."}, status=status.HTTP_200_OK)


class ShopifyDisconnectView(APIView):
    """DELETE /api/integrations/shopify/disconnect/?email=&org_id="""
    permission_classes = [AllowAny]

    def delete(self, request):
        email = request.query_params.get("email", "").lower().strip()
        org_id = request.query_params.get("org_id")
        org_id = int(org_id) if org_id and org_id.isdigit() else None

        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _resolve_org(email, org_id)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.SHOPIFY,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "Shopify integration not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        integration.shopify_snapshots.all().delete()
        integration.delete()

        return Response({"message": "Shopify disconnected."})


class ShopifySyncView(APIView):
    """POST /api/integrations/shopify/sync/?email=&org_id="""
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.query_params.get("email", "").lower().strip()
        org_id = request.query_params.get("org_id")
        org_id = int(org_id) if org_id and org_id.isdigit() else None

        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _resolve_org(email, org_id)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.SHOPIFY,
                is_active=True,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "Shopify not connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from .tasks import start_shopify_sync
        start_shopify_sync(integration.id)

        return Response({"message": "Sync started."}, status=status.HTTP_202_ACCEPTED)


class ShopifyDataView(APIView):
    """GET /api/integrations/shopify/data/?email=&org_id="""
    permission_classes = [AllowAny]

    def get(self, request):
        from datetime import timedelta
        from django.utils import timezone

        email = request.query_params.get("email", "").lower().strip()
        org_id = request.query_params.get("org_id")
        org_id = int(org_id) if org_id and org_id.isdigit() else None

        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _resolve_org(email, org_id)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.SHOPIFY,
                is_active=True,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "Shopify not connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Cleanup old snapshots
        cutoff = timezone.now() - timedelta(days=90)
        integration.shopify_snapshots.filter(created_at__lt=cutoff).delete()

        snapshot = integration.shopify_snapshots.first()
        if not snapshot:
            return Response(
                {"error": "No data available. Trigger a sync first."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Auto-sync if stale
        stale_threshold = timezone.now() - timedelta(hours=24)
        if (
            snapshot.created_at < stale_threshold
            and snapshot.sync_status == "complete"
            and not integration.shopify_snapshots.filter(sync_status="syncing").exists()
        ):
            from .tasks import start_shopify_sync
            start_shopify_sync(integration.id)

        serializer = ShopifyDataSnapshotSerializer(snapshot)
        return Response(serializer.data)


class WordPressConnectView(APIView):
    """POST /api/integrations/wordpress/connect/"""
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = WordPressConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        org, err = _get_org_or_400(data["email"])
        if err:
            return err

        from .services.wordpress import validate_wordpress_connection

        try:
            wp_info = validate_wordpress_connection(
                data["site_url"], data["username"], data["app_password"]
            )
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        integration, _ = Integration.objects.update_or_create(
            organization=org,
            provider=Integration.Provider.WORDPRESS,
            defaults={"is_active": True},
        )
        integration.set_access_token(data["app_password"])
        integration.metadata = {
            "site_url": wp_info["site_url"],
            "site_name": wp_info["site_name"],
            "username": data["username"],
            "wp_version": wp_info.get("wp_version", ""),
        }
        integration.save()

        return Response(
            {
                "message": "WordPress connected successfully.",
                "integration": IntegrationSerializer(integration).data,
            }
        )


class WordPressDisconnectView(APIView):
    """DELETE /api/integrations/wordpress/disconnect/?email="""
    permission_classes = [AllowAny]

    def delete(self, request):
        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.WORDPRESS,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "WordPress integration not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        integration.wordpress_snapshots.all().delete()
        integration.delete()

        return Response({"message": "WordPress disconnected."})


class WordPressSyncView(APIView):
    """POST /api/integrations/wordpress/sync/?email="""
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.WORDPRESS,
                is_active=True,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "WordPress not connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from .tasks import start_wordpress_sync

        start_wordpress_sync(integration.id)
        return Response({"message": "Sync started."}, status=status.HTTP_202_ACCEPTED)


class WordPressDataView(APIView):
    """GET /api/integrations/wordpress/data/?email="""
    permission_classes = [AllowAny]

    def get(self, request):
        from datetime import timedelta
        from django.utils import timezone

        email = request.query_params.get("email", "").lower().strip()
        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org, err = _get_org_or_400(email)
        if err:
            return err

        try:
            integration = Integration.objects.get(
                organization=org,
                provider=Integration.Provider.WORDPRESS,
                is_active=True,
            )
        except Integration.DoesNotExist:
            return Response(
                {"error": "WordPress not connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        cutoff = timezone.now() - timedelta(days=90)
        integration.wordpress_snapshots.filter(created_at__lt=cutoff).delete()

        snapshot = integration.wordpress_snapshots.first()
        if not snapshot:
            return Response(
                {"error": "No data available. Trigger a sync first."},
                status=status.HTTP_404_NOT_FOUND,
            )

        stale_threshold = timezone.now() - timedelta(hours=24)
        if (
            snapshot.created_at < stale_threshold
            and snapshot.sync_status == "complete"
            and not integration.wordpress_snapshots.filter(sync_status="syncing").exists()
        ):
            from .tasks import start_wordpress_sync

            start_wordpress_sync(integration.id)

        serializer = WordPressDataSnapshotSerializer(snapshot)
        return Response(serializer.data)

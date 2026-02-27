import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Organization
from .serializers import OnboardSerializer, OrganizationSerializer

logger = logging.getLogger("apps")


class OnboardView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = OnboardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]

        if Organization.objects.filter(owner_email=email).exists():
            return Response(
                {"error": "An organization already exists for this email."},
                status=status.HTTP_409_CONFLICT,
            )

        org = serializer.save()
        logger.info("Organization created: %s for %s", org.name, email)

        return Response(
            OrganizationSerializer(org).data,
            status=status.HTTP_201_CREATED,
        )


class CheckOrganizationView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        email = request.query_params.get("email", "").lower().strip()

        if not email:
            return Response(
                {"error": "Email parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        exists = Organization.objects.filter(owner_email=email).exists()
        return Response({"exists": exists})

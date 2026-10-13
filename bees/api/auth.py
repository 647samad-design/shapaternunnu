"""Token sign-in for the REST API."""
from datetime import timedelta

from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from ..models import ApiToken

KEYWORDS = (b"bearer", b"token")


class TokenAuthentication(BaseAuthentication):
    """``Authorization: Bearer lm_...`` (``Token lm_...`` works too).

    Tokens are looked up by their SHA-256 hash; expired tokens and tokens of
    deactivated users are refused."""

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() not in KEYWORDS:
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Send the token as: Authorization: Bearer <token>")
        try:
            key = parts[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed("Invalid token.")
        token = ApiToken.objects.select_related("user").filter(key_hash=ApiToken.hash(key)).first()
        if token is None or token.is_expired:
            raise exceptions.AuthenticationFailed("Invalid or expired token. Sign in again with POST /api/v1/auth/token/.")
        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("This account is deactivated.")
        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at > timedelta(minutes=5):
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return token.user, token

    def authenticate_header(self, request):
        return 'Bearer realm="api"'


try:  # describe the scheme in the OpenAPI schema
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class TokenScheme(OpenApiAuthenticationExtension):
        target_class = "bees.api.auth.TokenAuthentication"
        name = "BearerToken"

        def get_security_definition(self, auto_schema):
            return {"type": "http", "scheme": "bearer",
                    "description": "Token from POST /api/v1/auth/token/, e.g. `Authorization: Bearer lm_...`"}
except ImportError:  # pragma: no cover
    pass

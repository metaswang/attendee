from django.contrib.auth.hashers import check_password
from rest_framework import authentication, exceptions

from .models import ApiKey


class ApiKeyAuthentication(authentication.BaseAuthentication):
    def authenticate_header(self, request):
        return "Token"

    def authenticate(self, request):
        if "Authorization" not in request.headers:
            raise exceptions.AuthenticationFailed({"detail": "Missing Authorization header"})

        auth_header = request.headers.get("Authorization", "").split()

        if not auth_header or len(auth_header) != 2 or auth_header[0].lower() != "token":
            raise exceptions.AuthenticationFailed({"detail": "Invalid Authorization header. Should have this format: Token <api_key>"})

        api_key = auth_header[1]

        api_key_obj = None
        for candidate in ApiKey.objects.select_related("project").filter(disabled_at__isnull=True):
            if check_password(api_key, candidate.key_hash):
                api_key_obj = candidate
                break
        if not api_key_obj:
            raise exceptions.AuthenticationFailed({"detail": "Invalid or disabled API key"})

        # Return (None, api_key_obj) instead of (user, auth)
        return (None, api_key_obj)

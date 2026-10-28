"""Authentication: operators (session cookie or bearer token) and machines (X-Device-Key)."""
import base64
import hashlib
import hmac
import os
import struct
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.core import signing
from django.utils import timezone
from rest_framework import authentication, exceptions, permissions

from .models import AppUser, UserRole

# ---------------------------------------------------------------- passwords
# The Users table holds ASP.NET Core Identity v3 hashes: base64(0x01 | prf | iterations | salt length | salt | subkey),
# big-endian uint32 fields. New hashes use the same format, so the .NET API can still verify them while both exist.
_PRF = {0: "sha1", 1: "sha256", 2: "sha512"}
_ITERATIONS, _SALT_BYTES, _KEY_BYTES = 100_000, 16, 32  # the .NET 8 defaults (PBKDF2-HMAC-SHA512)


def hash_password(raw: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    key = hashlib.pbkdf2_hmac("sha512", raw.encode(), salt, _ITERATIONS, _KEY_BYTES)
    return base64.b64encode(b"\x01" + struct.pack(">III", 2, _ITERATIONS, _SALT_BYTES) + salt + key).decode()


def verify_password(stored: str, raw: str) -> bool:
    try:
        blob = base64.b64decode(stored, validate=True)
        if len(blob) < 13 or blob[0] != 1:  # only v3 is ever written by Identity on .NET Core
            return False
        prf, iterations, salt_len = struct.unpack(">III", blob[1:13])
        salt, key = blob[13:13 + salt_len], blob[13 + salt_len:]
        if prf not in _PRF or salt_len < 16 or len(key) < 16:
            return False
    except (ValueError, struct.error):
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac(_PRF[prf], raw.encode(), salt, iterations, len(key)), key)


class EmailBackend(ModelBackend):
    """Login by e-mail (case-insensitive), as the ASP.NET API did. Role checks never touch permission tables."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        try:
            user = AppUser.objects.get_by_natural_key(username or "")
        except AppUser.DoesNotExist:
            hash_password(password or "")  # same cost either way, so timing does not reveal which e-mails exist
            return None
        return user if user.check_password(password or "") and user.is_active else None

    def get_user_permissions(self, user_obj, obj=None):
        return set()

    def get_group_permissions(self, user_obj, obj=None):
        return set()


# ---------------------------------------------------------------- API bearer tokens (scripts such as ai-service/tools/live_test.py)
TOKEN_SALT = "sentinel.api-token"


def issue_token(user: AppUser) -> tuple[str, "timezone.datetime"]:
    expires = timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE)
    token = signing.dumps({"uid": user.pk, "pw": user.get_session_auth_hash()}, salt=TOKEN_SALT)  # an HMAC: changes with the password
    return token, expires


class BearerTokenAuthentication(authentication.BaseAuthentication):
    """'Authorization: Bearer <token>' from POST /api/auth/login. Expires like the session; a password change revokes it."""

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        try:
            data = signing.loads(header[7:], salt=TOKEN_SALT, max_age=settings.SESSION_COOKIE_AGE)
            user = AppUser.objects.get(pk=data["uid"], is_active=True)
        except (signing.BadSignature, AppUser.DoesNotExist, KeyError, TypeError):
            raise exceptions.AuthenticationFailed("Invalid or expired token.")
        if not hmac.compare_digest(user.get_session_auth_hash(), str(data.get("pw", ""))):
            raise exceptions.AuthenticationFailed("Invalid or expired token.")
        return user, None

    def authenticate_header(self, request):
        return "Bearer"


# ---------------------------------------------------------------- devices (AI service, IoT gateways, simulator)
class Device:
    """The identity of a machine that presented the shared device key."""
    is_authenticated = True
    is_device = True
    email = "device"
    role = "Device"
    pk = None


def device_key_matches(presented: str) -> bool:
    configured = settings.SENTINEL["DEVICE_API_KEY"]
    return bool(configured) and bool(presented) and hmac.compare_digest(presented.encode(), configured.encode())


class DeviceKeyAuthentication(authentication.BaseAuthentication):
    """Header X-Device-Key. With no key configured, device access is disabled."""

    def authenticate(self, request):
        presented = request.headers.get("X-Device-Key")
        if not presented:
            return None
        if not device_key_matches(presented):
            raise exceptions.AuthenticationFailed("Invalid device key.")
        return Device(), None

    def authenticate_header(self, request):
        return "DeviceKey"


def is_device(user) -> bool:
    return getattr(user, "is_device", False)


def is_operator(user) -> bool:
    return isinstance(user, AppUser) and user.is_active and user.role in (UserRole.ADMIN, UserRole.OPERATOR)


def _people_only(user) -> None:
    """Operator/Admin endpoints never accept a device key: 401, as the ASP.NET policies (JWT scheme only) answered."""
    if is_device(user):
        raise exceptions.NotAuthenticated("This endpoint needs an operator login, not a device key.")


class IsOperator(permissions.BasePermission):
    """Logged-in dashboard user with role Admin or Operator (the old "Operator" policy)."""

    def has_permission(self, request, view):
        _people_only(request.user)
        return is_operator(request.user)


class IsAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        _people_only(request.user)
        return is_operator(request.user) and request.user.role == UserRole.ADMIN


class IsOperatorOrDevice(permissions.BasePermission):
    def has_permission(self, request, view):
        return is_operator(request.user) or is_device(request.user)


def actor(request) -> str:
    """Who performed an action, for activity messages: the operator's e-mail, or "device"."""
    return getattr(request.user, "email", "unknown")

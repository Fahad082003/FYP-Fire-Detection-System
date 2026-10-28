"""FireVision AI (Sentinel) Django settings.

Configuration comes from environment variables in the ASP.NET-style "Section__Key" format, so the project's
existing .env file keeps working unchanged. Real environment variables always win over values in .env.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv() -> Path | None:
    """KEY=VALUE pairs from the nearest .env (this folder or up to three parents) into os.environ; set variables win."""
    for folder in [BASE_DIR, *BASE_DIR.parents][:4]:
        path = folder / ".env"
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))
        return path
    return None


# Tests never read .env: it can hold real Twilio/SMTP credentials (see tests/README in the docs).
ENV_FILE = None if os.environ.get("SENTINEL_NO_DOTENV") == "1" else load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool) -> bool:
    value = os.environ.get(key)
    return default if value is None or value == "" else value.strip().lower() in ("1", "true", "yes", "on")


def env_number(key: str, default: float) -> float:
    value = os.environ.get(key)
    return default if value is None or value == "" else float(value)


DEBUG = env_bool("SENTINEL_DEBUG", False)

# Signs session cookies and API bearer tokens. The .env name is kept from the ASP.NET version.
SECRET_KEY = env("Jwt__SigningKey")
if len(SECRET_KEY) < 32:
    if not DEBUG:
        raise RuntimeError("Jwt__SigningKey must be set (at least 32 characters). For a throwaway dev run set SENTINEL_DEBUG=true.")
    SECRET_KEY = "dev-only-insecure-key-" + "x" * 32

ALLOWED_HOSTS = [h for h in env("SENTINEL_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.auth",          # login/logout/session hashing for the existing Users table
    "django.contrib.contenttypes",  # required by django.contrib.auth
    "django.contrib.sessions",
    "django.contrib.messages",  # one-line confirmations after a form post (e.g. "Camera saved")
    "django.contrib.staticfiles",
    "rest_framework",
    "sentinel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
APPEND_SLASH = False  # API paths are kept exactly as the ASP.NET API had them (no trailing slash)

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

ASGI_APPLICATION = "config.asgi.application"


def sql_server(connection_string: str) -> dict:
    """ADO.NET connection string (the ConnectionStrings__Sentinel value used by the .NET API) -> Django DATABASES entry."""
    parts = {}
    for item in connection_string.split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            parts[key.strip().lower()] = value.strip()
    yes = ("true", "yes", "sspi")
    extra = ["TrustServerCertificate=yes" if parts.get("trustservercertificate", "").lower() in yes else "",
             "Trusted_Connection=yes" if parts.get("trusted_connection", parts.get("integrated security", "")).lower() in yes else ""]
    return {
        "ENGINE": "mssql",
        "NAME": parts.get("database", parts.get("initial catalog", "SentinelDb")),
        "HOST": parts.get("server", parts.get("data source", "localhost")),
        "USER": parts.get("user id", parts.get("uid", "")),
        "PASSWORD": parts.get("password", parts.get("pwd", "")),
        "OPTIONS": {"driver": env("SENTINEL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
                    "extra_params": ";".join(e for e in extra if e)},
        # Tests build a throwaway database from schema/sentinel.sql (see sentinel/test_runner.py).
        "TEST": {"NAME": env("SENTINEL_TEST_DB", "test_SentinelDb_django")},
    }


DATABASES = {"default": sql_server(env(
    "ConnectionStrings__Sentinel", "Server=localhost;Database=SentinelDb;Trusted_Connection=True;TrustServerCertificate=True"))}
DATABASE_ROUTERS = ["sentinel.routers.NoMigrationsOnLiveDatabase"]
TEST_RUNNER = "sentinel.test_runner.SchemaTestRunner"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

AUTH_USER_MODEL = "sentinel.AppUser"
AUTHENTICATION_BACKENDS = ["sentinel.security.EmailBackend"]
LOGIN_URL = "login"
# Signed cookies need no session table, so the existing database gets no Django tables at all.
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_AGE = int(env_number("Jwt__ExpiryMinutes", 480)) * 60  # same lifetime as the old JWT
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Behind HTTPS (the .NET API's Production mode): secure cookies, HTTP -> HTTPS redirect and HSTS.
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT = env_bool("SENTINEL_HTTPS", False)
SECURE_HSTS_SECONDS = 31_536_000 if SECURE_SSL_REDIRECT else 0
X_FRAME_OPTIONS = "DENY"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True  # stored as UTC datetime2, exactly like EF Core did

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "sentinel.security.DeviceKeyAuthentication",
        "sentinel.security.BearerTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["sentinel.security.IsOperator"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser", "rest_framework.parsers.MultiPartParser"],
    "EXCEPTION_HANDLER": "sentinel.api.problem_details",
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_THROTTLE_RATES": {  # "requests/seconds" (see sentinel.api.WindowThrottle)
        "login": "10/60",
        "ai-analyze": f"{int(env_number('RateLimits__AiAnalyze__PermitLimit', 10))}/{int(env_number('RateLimits__AiAnalyze__WindowSeconds', 60))}",
    },
}

# In-process cache: rate limits, alert throttles, deleted-camera memory (same scope as the .NET IMemoryCache).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Email: an empty SMTP host means "console mode" (the alert is written to the log), as before.
EMAIL_HOST = env("Notifications__Email__SmtpHost")
EMAIL_PORT = int(env_number("Notifications__Email__SmtpPort", 587))
EMAIL_USE_TLS = env_bool("Notifications__Email__UseStartTls", True)
EMAIL_HOST_USER = env("Notifications__Email__Username")
EMAIL_HOST_PASSWORD = env("Notifications__Email__Password")
EMAIL_TIMEOUT = 20

# Application options (names and defaults kept from the former ASP.NET appsettings.json).
SENTINEL = {
    "DEVICE_API_KEY": env("Device__ApiKey"),
    "SEED_DEMO_DATA": env_bool("Seed__DemoData", DEBUG),
    "SEED_ADMIN_EMAIL": env("Seed__AdminEmail", "admin@sentinel.local"),
    "SEED_ADMIN_PASSWORD": env("Seed__AdminPassword"),
    "COOLDOWN_SECONDS": env_number("Alerts__CooldownSeconds", 120),
    "INCIDENT_MERGE_WINDOW_SECONDS": env_number("Alerts__IncidentMergeWindowSeconds", 90),
    "CAMERA_LOST_SECONDS": env_number("Alerts__CameraLostSeconds", 10),
    "CAMERA_STARTUP_GRACE_SECONDS": env_number("Alerts__CameraStartupGraceSeconds", 60),
    "VISION_EVALUATION_INTERVAL_SECONDS": env_number("Alerts__VisionEvaluationIntervalSeconds", 2),
    "AUTO_SPRINKLER_ON_SIMULATED_DATA": env_bool("Alerts__AutoSprinklerOnSimulatedData", DEBUG),
    "SMOKE_CRITICAL_MULTIPLIER": env_number("Alerts__SmokeCriticalMultiplier", 1.5),
    "TEMPERATURE_CRITICAL_MULTIPLIER": env_number("Alerts__TemperatureCriticalMultiplier", 1.25),
    "WARNING_RATIO": env_number("Alerts__WarningRatio", 0.8),
    "SENSOR_HISTORY_POINTS": int(env_number("Alerts__SensorHistoryPoints", 60)),
    "SENSOR_HISTORY_RETENTION_HOURS": env_number("Alerts__SensorHistoryRetentionHours", 48),
    "SITE_NAME": env("Alerts__SiteName", "FireVision AI"),
    "DASHBOARD_URL": env("Alerts__DashboardUrl", "http://localhost:5291/"),
    "SMS_ACCOUNT_SID": env("Notifications__Sms__TwilioAccountSid"),
    "SMS_AUTH_TOKEN": env("Notifications__Sms__TwilioAuthToken"),
    "SMS_FROM": env("Notifications__Sms__FromNumber"),
    "SMS_RECIPIENTS": env("Notifications__Sms__Recipients"),
    "EMAIL_FROM": env("Notifications__Email__From", "alerts@sentinel.local"),
    "EMAIL_RECIPIENTS": env("Notifications__Email__Recipients"),
    "WEBPUSH_PUBLIC_KEY": env("Notifications__WebPush__PublicKey"),
    "WEBPUSH_PRIVATE_KEY": env("Notifications__WebPush__PrivateKey"),
    "WEBPUSH_SUBJECT": env("Notifications__WebPush__Subject", "mailto:ops@sentinel.local"),
    "AI_BASE_URL": env("AiService__BaseUrl", "http://localhost:8008"),
    "AI_TIMEOUT_SECONDS": env_number("AiService__TimeoutSeconds", 20),
    "AI_MAX_IMAGE_BYTES": int(env_number("AiService__MaxImageBytes", 5 * 1024 * 1024)),
    "WALKING_SPEED": env_number("FloorPlans__WalkingSpeedMetersPerSecond", 1.2),
    "DANGER_CLEARANCE_METERS": env_number("FloorPlans__DangerClearanceMeters", 1.0),
    "FLOORPLAN_IMAGE_DIR": BASE_DIR / env("FloorPlans__ImageDirectory", "App_Data/floorplans"),
    "FLOORPLAN_MAX_IMAGE_BYTES": int(env_number("FloorPlans__MaxImageBytes", 5 * 1024 * 1024)),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"sentinel": {"handlers": ["console"], "level": "INFO"}},
}

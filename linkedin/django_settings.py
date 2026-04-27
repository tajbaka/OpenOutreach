# linkedin/django_settings.py
"""
Minimal Django settings for using DjangoCRM's ORM + admin.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Playwright's sync API runs inside an async event loop, which triggers
# Django's async-safety check. We only use the ORM synchronously, so this is safe.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

ROOT_DIR = Path(__file__).resolve().parent.parent

# Load .env early so DATABASE_URL is available when DATABASES is computed below.
load_dotenv(ROOT_DIR / ".env")

BASE_DIR = ROOT_DIR

SECRET_KEY = "openoutreach-local-dev-key-change-in-production"

DEBUG = True

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.sites",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "crm.apps.CrmConfig",
    "chat.apps.ChatConfig",
    "linkedin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "linkedin.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Database: Postgres via DATABASE_URL (Neon). DATABASE_URL is required for
# every non-test runtime — no SQLite fallback. The daemon and dev tooling
# must point at the same DATABASE_URL to avoid split-brain.
# Tests get an in-memory SQLite (detected via pytest in sys.modules).
_database_url = os.environ.get("DATABASE_URL", "").strip()
if _database_url:
    import dj_database_url
    DATABASES = {
        "default": dj_database_url.parse(
            _database_url,
            conn_max_age=600,
            ssl_require=True,
        ),
    }
elif "pytest" in sys.modules:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it in .env to a Postgres connection "
        "string (e.g. from Neon). The SQLite fallback was removed to "
        "prevent silent split-brain — see linkedin/django_settings.py."
    )

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SITE_ID = 1

STATIC_URL = "/static/"
STATIC_ROOT = ROOT_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = ROOT_DIR / "media"

LOGIN_URL = "/admin/login/"

DEFAULT_FROM_EMAIL = "noreply@localhost"
EMAIL_SUBJECT_PREFIX = "CRM: "

LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English")]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

TESTING = sys.argv[1:2] == ["test"]

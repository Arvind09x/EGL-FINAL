"""
Django settings for the ESHTO Games Library project.
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# Paths & env
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-fallback-key")
DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")
ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

# ──────────────────────────────────────────────
# Application definition
# ──────────────────────────────────────────────
INSTALLED_APPS = [
    # Django built-in
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # Third-party
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "django_htmx",
    "django_extensions",
    # Project apps
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Third-party
    "allauth.account.middleware.AccountMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "eshto.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

WSGI_APPLICATION = "eshto.wsgi.application"

# ──────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

# ──────────────────────────────────────────────
# Custom User model (must be set before first migrate)
# ──────────────────────────────────────────────
AUTH_USER_MODEL = "core.User"

# ──────────────────────────────────────────────
# Password validation
# ──────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ──────────────────────────────────────────────
# Authentication backends (allauth)
# ──────────────────────────────────────────────
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

SITE_ID = 1

# allauth configuration
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_EMAIL_VERIFICATION = "none"  # Google already verifies
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
        "APP": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        },
    }
}
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = True

ACCOUNT_ADAPTER = "core.adapters.AshokaAccountAdapter"
SOCIALACCOUNT_ADAPTER = "core.adapters.AshokaSocialAccountAdapter"

LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# ──────────────────────────────────────────────
# Internationalization & timezone
# ──────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

# ──────────────────────────────────────────────
# Static & media files
# ──────────────────────────────────────────────
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ──────────────────────────────────────────────
# Default primary key field type
# ──────────────────────────────────────────────
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ──────────────────────────────────────────────
# Testing / Admin override
# ──────────────────────────────────────────────
# Add any Ashoka emails here. When they log in via Google, they will be
# automatically promoted to a senior admin/superuser.
# WARNING: Clear this list before production deployment!
TEST_SUPERUSERS = [
    "arvindsrinivas.sudarsan_ug2023@ashoka.edu.in",
]

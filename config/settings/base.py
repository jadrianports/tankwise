"""
Django settings for the Fuel Route Optimizer project.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-secret-key-do-not-use-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True") == "True"

ALLOWED_HOSTS = ["*"] if DEBUG else []

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "routing",
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

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Database
# Discrete DB_* env vars with a SQLite default (D-21). Deliberately not using
# a DATABASE_URL parser — Django has no native support for one, and adding a
# third-party parser (e.g. dj-database-url) is an unneeded dependency here.
DB_ENGINE = os.environ.get("DB_ENGINE", "django.db.backends.sqlite3")

if DB_ENGINE == "django.db.backends.sqlite3":
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": os.environ.get("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": os.environ.get("DB_NAME", ""),
            "HOST": os.environ.get("DB_HOST", ""),
            "USER": os.environ.get("DB_USER", ""),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "PORT": os.environ.get("DB_PORT", ""),
        }
    }

# Mapbox / corridor
# No default for MAPBOX_TOKEN -- an unset token must stay falsy None so the
# client can raise a clear config error, rather than silently defaulting to
# an empty string.
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN")
CORRIDOR_ROOFTOP_MI = os.environ.get("CORRIDOR_ROOFTOP_MI", "5")
CORRIDOR_CITY_MI = os.environ.get("CORRIDOR_CITY_MI", "20")

# Cache
# CACHE_BACKEND selects "redis" (django-redis, for the containerized demo)
# or "locmem" (default, keeps a fresh clone's runserver/tests working with
# zero Redis dependency). Branches BACKEND itself, not just LOCATION, so an
# unset/local env never attempts a Redis connection.
CACHE_BACKEND = os.environ.get("CACHE_BACKEND", "locmem")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "86400"))

if CACHE_BACKEND == "redis":
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "TIMEOUT": CACHE_TTL_SECONDS,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "TIMEOUT": CACHE_TTL_SECONDS,
        }
    }

# Django REST Framework
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "routing.exceptions.custom_exception_handler",
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

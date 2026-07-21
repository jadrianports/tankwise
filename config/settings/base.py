"""
Django settings for the Fuel Route Optimizer project.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _env(name, default=None):
    """Read an env var, treating a present-but-empty value the same as absent,
    so a blank line in a copied `.env` (e.g. `CACHE_TTL_SECONDS=`) falls back to
    the default instead of breaking int()/split() coercion downstream."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


SECRET_KEY = _env(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-secret-key-do-not-use-in-production",
)

DEBUG = _env("DJANGO_DEBUG", "True") == "True"

# Allowed hosts
# Read independently of DEBUG so a DEBUG=False deploy (gunicorn behind Nginx
# in Docker) still answers proxied requests instead of rejecting them with
# DisallowedHost. Comma-separated; permissive "*" default is fine for this
# local single-reviewer demo.
DJANGO_ALLOWED_HOSTS = _env("DJANGO_ALLOWED_HOSTS", "*")
ALLOWED_HOSTS = [h.strip() for h in DJANGO_ALLOWED_HOSTS.split(",") if h.strip()]

INSTALLED_APPS = [
    # Must precede the contrib apps so local `runserver` exercises the same
    # WhiteNoise code path as production instead of Django's own static
    # handler (WhiteNoise everywhere, no nginx sidecar).
    "whitenoise.runserver_nostatic",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # OpenAPI 3 schema generation for /api/schema, /api/docs, /api/redoc --
    # its Swagger/ReDoc UI assets are CDN-hosted by default, so this app
    # never touches collectstatic/WhiteNoise.
    "drf_spectacular",
    "routing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Must sit immediately after SecurityMiddleware -- WhiteNoise's own
    # documented required position -- so it intercepts static/SPA requests
    # before session, auth and CSRF middleware run.
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
# Discrete DB_* env vars with a SQLite default -- no DATABASE_URL
# parser, since Django has no native support and dj-database-url is an
# unneeded dependency here.
DB_ENGINE = _env("DB_ENGINE", "django.db.backends.sqlite3")

if DB_ENGINE == "django.db.backends.sqlite3":
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": _env("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        }
    }
else:
    # DB_HOST points at Neon's pooled `-pooler` endpoint for the running app
    # (PgBouncer already pools connections, so CONN_MAX_AGE defaults to 0 --
    # stacking Django's own persistent connections on top would be a second,
    # redundant pooling layer). entrypoint.sh's migrate step instead targets
    # DB_MIGRATE_HOST, the direct (non-pooled) endpoint, because Neon's own
    # docs flag transaction-mode pooling as error-prone for schema migrations.
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": _env("DB_NAME", ""),
            "HOST": _env("DB_HOST", ""),
            "USER": _env("DB_USER", ""),
            "PASSWORD": _env("DB_PASSWORD", ""),
            "PORT": _env("DB_PORT", ""),
            "OPTIONS": {"sslmode": _env("DB_SSLMODE", "require")},
            "CONN_MAX_AGE": int(_env("DB_CONN_MAX_AGE", "0")),
            "CONN_HEALTH_CHECKS": _env("DB_CONN_HEALTH_CHECKS", "True") == "True",
        }
    }

# Mapbox / corridor
# No default for MAPBOX_TOKEN -- an unset token must stay falsy None so the
# client can raise a clear config error, rather than silently defaulting to
# an empty string.
MAPBOX_TOKEN = _env("MAPBOX_TOKEN")
# The public (pk.*) token used only to build map_url -- never the secret
# above. Same no-default treatment so an unset value stays falsy None.
MAPBOX_PUBLIC_TOKEN = _env("MAPBOX_PUBLIC_TOKEN")
CORRIDOR_ROOFTOP_MI = _env("CORRIDOR_ROOFTOP_MI", "5")
CORRIDOR_CITY_MI = _env("CORRIDOR_CITY_MI", "20")

# Fuel price dataset vintage (VEH-08). A constant, not a Station column
# -- the source CSV has one vintage and no per-row dates, so a column
# would store thousands of identical copies plus a migration and a
# reseed for zero added information, while a constant survives a
# future Postgres migration untouched. Estimated by cross-referencing
# the dataset's own price statistics (mean $3.499/gal, median $3.432,
# min $2.687, max $6.399) against EIA's historical weekly on-highway
# diesel series: the California high near $6.40 rules out 2021 and
# earlier, and the ~$3.50 national-equivalent mean rules out most of
# 2022-2023, leaving late 2024-early 2025 as the best-fit window.
FUEL_PRICE_AS_OF = _env("FUEL_PRICE_AS_OF", "2025-01-01")
FUEL_PRICE_DATA_NOTE = _env(
    "FUEL_PRICE_DATA_NOTE",
    "Fuel prices come from a static OPIS truck-stop snapshot with no "
    "per-row timestamp. Price levels are consistent with U.S. retail "
    "diesel of late 2024-early 2025: the dataset mean of ~$3.50/gal "
    "matches EIA's on-highway national average for that window, and "
    "California outliers up to $6.40 confirm a post-2022 vintage. "
    "This is a dataset vintage, not a live quote.",
)

# Cache
# CACHE_BACKEND selects "redis" (django-redis, containerized demo) or
# "locmem" (default, keeps a fresh clone's runserver/tests working with zero
# Redis dependency). Branches BACKEND itself so an unset/local env never
# attempts a Redis connection.
CACHE_BACKEND = _env("CACHE_BACKEND", "locmem")
REDIS_URL = _env("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_SECONDS = int(_env("CACHE_TTL_SECONDS", "86400"))

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
# Throttle rates are deliberately generous defaults -- a recruiter clicking
# demo chips and the frontend's debounced what-if sliders must never trip
# them, while a script cannot drain the Mapbox token budget. Both are
# env-tunable so they can be retuned from the Render dashboard without a
# redeploy.
# NUM_PROXIES=1 trusts exactly one hop, matching Render's single edge proxy.
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "routing.exceptions.custom_exception_handler",
    "DEFAULT_THROTTLE_RATES": {
        "route_burst": _env("ROUTE_THROTTLE_BURST_RATE", "20/min"),
        "route_sustained": _env("ROUTE_THROTTLE_SUSTAINED_RATE", "200/day"),
    },
    "NUM_PROXIES": int(_env("NUM_PROXIES", "1")),
    # Lets drf-spectacular introspect every view for /api/schema
    # instead of DRF's default CoreAPI-based schema generator.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# drf-spectacular -- Swagger UI at /api/docs, ReDoc at /api/redoc,
# raw schema at /api/schema. SERVE_INCLUDE_SCHEMA is False so /api/schema
# doesn't recursively document itself. SWAGGER_UI_DIST/REDOC_DIST are left
# unset (CDN defaults) on purpose -- self-hosting them would pull their
# static assets through collectstatic/WhiteNoise's CompressedManifestStorage
# for zero benefit on a public $0 demo.
SPECTACULAR_SETTINGS = {
    "TITLE": "TankWise API",
    "DESCRIPTION": "Cost-optimal fuel-stop routing for the continental US.",
    "VERSION": "2.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_SETTINGS": {"deepLinking": True},
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
STATIC_ROOT = BASE_DIR / "staticfiles"

# The built SPA's directory (the SPA owns the root, backend stays under
# /api/, collectstatic output serves at /static/). WhiteNoise's own docs
# caution WHITENOISE_ROOT against bulk static files because it applies no
# cache versioning -- but Vite content-hashes every emitted asset filename
# (e.g. index-a1b2c3.js), so cache-busting already comes from the filename
# itself, making WHITENOISE_ROOT the right tool for this specific job.
WHITENOISE_ROOT = BASE_DIR / "frontend" / "dist"
WHITENOISE_INDEX_FILE = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

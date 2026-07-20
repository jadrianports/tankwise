"""Production settings for the Fuel Route Optimizer project.

Selected via DJANGO_SETTINGS_MODULE=config.settings.production. This module
exists so production hardening (HTTPS enforcement, secure cookies, explicit
ALLOWED_HOSTS) cannot leak into local dev and vice versa -- base.py remains
the local default, permissive on purpose for a zero-setup reviewer clone.
"""

from config.settings.base import *  # noqa: F401,F403
from config.settings.base import _env, BASE_DIR  # noqa: F401

DEBUG = False

# Render terminates TLS at its edge and forwards the original scheme via
# this header; Django must read it to correctly detect HTTPS behind the proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Render's health check accepts any 2xx or 3xx response as healthy, so a
# 301 from this redirect on the internal plaintext check still passes the
# deploy gate -- no redirect exemption for /api/ready or /api/health is
# configured or needed.
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = int(_env("SECURE_HSTS_SECONDS", "31536000"))  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True

# Explicit ALLOWED_HOSTS -- a wildcard is never permitted here (Host-header
# injection / cache-poisoning class of attack). Render auto-injects
# RENDER_EXTERNAL_HOSTNAME for every web service, so the assigned
# onrender.com hostname is appended at runtime rather than hardcoded.
ALLOWED_HOSTS = [h.strip() for h in _env("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()]
_render_host = _env("RENDER_EXTERNAL_HOSTNAME")
if _render_host and _render_host not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_host)

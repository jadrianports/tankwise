"""Tests for `config/settings/production.py` -- the hardened deploy profile.

Imports the module directly via importlib with a controlled environment
rather than switching DJANGO_SETTINGS_MODULE mid-suite, since the rest of
this test run stays on the default (SQLite/local) settings module.
"""
import importlib
import os
import sys
from unittest import mock

from django.test import SimpleTestCase


def _import_fresh(module_name, env_overrides):
    """(Re-)import a settings module with a controlled environment, forcing
    both it and config.settings.base to re-execute their module-level code
    under the given env vars rather than reusing whatever was cached when
    the test process started."""
    for name in ("config.settings.production", "config.settings.base"):
        sys.modules.pop(name, None)
    with mock.patch.dict(os.environ, env_overrides, clear=False):
        return importlib.import_module(module_name)


class ProductionSettingsSecurityTests(SimpleTestCase):
    def setUp(self):
        self.settings = _import_fresh(
            "config.settings.production",
            {"DJANGO_ALLOWED_HOSTS": "example.onrender.com"},
        )

    def test_debug_is_false(self):
        self.assertFalse(self.settings.DEBUG)

    def test_ssl_redirect_enabled(self):
        self.assertTrue(self.settings.SECURE_SSL_REDIRECT)

    def test_hsts_seconds_is_positive_int(self):
        self.assertIsInstance(self.settings.SECURE_HSTS_SECONDS, int)
        self.assertGreater(self.settings.SECURE_HSTS_SECONDS, 0)

    def test_hsts_preload_and_subdomains_enabled(self):
        self.assertTrue(self.settings.SECURE_HSTS_PRELOAD)
        self.assertTrue(self.settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)

    def test_cookies_secure_and_httponly(self):
        self.assertTrue(self.settings.SESSION_COOKIE_SECURE)
        self.assertTrue(self.settings.CSRF_COOKIE_SECURE)
        self.assertTrue(self.settings.SESSION_COOKIE_HTTPONLY)

    def test_secure_proxy_ssl_header(self):
        self.assertEqual(
            self.settings.SECURE_PROXY_SSL_HEADER,
            ("HTTP_X_FORWARDED_PROTO", "https"),
        )

    def test_allowed_hosts_has_no_wildcard(self):
        self.assertNotIn("*", self.settings.ALLOWED_HOSTS)


class ProductionSettingsAllowedHostsTests(SimpleTestCase):
    def test_render_external_hostname_appended(self):
        settings = _import_fresh(
            "config.settings.production",
            {
                "DJANGO_ALLOWED_HOSTS": "example.onrender.com",
                "RENDER_EXTERNAL_HOSTNAME": "tankwise.onrender.com",
            },
        )

        self.assertIn("tankwise.onrender.com", settings.ALLOWED_HOSTS)


class ProductionSettingsDatabaseTests(SimpleTestCase):
    def test_postgres_engine_carries_sslmode_option(self):
        settings = _import_fresh(
            "config.settings.production",
            {
                "DJANGO_ALLOWED_HOSTS": "example.onrender.com",
                "DB_ENGINE": "django.db.backends.postgresql",
                "DB_HOST": "db.neon.tech",
                "DB_NAME": "tankwise",
                "DB_USER": "tankwise",
                "DB_PASSWORD": "secret",
            },
        )

        self.assertIn("OPTIONS", settings.DATABASES["default"])
        self.assertIn("sslmode", settings.DATABASES["default"]["OPTIONS"])

    def test_unset_db_engine_stays_sqlite(self):
        env = {"DJANGO_ALLOWED_HOSTS": "example.onrender.com"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DB_ENGINE", None)
            settings = _import_fresh("config.settings.production", env)

        self.assertEqual(
            settings.DATABASES["default"]["ENGINE"],
            "django.db.backends.sqlite3",
        )

"""Tests for `GET /api/ready` -- the dependency-aware readiness probe
Render gates traffic routing on (D-16..D-19).

Mirrors `test_health.py`'s `APITestCase` shape. `MAPBOX_TOKEN`/
`MAPBOX_PUBLIC_TOKEN` are overridden per-test class/method so each
scenario controls the token check independently of whatever the test
settings module happens to have configured.
"""
from unittest import mock

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

READY_URL = "/api/ready"

VALID_SECRET_TOKEN = "sk.fake-secret-token-should-never-leak"
VALID_PUBLIC_TOKEN = "pk.fake-public-token-should-never-leak"
DB_EXCEPTION_MESSAGE = "db down: host=secret-host.internal user=admin"


@override_settings(
    MAPBOX_TOKEN=VALID_SECRET_TOKEN, MAPBOX_PUBLIC_TOKEN=VALID_PUBLIC_TOKEN
)
class ReadyHealthyTests(APITestCase):
    def test_all_checks_pass_returns_200_with_full_body_shape(self):
        response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ready")
        self.assertEqual(set(response.data["checks"].keys()), {"db", "cache", "tokens"})
        self.assertTrue(response.data["checks"]["db"])
        self.assertTrue(response.data["checks"]["cache"])
        self.assertTrue(response.data["checks"]["tokens"])

    def test_resolves_under_api_prefix(self):
        response = self.client.get(READY_URL)

        self.assertNotEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_empty_station_table_returns_200_with_zero_station_count(self):
        response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["station_count"], 0)


@override_settings(
    MAPBOX_TOKEN=VALID_SECRET_TOKEN, MAPBOX_PUBLIC_TOKEN=VALID_PUBLIC_TOKEN
)
class ReadyDbFailureTests(APITestCase):
    def test_db_failure_returns_503_with_db_false_and_null_station_count(self):
        with mock.patch(
            "routing.views.connection.cursor",
            side_effect=Exception(DB_EXCEPTION_MESSAGE),
        ):
            response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(response.data["checks"]["db"])
        self.assertIsNone(response.data["station_count"])
        # Other checks still report their own real state, per D-16.
        self.assertTrue(response.data["checks"]["cache"])
        self.assertTrue(response.data["checks"]["tokens"])

    def test_db_failure_body_excludes_exception_message_and_tokens(self):
        with mock.patch(
            "routing.views.connection.cursor",
            side_effect=Exception(DB_EXCEPTION_MESSAGE),
        ):
            response = self.client.get(READY_URL)

        body = str(response.data)
        self.assertNotIn("secret-host", body)
        self.assertNotIn(DB_EXCEPTION_MESSAGE, body)
        self.assertNotIn(VALID_SECRET_TOKEN, body)
        self.assertNotIn(VALID_PUBLIC_TOKEN, body)


@override_settings(
    MAPBOX_TOKEN=VALID_SECRET_TOKEN, MAPBOX_PUBLIC_TOKEN=VALID_PUBLIC_TOKEN
)
class ReadyCacheFailureTests(APITestCase):
    def test_cache_failure_returns_503_with_cache_false(self):
        with mock.patch(
            "routing.views.cache.set", side_effect=Exception("cache boom")
        ):
            response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(response.data["checks"]["cache"])
        self.assertTrue(response.data["checks"]["db"])
        self.assertTrue(response.data["checks"]["tokens"])


class ReadyTokenFailureTests(APITestCase):
    @override_settings(MAPBOX_TOKEN=None, MAPBOX_PUBLIC_TOKEN=VALID_PUBLIC_TOKEN)
    def test_missing_secret_token_returns_503_with_tokens_false(self):
        response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(response.data["checks"]["tokens"])

    @override_settings(MAPBOX_TOKEN=VALID_SECRET_TOKEN, MAPBOX_PUBLIC_TOKEN=None)
    def test_missing_public_token_returns_503_with_tokens_false(self):
        response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(response.data["checks"]["tokens"])

    @override_settings(
        MAPBOX_TOKEN=VALID_SECRET_TOKEN, MAPBOX_PUBLIC_TOKEN="sk.wrong-prefix-token"
    )
    def test_non_pk_prefixed_public_token_returns_503_with_tokens_false(self):
        response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(response.data["checks"]["tokens"])

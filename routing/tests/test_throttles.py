"""Tests for `POST /api/route` rate limiting (stacked burst + sustained
throttling, D-12..D-15).

DRF stores throttle history in the default cache, so `setUp` clears it
each test to prevent bucket state leaking across tests/prior requests.
Every test posts a deliberately invalid body (missing "start"/"finish")
so an allowed request returns 400 without ever reaching Mapbox --
throttle history is recorded during `check_throttles()`, before the view
body runs, so an invalid body still consumes the bucket.

Rate overrides use `mock.patch.object(SimpleRateThrottle, "THROTTLE_RATES", ...)`
rather than `override_settings(REST_FRAMEWORK=...)` alone: DRF binds
`SimpleRateThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES`
as a plain class attribute once, at `rest_framework/throttling.py`'s
import time. `override_settings`'s `setting_changed` signal only resets
`api_settings`'s own cached-attribute lookup, not this already-bound
class attribute, so it alone cannot tighten the effective rate inside a
test -- the real `ROUTE_THROTTLE_BURST_RATE`/`_SUSTAINED_RATE` defaults
from `config/settings/base.py` would still apply. Patching the class
attribute directly is the reliable way to exercise a tightened limit.
"""
from unittest import mock

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

ROUTE_URL = "/api/route"
HEALTH_URL = "/api/health"
READY_URL = "/api/ready"

MOCK_GET_ROUTES = "routing.views.get_routes"


def _tight_rates(burst_rate, sustained_rate="1000/day"):
    return mock.patch.object(
        SimpleRateThrottle,
        "THROTTLE_RATES",
        {"route_burst": burst_rate, "route_sustained": sustained_rate},
    )


class RouteThrottleTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_request_past_burst_limit_returns_429(self):
        with _tight_rates("2/min"):
            self.client.post(ROUTE_URL, {}, format="json")
            self.client.post(ROUTE_URL, {}, format="json")

            response = self.client.post(ROUTE_URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_429_carries_retry_after_header(self):
        with _tight_rates("1/min"):
            self.client.post(ROUTE_URL, {}, format="json")

            response = self.client.post(ROUTE_URL, {}, format="json")

        self.assertIn("Retry-After", response)

    def test_429_body_has_rate_limited_envelope(self):
        with _tight_rates("1/min"):
            self.client.post(ROUTE_URL, {}, format="json")

            response = self.client.post(ROUTE_URL, {}, format="json")

        self.assertEqual(response.data["error"]["code"], "rate_limited")
        retry_after = response.data["error"]["detail"]["retry_after_s"]
        self.assertIsNotNone(retry_after)
        float(retry_after)  # raises if not numeric ("integer-ish")

    def test_health_and_ready_unaffected_after_route_bucket_exhausted(self):
        with _tight_rates("1/min"):
            self.client.post(ROUTE_URL, {}, format="json")
            self.client.post(ROUTE_URL, {}, format="json")  # exhausts the bucket

            health_response = self.client.get(HEALTH_URL)
            ready_response = self.client.get(READY_URL)

        self.assertEqual(health_response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(ready_response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_different_forwarded_ips_get_separate_buckets(self):
        with _tight_rates("1/min"):
            response_a = self.client.post(
                ROUTE_URL, {}, format="json", HTTP_X_FORWARDED_FOR="203.0.113.1"
            )
            response_b = self.client.post(
                ROUTE_URL, {}, format="json", HTTP_X_FORWARDED_FOR="203.0.113.2"
            )

        self.assertEqual(response_a.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response_b.status_code, status.HTTP_400_BAD_REQUEST)

    def test_same_forwarded_ip_trips_the_limit(self):
        with _tight_rates("1/min"):
            self.client.post(
                ROUTE_URL, {}, format="json", HTTP_X_FORWARDED_FOR="203.0.113.5"
            )

            response = self.client.post(
                ROUTE_URL, {}, format="json", HTTP_X_FORWARDED_FOR="203.0.113.5"
            )

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_throttled_request_never_calls_mapbox(self):
        with _tight_rates("1/min"), mock.patch(MOCK_GET_ROUTES) as mock_get_routes:
            self.client.post(ROUTE_URL, {}, format="json")
            self.client.post(ROUTE_URL, {}, format="json")  # throttled

            mock_get_routes.assert_not_called()

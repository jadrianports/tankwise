"""Tests for the global outbound-call budget.

The per-IP DRF throttles are covered in test_throttles.py. These cover the
ceiling those throttles cannot express: the project-wide total, shared
across gunicorn workers via the cache.
"""
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from routing.services import budget
from routing.services.budget import UpstreamBudgetExhaustedError

ROUTE_URL = "/api/route"
START_COORD = "41.8781,-87.6298"
FINISH_COORD = "38.6270,-90.1994"


@override_settings(
    MAPBOX_BUDGET_ENABLED=True,
    MAPBOX_DAILY_CALL_CAP=3,
    MAPBOX_MONTHLY_CALL_CAP=1000,
)
class ConsumeTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_allows_calls_up_to_the_cap(self):
        for _ in range(3):
            budget.consume(budget.DIRECTIONS)

    def test_rejects_the_call_that_would_exceed_the_cap(self):
        for _ in range(3):
            budget.consume(budget.DIRECTIONS)

        with self.assertRaises(UpstreamBudgetExhaustedError) as ctx:
            budget.consume(budget.DIRECTIONS)

        self.assertEqual(ctx.exception.window, "day")
        self.assertEqual(ctx.exception.cap, 3)
        self.assertEqual(ctx.exception.kind, budget.DIRECTIONS)

    def test_kinds_are_counted_independently(self):
        """Directions and Geocoding bill against separate Mapbox products,
        so exhausting one must not lock out the other."""
        for _ in range(3):
            budget.consume(budget.DIRECTIONS)

        budget.consume(budget.GEOCODING)  # must not raise

    def test_a_rejected_call_does_not_consume_more_budget(self):
        for _ in range(3):
            budget.consume(budget.DIRECTIONS)
        with self.assertRaises(UpstreamBudgetExhaustedError):
            budget.consume(budget.DIRECTIONS)

        self.assertEqual(budget.usage(budget.DIRECTIONS)["day"][0], 3)

    def test_usage_reports_counts_without_incrementing(self):
        budget.consume(budget.DIRECTIONS)

        self.assertEqual(budget.usage(budget.DIRECTIONS)["day"], (1, 3))
        self.assertEqual(budget.usage(budget.DIRECTIONS)["day"], (1, 3))


@override_settings(MAPBOX_BUDGET_ENABLED=True, MAPBOX_DAILY_CALL_CAP=0,
                   MAPBOX_MONTHLY_CALL_CAP=0)
class DisabledWindowTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_a_zero_cap_disables_that_window(self):
        for _ in range(50):
            budget.consume(budget.DIRECTIONS)


@override_settings(MAPBOX_BUDGET_ENABLED=False, MAPBOX_DAILY_CALL_CAP=1)
class KillSwitchTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_disabling_the_budget_bypasses_counting_entirely(self):
        for _ in range(10):
            budget.consume(budget.DIRECTIONS)


@override_settings(MAPBOX_BUDGET_ENABLED=True, MAPBOX_DAILY_CALL_CAP=1)
class FailOpenTests(SimpleTestCase):
    """A cache outage must not take the whole demo offline -- the caps sit
    far enough under the free tier to absorb some uncounted calls."""

    def test_a_cache_failure_allows_the_call(self):
        with mock.patch(
            "routing.services.budget.cache.get", side_effect=ConnectionError("down")
        ):
            budget.consume(budget.DIRECTIONS)  # must not raise

    def test_usage_degrades_to_empty_rather_than_raising(self):
        with mock.patch(
            "routing.services.budget.cache.get", side_effect=ConnectionError("down")
        ):
            self.assertEqual(budget.usage(budget.DIRECTIONS), {})


@override_settings(
    MAPBOX_BUDGET_ENABLED=True,
    MAPBOX_DAILY_CALL_CAP=1,
    MAPBOX_MONTHLY_CALL_CAP=1000,
    # get_routes checks the token before it checks the budget, so without
    # this the ImproperlyConfigured branch (502) would mask the 503 under
    # test. Never used for a real call -- the budget rejects first.
    MAPBOX_TOKEN="test-token",
    MAPBOX_PUBLIC_TOKEN="pk.test-token",
)
class BudgetHttpResponseTests(APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_exhausted_budget_returns_503_with_its_own_code(self):
        """503 rather than 429: the service as a whole is out of allowance,
        which is not the same claim as 'you personally were too chatty'."""
        for _ in range(1):
            budget.consume(budget.DIRECTIONS)

        response = self.client.post(
            ROUTE_URL,
            {"start": START_COORD, "finish": FINISH_COORD},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["error"]["code"], "upstream_budget_exhausted")
        self.assertEqual(response.data["error"]["detail"]["window"], "day")

    def test_no_network_request_is_issued_once_the_budget_is_gone(self):
        """The whole point: the token stops being spent. Asserted against
        the HTTP session itself, not a stand-in one layer up."""
        from routing.services import mapbox

        budget.consume(budget.DIRECTIONS)

        with mock.patch.object(mapbox._SESSION, "get") as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        mock_get.assert_not_called()
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_an_already_cached_route_still_serves_while_exhausted(self):
        """Lockout must not break routes that cost nothing to serve -- the
        view reads cache before any upstream call, and this pins that
        ordering so a later refactor cannot silently invert it."""
        from routing.services import mapbox

        payload = {"total_cost": "12.34", "total_route_mi": "500"}
        with mock.patch("routing.views.build_cache_key", return_value="k") as _key:
            cache.set("k", payload, 60)
            budget.consume(budget.DIRECTIONS)

            with mock.patch.object(mapbox._SESSION, "get") as mock_get:
                response = self.client.post(
                    ROUTE_URL,
                    {"start": START_COORD, "finish": FINISH_COORD},
                    format="json",
                )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_cost"], "12.34")
        mock_get.assert_not_called()

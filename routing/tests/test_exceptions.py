"""Tests for the custom DRF exception handler.

`custom_exception_handler` is called directly with a synthetic context --
no HTTP dispatch needed to exercise the envelope/status mapping.
"""
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.exceptions import Throttled, ValidationError

from routing.exceptions import custom_exception_handler
from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError
from routing.services.mapbox import MapboxRequestError, RouteNotFoundError

FAKE_TOKEN = "sk.fake-mapbox-token-should-never-leak"


class RouteNotFoundMappingTests(SimpleTestCase):
    def test_route_not_found_maps_to_422(self):
        response = custom_exception_handler(RouteNotFoundError("no route"), {})

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.data["error"]["code"], "route_not_found")


class InfeasibleRouteMappingTests(SimpleTestCase):
    def test_infeasible_route_maps_to_422_with_gap_detail(self):
        exc = InfeasibleRouteError(
            from_station="START",
            to_station="STOP1",
            gap_mi=Decimal("612.5"),
            max_range_mi=Decimal("500"),
        )

        response = custom_exception_handler(exc, {})

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.data["error"]["code"], "infeasible_route")
        detail = response.data["error"]["detail"]
        self.assertEqual(detail["from_station"], "START")
        self.assertEqual(detail["to_station"], "STOP1")
        self.assertEqual(detail["gap_mi"], "613")
        self.assertEqual(detail["max_range_mi"], "500")


class MapboxRequestErrorMappingTests(SimpleTestCase):
    def test_mapbox_request_error_maps_to_502(self):
        response = custom_exception_handler(MapboxRequestError("boom"), {})

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data["error"]["code"], "upstream_error")


class ImproperlyConfiguredMappingTests(SimpleTestCase):
    def test_improperly_configured_maps_to_502(self):
        response = custom_exception_handler(
            ImproperlyConfigured("MAPBOX_TOKEN is not set"), {}
        )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data["error"]["code"], "upstream_error")


class InvalidRouteInputMappingTests(SimpleTestCase):
    def test_invalid_route_input_maps_to_400(self):
        response = custom_exception_handler(
            InvalidRouteInputError("total_route_mi must be positive"), {}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"]["code"], "invalid_input")


class DRFValidationErrorMappingTests(SimpleTestCase):
    def test_drf_validation_error_wrapped_in_envelope_at_400(self):
        response = custom_exception_handler(ValidationError("bad input"), {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"]["code"], "invalid_input")
        self.assertIn("message", response.data["error"])
        self.assertIn("detail", response.data["error"])


class ThrottledMappingTests(SimpleTestCase):
    def test_throttled_maps_to_429_with_rate_limited_envelope(self):
        exc = Throttled(wait=5)

        response = custom_exception_handler(exc, {})

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data["error"]["code"], "rate_limited")
        self.assertEqual(response.data["error"]["detail"]["retry_after_s"], 5)


class UnrecognizedExceptionTests(SimpleTestCase):
    def test_unrecognized_exception_returns_none(self):
        self.assertIsNone(custom_exception_handler(RuntimeError("unexpected"), {}))


class TokenLeakTests(SimpleTestCase):
    """No error envelope may ever contain the
    Mapbox token, regardless of what the underlying exception message
    carries."""

    def test_mapbox_request_error_message_never_echoes_token(self):
        exc = MapboxRequestError(f"Mapbox request failed, token={FAKE_TOKEN}")

        response = custom_exception_handler(exc, {})

        self.assertNotIn(FAKE_TOKEN, str(response.data))

    def test_improperly_configured_message_never_echoes_token(self):
        exc = ImproperlyConfigured(f"bad config, token={FAKE_TOKEN}")

        response = custom_exception_handler(exc, {})

        self.assertNotIn(FAKE_TOKEN, str(response.data))

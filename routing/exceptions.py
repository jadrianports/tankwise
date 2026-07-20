"""Custom DRF exception handler: the standard error envelope, plus the
status mapping for the project's domain exception hierarchy.

This module holds the request path's only DRF import for error handling --
`routing/services/mapbox.py` and `routing/services/exceptions.py` stay
DRF-free and raise plain-Python exceptions; this handler is the sole
translation layer from those exceptions to HTTP.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ImproperlyConfigured
from rest_framework import status
from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_handler

from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError
from routing.services.mapbox import MapboxRequestError, RouteNotFoundError


def _envelope(code, message, detail=None):
    """The standard error envelope shape."""
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def _quantize_miles(value) -> str:
    """Round a Decimal distance to the nearest whole mile for the HTTP
    response only -- the solver's own gap comparison against max range
    already ran on the full-precision value before this exception was
    ever constructed."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return str(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def custom_exception_handler(exc, context):
    """Registered via `REST_FRAMEWORK["EXCEPTION_HANDLER"]`.

    First defers to DRF's default handler -- if it recognizes the
    exception (e.g. a serializer `ValidationError`), its response is
    re-wrapped in the envelope under `invalid_input`, preserving the
    original status code. `Throttled` is also recognized by DRF's
    default handler -- it is re-coded to `rate_limited` rather than
    re-created, since the default handler already attaches the
    `Retry-After` header to the `Response` it returns and reassigning
    only `.data` preserves that header untouched. Otherwise dispatches
    by `isinstance` on the project's plain-Python domain exceptions.
    Returns `None` for anything unrecognized so DRF/Django's default 500
    handler takes over -- never surfaces a traceback.

    Every branch assigns `result` instead of returning immediately, so a
    single tail-attach point can carry partial Server-Timing data
    (`context["view"]._timing`, per `routing.timing.ServerTiming`) onto
    whatever error response is built -- the stages that completed before
    the failure, per D-10. The tail Server-Timing attach is a no-op for
    a 429 because the throttle rejects before the pipeline runs, so
    `view._timing` was never created.
    """
    response = drf_default_handler(exc, context)
    if response is not None:
        if isinstance(exc, Throttled):
            response.data = _envelope(
                "rate_limited", "Too many requests.", {"retry_after_s": exc.wait}
            )
        else:
            response.data = _envelope("invalid_input", "Invalid request.", response.data)
        result = response
    elif isinstance(exc, RouteNotFoundError):
        result = Response(
            _envelope("route_not_found", str(exc)),
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    elif isinstance(exc, InfeasibleRouteError):
        result = Response(
            _envelope(
                "infeasible_route",
                str(exc),
                {
                    "from_station": exc.from_station,
                    "to_station": exc.to_station,
                    "gap_mi": _quantize_miles(exc.gap_mi),
                    "max_range_mi": str(exc.max_range_mi),
                },
            ),
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    elif isinstance(exc, MapboxRequestError):
        result = Response(
            _envelope("upstream_error", "Upstream routing provider failed."),
            status=status.HTTP_502_BAD_GATEWAY,
        )
    elif isinstance(exc, InvalidRouteInputError):
        result = Response(
            _envelope("invalid_input", str(exc)), status=status.HTTP_400_BAD_REQUEST
        )
    elif isinstance(exc, ImproperlyConfigured):
        result = Response(
            _envelope("upstream_error", "Service misconfigured."),
            status=status.HTTP_502_BAD_GATEWAY,
        )
    else:
        result = None

    if result is not None:
        view = context.get("view")
        timing = getattr(view, "_timing", None)
        if timing is not None:
            result["Server-Timing"] = timing.header_value()
    return result

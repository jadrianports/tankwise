"""Mapbox Directions v5 client: single-call route fetch (ROUTE-02, D-05/D-07).

Request-path HTTP + Django settings only -- no `routing.models`/
`routing.pipeline` import (D-12). Distance is exact, unrounded `Decimal`,
consistent with the project's money/measure discipline; the `access_token`
always rides in `requests.get(params=...)`, never interpolated into the
URL string (D-07, Pitfall B).
"""
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from requests.adapters import HTTPAdapter
from shapely.geometry import LineString
from urllib3.util.retry import Retry

# Mapbox convention: longitude first in the path segment.
DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/driving/{lon1},{lat1};{lon2},{lat2}"

GEOCODING_URL = "https://api.mapbox.com/search/geocode/v6/forward"

# A single pooled session, reused for both the Directions and Geocoding
# calls within a request and across requests (HTTP keep-alive). This
# avoids a fresh TLS handshake per Mapbox call, which is the biggest
# fixed cost on the mixed/address-input paths that make 2-3 calls.
#
# Pooled keep-alive connections can be closed by the remote during an
# idle gap; without retries, reusing a now-dead connection surfaces as a
# spurious ConnectionError -> upstream_error 502. A bounded Retry lets
# both (idempotent GET) calls transparently recover on a fresh
# connection, and also rides out transient Mapbox 5xx/429 blips. It does
# NOT retry deterministic auth/4xx (401/403/404) or a Mapbox NoRoute,
# which comes back as HTTP 200 with code != "Ok" and is handled in-app.
_RETRY = Retry(
    total=2,
    connect=2,
    read=2,  # retries RemoteDisconnected/reset on a reused GET (stale conn)
    status=2,
    backoff_factor=0.3,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,  # existing status_code != 200 check owns the final response
)
_SESSION = requests.Session()
_SESSION.mount(
    "https://",
    HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=_RETRY),
)


class MapboxError(Exception):
    """Base class for all Mapbox Directions client errors."""


class RouteNotFoundError(MapboxError):
    """No drivable route exists between the requested points: Mapbox
    returned a `code` other than "Ok" (e.g. "NoRoute") or an empty
    `routes` list. Phase 4 maps this to API-04."""

    def __init__(self, message):
        super().__init__(message)


class MapboxRequestError(MapboxError):
    """The Directions request itself failed: a non-200 HTTP status or a
    transport-level failure (connection error, timeout). Phase 4 maps
    this to an upstream-failure response."""

    def __init__(self, message):
        super().__init__(message)


@dataclass(frozen=True)
class Route:
    """A driving route resolved from Mapbox Directions."""

    total_route_mi: Decimal
    geometry: LineString
    raw_coordinates: list


def get_route(start, finish) -> Route:
    """Fetch the driving route between `start` and `finish` in exactly one
    Mapbox Directions call (ROUTE-02).

    `start`/`finish` are `(latitude, longitude)` Decimal pairs -- note this
    is the opposite order from the Mapbox path segment, which is built as
    lon,lat below (Pitfall 5).

    Raises `ImproperlyConfigured` if `settings.MAPBOX_TOKEN` is unset,
    before any HTTP call is attempted (D-08). Raises `MapboxRequestError`
    on a non-200 status or a `requests` transport failure. Raises
    `RouteNotFoundError` when Mapbox reports no route (`code != "Ok"` or
    an empty `routes` list).
    """
    if not settings.MAPBOX_TOKEN:
        raise ImproperlyConfigured(
            "MAPBOX_TOKEN is not set -- cannot call the Mapbox Directions API"
        )

    start_lat, start_lng = start
    finish_lat, finish_lng = finish
    url = DIRECTIONS_URL.format(
        lon1=start_lng, lat1=start_lat, lon2=finish_lng, lat2=finish_lat
    )

    try:
        response = _SESSION.get(
            url,
            params={
                "geometries": "geojson",
                "overview": "full",
                "access_token": settings.MAPBOX_TOKEN,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        raise MapboxRequestError("Mapbox Directions request failed") from exc

    if response.status_code != 200:
        raise MapboxRequestError(
            f"Mapbox Directions request failed with status {response.status_code}"
        )

    return _parse_directions_response(response.json())


def geocode(address) -> tuple:
    """Resolve a free-text address to a (latitude, longitude) Decimal pair
    via exactly one Mapbox Geocoding v6 forward call (API-05, D-16).

    Uses the v6 endpoint's default temporary-geocoding tier -- the result
    must only ever flow into an in-memory `get_route()` call and the
    response payload, never a DB write (D-16 ToS constraint).

    Raises `ImproperlyConfigured` if `settings.MAPBOX_TOKEN` is unset,
    before any HTTP call is attempted. Raises `MapboxRequestError` on a
    non-200 status or a `requests` transport failure. Raises
    `RouteNotFoundError` when no geocoding result is found for `address`.
    """
    if not settings.MAPBOX_TOKEN:
        raise ImproperlyConfigured(
            "MAPBOX_TOKEN is not set -- cannot call the Mapbox Geocoding API"
        )

    try:
        response = _SESSION.get(
            GEOCODING_URL,
            params={
                "q": address,
                "country": "us",
                "limit": 1,
                "access_token": settings.MAPBOX_TOKEN,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        raise MapboxRequestError("Mapbox Geocoding request failed") from exc

    if response.status_code != 200:
        raise MapboxRequestError(
            f"Mapbox Geocoding request failed with status {response.status_code}"
        )

    return _parse_geocoding_response(response.json())


def _parse_geocoding_response(data) -> tuple:
    """Parse a Mapbox Geocoding v6 JSON response into a (lat, lng) Decimal
    pair, kept separable from the transport call so it is unit-testable
    against a recorded fixture without a real request."""
    features = data.get("features") or []
    if not features:
        raise RouteNotFoundError("No geocoding result for address")

    lng, lat = features[0]["geometry"]["coordinates"]
    return Decimal(str(lat)), Decimal(str(lng))


def _parse_directions_response(data) -> Route:
    """Parse a Mapbox Directions v5 JSON response into a `Route`, kept
    separable from the transport call so it is unit-testable against a
    recorded fixture without a real request (D-13)."""
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RouteNotFoundError(
            f"Mapbox found no route: code={data.get('code')!r}"
        )

    route0 = data["routes"][0]
    coords = route0["geometry"]["coordinates"]
    total_route_mi = Decimal(str(route0["distance"])) / Decimal("1609.344")

    return Route(
        total_route_mi=total_route_mi,
        geometry=LineString(coords),
        raw_coordinates=coords,
    )

"""Mapbox Directions v5 client: single-call route fetch.

Request-path HTTP + Django settings only -- no `routing.models`/
`routing.pipeline` import. Distance is exact, unrounded `Decimal`,
consistent with the project's money/measure discipline; the `access_token`
always rides in `requests.get(params=...)`, never interpolated into the
URL string.
"""
from dataclasses import dataclass, field
from decimal import Decimal

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from requests.adapters import HTTPAdapter
from shapely.geometry import LineString
from urllib3.util.retry import Retry

# Mapbox convention: longitude first in the path segment.
DIRECTIONS_BASE_URL = "https://api.mapbox.com/directions/v5/mapbox/driving"

GEOCODING_URL = "https://api.mapbox.com/search/geocode/v6/forward"

# One pooled keep-alive session for all Mapbox calls (avoids a per-call
# TLS handshake). The bounded Retry recovers a stale reused connection
# (spurious ConnectionError -> 502) and transient 5xx/429; it does NOT
# retry auth/4xx or a Mapbox NoRoute (HTTP 200 code != "Ok", handled in-app).
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
    `routes` list. The view layer maps this to a 422 response."""

    def __init__(self, message):
        super().__init__(message)


class MapboxRequestError(MapboxError):
    """The Directions request itself failed: a non-200 HTTP status or a
    transport-level failure (connection error, timeout). The view layer
    maps this to an upstream-failure response."""

    def __init__(self, message):
        super().__init__(message)


@dataclass(frozen=True)
class Route:
    """A driving route resolved from Mapbox Directions."""

    total_route_mi: Decimal
    geometry: LineString
    raw_coordinates: list
    duration_s: Decimal = Decimal(0)
    annotation_durations: list = field(default_factory=list)
    annotation_distances: list = field(default_factory=list)
    alternative_index: int = 0
    leg_distances_mi: list = field(default_factory=list)
    leg_annotation_lengths: list = field(default_factory=list)


def get_routes(ordered_coords) -> list:
    """Fetch every driving route alternative Mapbox offers across the
    ordered stops in `ordered_coords`, in exactly one Mapbox Directions
    call.

    `ordered_coords`: an ordered sequence of at least 2 `(latitude,
    longitude)` Decimal pairs -- start, any intermediate waypoints in
    visit order, then finish. Note this is the opposite order from the
    Mapbox path segment, which is built as lon,lat below. A 2-element
    sequence reproduces the original start/finish-only request
    byte-for-byte.

    Raises `ImproperlyConfigured` if `settings.MAPBOX_TOKEN` is unset,
    before any HTTP call is attempted. Raises `MapboxRequestError`
    on a non-200 status or a `requests` transport failure. Raises
    `RouteNotFoundError` when Mapbox reports no route (`code != "Ok"` or
    an empty `routes` list).
    """
    if not settings.MAPBOX_TOKEN:
        raise ImproperlyConfigured(
            "MAPBOX_TOKEN is not set -- cannot call the Mapbox Directions API"
        )

    coords_path = ";".join(f"{lng},{lat}" for lat, lng in ordered_coords)
    url = f"{DIRECTIONS_BASE_URL}/{coords_path}"

    try:
        response = _SESSION.get(
            url,
            params={
                "geometries": "geojson",
                "overview": "full",
                "alternatives": "true",
                "annotations": "duration,distance",
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
    via exactly one Mapbox Geocoding v6 forward call.

    Uses the v6 endpoint's default temporary-geocoding tier -- the result
    must only ever flow into an in-memory `get_routes()` call and the
    response payload, never a DB write (Mapbox's temporary-geocoding terms
    forbid storing it).

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
    """Parse a Mapbox Geocoding v6 JSON response into a (lat, lng) Decimal pair.
    Kept separate from the transport call so it is fixture-testable offline."""
    features = data.get("features") or []
    if not features:
        raise RouteNotFoundError("No geocoding result for address")

    lng, lat = features[0]["geometry"]["coordinates"]
    return Decimal(str(lat)), Decimal(str(lng))


def _parse_directions_response(data) -> list:
    """Parse a Mapbox Directions v5 JSON response into a list of `Route`,
    one per alternative Mapbox returned (ordered as Mapbox returned them).
    Kept separate from the transport call so it is fixture-testable
    offline."""
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RouteNotFoundError(
            f"Mapbox found no route: code={data.get('code')!r}"
        )

    return [
        _parse_single_route(route_data, index)
        for index, route_data in enumerate(data["routes"])
    ]


def _parse_single_route(route_data, alternative_index) -> Route:
    """Parse one element of `data["routes"]` into a `Route`. Annotation
    arrays are read defensively -- a missing `legs`, `annotation`, or key
    degrades to an empty list rather than raising.

    Multi-stop routes carry N-1 Mapbox `legs` for N coordinates, each
    with its own `annotation` arrays -- these are concatenated across
    EVERY leg, in Mapbox's own visit order, never truncated to `legs[0]`
    (WAY-04). `leg_distances_mi`/`leg_annotation_lengths` carry each
    leg's own scalar distance and segment count so a later flattening
    step can slice `raw_coordinates` back into per-leg geometry (see
    `routing.services.multi_leg`)."""
    coords = route_data["geometry"]["coordinates"]
    total_route_mi = Decimal(str(route_data["distance"])) / Decimal("1609.344")
    duration_s = Decimal(str(route_data["duration"]))

    legs = route_data.get("legs") or []
    raw_durations = [
        value
        for leg in legs
        for value in (leg.get("annotation", {}).get("duration") or [])
    ]
    raw_distances_m = [
        value
        for leg in legs
        for value in (leg.get("annotation", {}).get("distance") or [])
    ]

    annotation_durations = [Decimal(str(value)) for value in raw_durations]
    annotation_distances = [
        Decimal(str(value)) / Decimal("1609.344") for value in raw_distances_m
    ]

    leg_distances_mi = [
        Decimal(str(leg.get("distance", 0))) / Decimal("1609.344") for leg in legs
    ]
    leg_annotation_lengths = [
        len(leg.get("annotation", {}).get("distance") or []) for leg in legs
    ]

    return Route(
        total_route_mi=total_route_mi,
        geometry=LineString(coords),
        raw_coordinates=coords,
        duration_s=duration_s,
        annotation_durations=annotation_durations,
        annotation_distances=annotation_distances,
        alternative_index=alternative_index,
        leg_distances_mi=leg_distances_mi,
        leg_annotation_lengths=leg_annotation_lengths,
    )

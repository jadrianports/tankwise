"""Mapbox Static Images `map_url` builder.

Builds a `https://api.mapbox.com/styles/v1/mapbox/streets-v12/static/...`
URL string -- this backend never fetches the PNG itself; the actual
image render happens client-side when a reviewer opens the URL. The
access token rides only in the `access_token` query parameter, mirroring
the token-never-in-URL-string discipline already used by
`routing/services/mapbox.py`'s Directions/Geocoding calls.
"""
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

import polyline as polyline_lib

STATIC_IMAGES_URL = "https://api.mapbox.com/styles/v1/mapbox/streets-v12/static"
MAX_URL_LENGTH = 8192

_IMAGE_SIZE = "600x400"
_START_PIN_COLOR = "3b82f6"
_STOP_PIN_COLOR = "f59e0b"
_FINISH_PIN_COLOR = "22c55e"
_PATH_OVERLAY_STYLE = "path-3+ef4444-0.8"

_INITIAL_TOLERANCE = 0.0001
_TOLERANCE_CEILING = 1.0

# Fixed tolerance (degrees) for simplifying `route.geometry` in the JSON
# `route_geometry` field -- distinct from the progressive ladder above,
# which targets the Static Images URL length limit instead.
RESPONSE_GEOMETRY_TOLERANCE = 0.005


def simplify_geometry(route) -> list:
    """Simplify `route.geometry` at `RESPONSE_GEOMETRY_TOLERANCE` and
    return a list of `[lng, lat]` pairs -- the same coordinate order as
    `route.raw_coordinates`. `simplify(..., preserve_topology=False)`
    always retains the first and last vertices of the input, so the
    route's exact start/finish endpoints are preserved."""
    simplified = route.geometry.simplify(
        RESPONSE_GEOMETRY_TOLERANCE, preserve_topology=False
    )
    return [[lng, lat] for lng, lat in simplified.coords]


def build_map_url(route, start, finish, stop_coords) -> str:
    """Build a Static Images URL with a start pin, a finish pin, one
    labeled pin per fuel stop (in route order), an `auto` viewport,
    and a `path-` overlay whose encoded polyline is progressively
    simplified until the full URL fits under `MAX_URL_LENGTH` (guard
    loop) -- or the tolerance ceiling is reached, whichever comes first.

    `start`/`finish` are `(lat, lng)` pairs; `stop_coords` is an ordered
    list of `(lat, lng)` pairs, one per fuel stop (the
    orchestrator looks these up from `Station` by `opis_id`, since
    `FuelStop` carries no lat/lng of its own).

    Uses `settings.MAPBOX_PUBLIC_TOKEN` (never the secret `MAPBOX_TOKEN`)
    since this URL is returned to and opened by the browser. Raises
    `ImproperlyConfigured` if the public token is unset, or if it does not
    start with `pk.` -- guarding against a secret token being pasted into
    the public slot by mistake.
    """
    token = settings.MAPBOX_PUBLIC_TOKEN
    if not token:
        raise ImproperlyConfigured(
            "MAPBOX_PUBLIC_TOKEN is not set; map_url cannot be built."
        )
    if not token.startswith("pk."):
        raise ImproperlyConfigured(
            "MAPBOX_PUBLIC_TOKEN must start with 'pk.'; a secret token may "
            "have been pasted into the public token slot."
        )

    markers = _build_markers(start, finish, stop_coords)

    tolerance = _INITIAL_TOLERANCE
    while True:
        encoded = _encode_geometry(route, tolerance)
        # Percent-encode only the polyline payload (it holds URL-unsafe `\`, `|`,
        # `?`) -- never the surrounding marker/path grammar. Must run before the
        # length check so MAX_URL_LENGTH measures the real transmitted URL.
        encoded_safe = quote(encoded, safe="")
        overlay = ",".join(markers + [f"{_PATH_OVERLAY_STYLE}({encoded_safe})"])
        url = (
            f"{STATIC_IMAGES_URL}/{overlay}/auto/{_IMAGE_SIZE}"
            f"?access_token={token}"
        )
        if len(url) <= MAX_URL_LENGTH or tolerance > _TOLERANCE_CEILING:
            return url
        tolerance *= 2


def _build_markers(start, finish, stop_coords):
    start_lat, start_lng = start
    finish_lat, finish_lng = finish

    markers = [f"pin-s-a+{_START_PIN_COLOR}({start_lng},{start_lat})"]
    for n, (lat, lng) in enumerate(stop_coords, start=1):
        markers.append(f"pin-s-{n}+{_STOP_PIN_COLOR}({lng},{lat})")
    markers.append(f"pin-s-b+{_FINISH_PIN_COLOR}({finish_lng},{finish_lat})")
    return markers


def _encode_geometry(route, tolerance) -> str:
    """Simplify `route.geometry` (lng,lat LineString) at `tolerance`, then
    encode as a Google/Mapbox polyline string -- which expects (lat,lng)
    pairs, the opposite order from the geometry's raw GeoJSON coords."""
    simplified = route.geometry.simplify(tolerance, preserve_topology=False)
    return polyline_lib.encode([(lat, lng) for lng, lat in simplified.coords])

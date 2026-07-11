"""Cache-key normalizer for the /api/route response cache.

Small, module-level pure helpers -- mirrors `routing/services/corridor.py`'s
`_as_decimal`/helper style. Coordinates are rounded to 5 decimal places
(~1 m) so trivial float differences still hit; addresses are casefolded
and whitespace-collapsed so an exact-repeat address (modulo case/spacing)
skips all outbound calls. Explicit `c:`/`a:` prefixes give the two paths
separate namespaces so a coordinate token and an address token can never
collide (mitigates cross-domain cache-key collisions).
"""
from decimal import Decimal

COORD_PRECISION = 5


def _coord_token(lat, lng) -> str:
    lat_value = lat if isinstance(lat, Decimal) else Decimal(str(lat))
    lng_value = lng if isinstance(lng, Decimal) else Decimal(str(lng))
    return f"c:{round(lat_value, COORD_PRECISION)},{round(lng_value, COORD_PRECISION)}"


def _address_token(value: str) -> str:
    return f"a:{' '.join(value.casefold().split())}"


def _endpoint_token(endpoint) -> str:
    if endpoint["kind"] == "coordinate":
        return _coord_token(endpoint["lat"], endpoint["lng"])
    return _address_token(endpoint["value"])


def build_cache_key(validated_data) -> str:
    """Build the cache key for a validated `{"start": ..., "finish": ...}`
    payload (the `RouteRequestSerializer.validated_data` shape).

    Each of `start`/`finish` is `{"kind": "coordinate", "lat", "lng"}` or
    `{"kind": "address", "value"}`. Composed as a simple string, not a
    hash -- no need to hand-roll one at this scale."""
    start_token = _endpoint_token(validated_data["start"])
    finish_token = _endpoint_token(validated_data["finish"])
    return f"route:v1:{start_token}|{finish_token}"

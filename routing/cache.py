"""Cache-key normalizer for the /api/route response cache.

Small, module-level pure helpers -- mirrors `routing/services/corridor.py`'s
`_as_decimal`/helper style. Coordinates are rounded to 5 decimal places
(~1 m) so trivial float differences still hit; addresses are casefolded
and whitespace-collapsed so an exact-repeat address (modulo case/spacing)
skips all outbound calls. Explicit `c:`/`a:`/`v:` prefixes give each
component its own namespace so a coordinate token, an address token, and
the vehicle token can never collide (mitigates cross-domain cache-key
collisions).

The key is versioned `route:v2:` because every v2 response field
(vehicle echo, savings, alternatives, per-leg breakdown, rationale)
changes the cached payload shape -- an entry keyed under the previous
prefix is not merely stale but structurally wrong for a v2 consumer.
Bumping the prefix makes those entries unreachable rather than
mis-served (it also means a misconfigured deploy can never silently
return an old-shaped payload through the new code path).
"""
from decimal import Decimal

COORD_PRECISION = 5
# Vehicle-token quantization: fine enough that two genuinely different
# vehicle profiles can never quantize together, coarse enough that a
# client sending 6 vs. 6.00 vs. 6.001 still hits the same cached
# answer instead of paying for a redundant Mapbox call.
MPG_PRECISION = 2
TANK_PRECISION = 1
FUEL_PRECISION = 3


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


def _vehicle_token(vehicle) -> str:
    # Imported locally (not at module scope) purely to keep the two
    # modules' import order irrelevant -- routing.serializers is the
    # sole declaration site for the three defaults, reused here
    # rather than redeclared, so `build_cache_key` stays total over
    # any validated-data dict, including ones existing tests construct
    # by hand without a "vehicle" key.
    from routing.serializers import (
        DEFAULT_MPG,
        DEFAULT_STARTING_FUEL,
        DEFAULT_TANK_RANGE_MI,
    )

    vehicle = vehicle or {}
    mpg = vehicle.get("mpg", DEFAULT_MPG)
    tank_range_mi = vehicle.get("tank_range_mi", DEFAULT_TANK_RANGE_MI)
    starting_fuel = vehicle.get("starting_fuel", DEFAULT_STARTING_FUEL)

    mpg = mpg if isinstance(mpg, Decimal) else Decimal(str(mpg))
    tank_range_mi = (
        tank_range_mi
        if isinstance(tank_range_mi, Decimal)
        else Decimal(str(tank_range_mi))
    )
    starting_fuel = (
        starting_fuel
        if isinstance(starting_fuel, Decimal)
        else Decimal(str(starting_fuel))
    )

    return (
        f"v:{round(mpg, MPG_PRECISION)},"
        f"{round(tank_range_mi, TANK_PRECISION)},"
        f"{round(starting_fuel, FUEL_PRECISION)}"
    )


def build_cache_key(validated_data) -> str:
    """Build the cache key for a validated
    `{"start": ..., "finish": ..., "vehicle": ...}` payload (the
    `RouteRequestSerializer.validated_data` shape).

    Each of `start`/`finish` is `{"kind": "coordinate", "lat", "lng"}` or
    `{"kind": "address", "value"}`. `vehicle` is optional here -- see
    `_vehicle_token` -- so callers that omit it (existing tests, a
    v1.0-shaped request that resolved to defaults) still get a stable
    key. Composed as a simple string, not a hash -- no need to
    hand-roll one at this scale."""
    start_token = _endpoint_token(validated_data["start"])
    finish_token = _endpoint_token(validated_data["finish"])
    vehicle_token = _vehicle_token(validated_data.get("vehicle"))
    return f"route:v2:{start_token}|{finish_token}|{vehicle_token}"

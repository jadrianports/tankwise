"""DRF request/response serializers for the /api/route endpoint.

`LocationField` sniffs a coordinate-or-address input without a caller
-supplied type tag and bounds-checks coordinates against the
continental-US bbox. `RouteResponseSerializer`/`FuelStopSerializer`
render the frontend-facing response contract, quantizing money and gallons
to exactly 2 decimal places and route distance to the nearest whole mile,
all at this one boundary -- the solver and Mapbox client
upstream never round.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework import serializers

from routing.map_url import simplify_geometry
from routing.pipeline.bbox import is_valid as bbox_is_valid

MAX_ADDRESS_LENGTH = 256

# Vehicle profile defaults -- declared once here so the nested
# VehicleSerializer's per-field defaults and RouteRequestSerializer's
# absent-vehicle fill-in both read these same three names (D-01).
DEFAULT_MPG = Decimal("10")
DEFAULT_TANK_RANGE_MI = Decimal("500")
DEFAULT_STARTING_FUEL = Decimal("1")


def _quantize_money(value) -> str:
    """Coerce a Decimal (or Decimal-able value) to a string quantized to
    exactly 2 decimal places, ROUND_HALF_UP. Applied only at
    this serializer boundary -- never upstream in the solver/Mapbox
    client."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _quantize_gallons(value) -> str:
    """Same treatment as `_quantize_money` (2 decimal places,
    ROUND_HALF_UP), applied to gallons at this one boundary -- the
    solver's internal fuel-purchase math stays full precision."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _quantize_miles(value) -> str:
    """Coerce a Decimal (or Decimal-able value) to a string quantized to
    the nearest whole mile, ROUND_HALF_UP. Applied only at this
    serializer boundary -- the route's exact distance is still what the
    solver/corridor math uses upstream."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return str(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _location_repr(coords):
    """Render a {"latitude": ..., "longitude": ...} coordinate dict as
    Decimal-as-string values. Returns None when no coords were
    supplied via context."""
    if not coords:
        return None
    lat = coords.get("latitude")
    lng = coords.get("longitude")
    return {
        "latitude": str(lat) if lat is not None else None,
        "longitude": str(lng) if lng is not None else None,
    }


class LocationField(serializers.Field):
    """Accepts either a coordinate (`"lat,lng"` string or `[lat, lng]`
    pair) or a free-text address string. Sniffs the shape --
    the caller never declares a type.

    A parsed coordinate is bounds-checked against the continental-US
    bbox; an address longer than `MAX_ADDRESS_LENGTH` is rejected
    here, before any outbound geocoding call (bounds the request size to
    guard against oversized-input abuse).
    """

    def to_internal_value(self, data):
        coord = self._try_parse_coordinate(data)
        if coord is not None:
            lat, lng = coord
            if not bbox_is_valid(lat, lng):
                raise serializers.ValidationError(
                    f"Coordinate ({lat}, {lng}) is outside the continental US."
                )
            return {"kind": "coordinate", "lat": lat, "lng": lng}
        if isinstance(data, str) and data.strip():
            value = data.strip()
            if len(value) > MAX_ADDRESS_LENGTH:
                raise serializers.ValidationError(
                    f"Address must be at most {MAX_ADDRESS_LENGTH} characters, "
                    f"got {len(value)}."
                )
            return {"kind": "address", "value": value}
        raise serializers.ValidationError(
            "Must be a 'lat,lng' string, a [lat, lng] pair, or a "
            "non-empty address string."
        )

    def _try_parse_coordinate(self, data):
        try:
            if isinstance(data, str) and "," in data:
                lat_str, lng_str = data.split(",", 1)
                return Decimal(lat_str.strip()), Decimal(lng_str.strip())
            if isinstance(data, (list, tuple)) and len(data) == 2:
                return Decimal(str(data[0])), Decimal(str(data[1]))
        except (InvalidOperation, ValueError):
            return None
        return None

    def to_representation(self, value):
        return value


class VehicleSerializer(serializers.Serializer):
    """Optional per-request vehicle profile: `{"mpg", "tank_range_mi",
    "starting_fuel"}`. `starting_fuel` is a fraction of tank capacity
    (0.0-1.0), never gallons or miles, so it stays meaningful when
    `tank_range_mi` changes on its own.

    Bounds are wide enough to admit any legitimate vehicle -- a loaded
    semi near 6 mpg, a sedan near 32, a twin-tank semi near 1800 miles
    of range -- while rejecting the values that actually break the
    solver: 0 mpg is a division by zero, a negative or absurd
    tank_range_mi blows up the reachable-set loop, and a starting_fuel
    outside [0, 1] has no physical meaning. These DRF bounds are the
    primary input gate; the solver's own `_validate` backstop (plan
    07-01) is a second, independent line of defense, not the first.
    """

    mpg = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        default=DEFAULT_MPG,
        min_value=Decimal("1"),
        max_value=Decimal("100"),
    )
    tank_range_mi = serializers.DecimalField(
        max_digits=6,
        decimal_places=1,
        required=False,
        default=DEFAULT_TANK_RANGE_MI,
        min_value=Decimal("20"),
        max_value=Decimal("2000"),
    )
    starting_fuel = serializers.DecimalField(
        max_digits=4,
        decimal_places=3,
        required=False,
        default=DEFAULT_STARTING_FUEL,
        min_value=Decimal("0"),
        max_value=Decimal("1"),
    )


class RouteRequestSerializer(serializers.Serializer):
    """`{"start": <loc>, "finish": <loc>, "vehicle": <optional>}`.

    `start`/`finish` are both polymorphic coordinate-or-address fields.
    `vehicle` is an optional nested object -- `{"mpg", "tank_range_mi",
    "starting_fuel"}`, each independently optional -- resolved to
    `DEFAULT_MPG` / `DEFAULT_TANK_RANGE_MI` / `DEFAULT_STARTING_FUEL`
    (10 mpg / 500 mi / a full tank) wherever a key is missing. Omitting
    `vehicle` entirely preserves the exact v1.0 request contract: a
    body of just `{"start", "finish"}` still validates and resolves to
    the same defaulted profile.
    """

    start = LocationField()
    finish = LocationField()
    vehicle = VehicleSerializer(required=False)

    def validate(self, attrs):
        # VehicleSerializer(required=False) with no `default=` is
        # skipped entirely by DRF when the "vehicle" key is absent
        # from the request body, so it never reaches `attrs`. Fill it
        # in here from the same three default constants the nested
        # serializer's own fields use, so every downstream consumer
        # (cache key builder, orchestrator, response serializer) can
        # read `validated_data["vehicle"]` unconditionally.
        if "vehicle" not in attrs:
            attrs["vehicle"] = {
                "mpg": DEFAULT_MPG,
                "tank_range_mi": DEFAULT_TANK_RANGE_MI,
                "starting_fuel": DEFAULT_STARTING_FUEL,
            }
        return attrs


def price_freshness() -> dict:
    """Return the configured fuel-price dataset vintage and its paired
    limitation note (VEH-08 / D-25 / D-26).

    Validated at point of use, not import time -- mirrors
    `routing.services.corridor._corridor_widths`'s pattern of raising
    `ImproperlyConfigured` where the value is actually consumed, so a
    misconfigured deployment fails loudly at request time rather than
    silently shipping a null freshness field.
    """
    as_of = settings.FUEL_PRICE_AS_OF
    if not as_of:
        raise ImproperlyConfigured(
            f"FUEL_PRICE_AS_OF must be a non-empty ISO date, got {as_of!r}"
        )
    return {
        "price_as_of": as_of,
        "price_data_note": settings.FUEL_PRICE_DATA_NOTE,
    }


class FuelStopSerializer(serializers.Serializer):
    """Per-stop response shape. `instance` is a
    `routing.services.solver.FuelStop`; per-stop lat/lng is not carried by
    `FuelStop` itself, so it is looked up from `self.context["stop_coords"]`
    (opis_id -> {"latitude", "longitude"}), injected by the orchestrator.
    """

    def to_representation(self, instance):
        stop_coords = self.context.get("stop_coords", {})
        coords = stop_coords.get(instance.opis_id)
        return {
            "name": instance.name,
            "station_id": instance.opis_id,
            "location": _location_repr(coords),
            "distance_from_start_mi": _quantize_miles(instance.distance_from_start_mi),
            "price_per_gallon": _quantize_money(instance.price_per_gallon),
            "gallons": _quantize_gallons(instance.gallons),
            "cost": _quantize_money(instance.cost),
        }


class RouteResponseSerializer(serializers.Serializer):
    """Renders the full computed response payload.

    `instance` is a mapping with keys `"route"` (a
    `routing.services.mapbox.Route`), `"plan"` (a
    `routing.services.solver.FuelPlan`), and optionally `"map_url"`.
    `self.context` may carry `"stop_coords"` (see `FuelStopSerializer`),
    `"start_coords"`, and `"finish_coords"` (each a
    `{"latitude", "longitude"}` dict), all injected by the orchestrator.

    `route_geometry` is simplified via `routing.map_url.simplify_geometry`
    rather than `route.raw_coordinates` -- a full-resolution route can be
    several thousand points, dominating the payload for no map benefit.
    Simplification preserves the exact start/finish endpoints.
    """

    def to_representation(self, instance):
        route = instance["route"]
        plan = instance["plan"]

        fuel_stops = FuelStopSerializer(
            plan.stops, many=True, context=self.context
        ).data

        return {
            "start": _location_repr(self.context.get("start_coords")),
            "finish": _location_repr(self.context.get("finish_coords")),
            "route_geometry": simplify_geometry(route),
            "total_route_mi": _quantize_miles(route.total_route_mi),
            "fuel_stops": fuel_stops,
            "total_cost": _quantize_money(plan.total_cost),
            "total_gallons": _quantize_gallons(plan.total_gallons),
            "map_url": instance.get("map_url"),
        }

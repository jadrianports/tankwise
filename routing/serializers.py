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

from rest_framework import serializers

from routing.map_url import simplify_geometry
from routing.pipeline.bbox import is_valid as bbox_is_valid

MAX_ADDRESS_LENGTH = 256


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


class RouteRequestSerializer(serializers.Serializer):
    """`{"start": <loc>, "finish": <loc>}` -- both fields polymorphic
    coordinate-or-address."""

    start = LocationField()
    finish = LocationField()


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

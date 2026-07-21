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


def _percent_repr(fraction):
    """Render a 0-to-1 Decimal fraction as a percentage number (e.g.
    `Decimal("0.125")` -> `12.5`), quantized to one decimal place and
    coerced to `float` so it survives `json.dumps` -- a raw `Decimal`
    does not. Returns `None` when `fraction` is `None` (a legitimate
    "cannot be computed" result, not a formatting gap)."""
    if fraction is None:
        return None
    d = fraction if isinstance(fraction, Decimal) else Decimal(str(fraction))
    return float((d * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _rationale_repr(instance) -> dict:
    """Render a `FuelStop`'s rationale fields (set by the solver at the
    exact branch that produced the purchase -- see `PurchaseReason`) into
    a plain, JSON-safe dict. Every price in this object reuses
    `_quantize_money`; no new money formatter is introduced."""
    return {
        "purchase_reason": instance.purchase_reason,
        "reason_target_station_id": instance.reason_target_opis_id,
        "reason_target_name": instance.reason_target_name,
        "skipped_count": int(instance.skipped_count),
        "skipped_avg_price": (
            _quantize_money(instance.skipped_avg_price)
            if instance.skipped_avg_price is not None
            else None
        ),
        "corridor_avg_price": (
            _quantize_money(instance.corridor_avg_price)
            if instance.corridor_avg_price is not None
            else None
        ),
        "price_percentile": _percent_repr(instance.price_percentile),
    }


def _duration_repr(value):
    """Coerce a duration in seconds to a plain `int`, `None` when `value`
    is `None`. Seconds are not a money/gallon/mile quantity, so this
    deliberately does not introduce a fourth quantizer -- an inline
    rounded integer is the right granularity here."""
    if value is None:
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _vehicle_repr(vehicle) -> dict:
    """Echo the resolved vehicle profile plus a derived `starting_fuel_mi`
    (D-04) -- what makes the free-full-tank assumption visible instead of
    looking like a bug on a short trip."""
    mpg = vehicle["mpg"]
    tank_range_mi = vehicle["tank_range_mi"]
    starting_fuel = vehicle["starting_fuel"]
    starting_fuel_mi = starting_fuel * tank_range_mi
    return {
        "mpg": str(mpg),
        "tank_range_mi": _quantize_miles(tank_range_mi),
        "starting_fuel": str(starting_fuel),
        "starting_fuel_mi": _quantize_miles(starting_fuel_mi),
    }


def _legs_repr(legs) -> list:
    """Render a `routing.services.legs.Leg` list. `from`/`to` are read
    from `from_name`/`to_name` -- `Leg` cannot use `from` as a field name
    since it is a Python keyword."""
    return [
        {
            "from": leg.from_name,
            "to": leg.to_name,
            "distance_mi": _quantize_miles(leg.distance_mi),
            "duration_s": _duration_repr(leg.duration_s),
            "gallons": _quantize_gallons(leg.gallons),
            "cost": _quantize_money(leg.cost),
        }
        for leg in legs
    ]


def _savings_repr(savings):
    """Render a `routing.services.naive_baseline.Savings` into the D-16
    shape, or `None` when `savings` itself is `None` (the naive baseline
    never solved -- see `savings_note`, a sibling top-level key, not
    nested here)."""
    if savings is None:
        return None
    return {
        "amount": _quantize_money(savings.amount),
        "percent": _percent_repr(savings.percent),
        "naive_total_cost": _quantize_money(savings.naive_total_cost),
        "naive_total_gallons": _quantize_gallons(savings.naive_total_gallons),
        "naive_stop_count": int(savings.naive_stop_count),
    }


def _alternatives_repr(alternatives) -> list:
    """Render the compact D-11 alternatives comparison array -- five
    scalar keys per entry, no geometry, no stop list. `total_cost` is
    `None` for an infeasible alternative rather than the entry being
    omitted."""
    return [
        {
            "total_route_mi": _quantize_miles(a["total_route_mi"]),
            "duration_s": _duration_repr(a.get("duration_s")),
            "total_cost": (
                _quantize_money(a["total_cost"])
                if a.get("total_cost") is not None
                else None
            ),
            "chosen": bool(a["chosen"]),
            "feasible": bool(a["feasible"]),
        }
        for a in alternatives
    ]


def _candidate_stations_repr(candidates, candidate_coords) -> list:
    """Render the corridor's `candidate_stations[]` array (D-09/D-10): a
    lean five-field entry per in-corridor candidate station -- no
    `name`, no `address`. Reuses `_quantize_money`/`_quantize_miles`; no
    new formatter is introduced.

    A candidate whose `opis_id` is `None`, or one with no resolvable
    row in `candidate_coords`, has no coordinates and cannot be placed
    on the map -- it is filtered out entirely here. This is a
    deliberate divergence from `fuel_stops[]`, which keeps
    null-`station_id` stops since they still render in the stop list
    by index."""
    result = []
    for candidate in candidates:
        coords = (
            candidate_coords.get(candidate.opis_id)
            if candidate.opis_id is not None
            else None
        )
        if coords is None:
            continue
        result.append(
            {
                "station_id": candidate.opis_id,
                "lat": float(coords["latitude"]),
                "lng": float(coords["longitude"]),
                "price_per_gallon": _quantize_money(candidate.price_per_gallon),
                "distance_from_start_mi": _quantize_miles(
                    candidate.distance_from_start_mi
                ),
            }
        )
    return result


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

    Every stop also carries a `rationale` object (`_rationale_repr`) --
    structured, English-prose-free facts (`purchase_reason`,
    `reason_target_station_id`/`reason_target_name`,
    `skipped_count`/`skipped_avg_price`, `price_percentile`,
    `corridor_avg_price`) explaining why the stop happened and for how
    much. Every value in it was computed by the solver at the branch that
    produced the purchase -- this class re-derives nothing, it only
    formats.
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
            "rationale": _rationale_repr(instance),
        }


class RouteResponseSerializer(serializers.Serializer):
    """Renders the full computed response payload -- a pure formatter.
    Every value it renders was computed upstream by the solver, the leg
    builder (`routing.services.legs`), or the naive baseline
    (`routing.services.naive_baseline`); this class re-derives nothing.

    Instance-dict contract (what plan 07-08's orchestrator must
    populate):

    - `"route"` (required): the winning `routing.services.mapbox.Route`.
    - `"plan"` (required): the winning `routing.services.solver.FuelPlan`.
    - `"map_url"` (optional): the Static Images URL, or `None`.
    - `"vehicle"` (optional): the resolved profile dict with `"mpg"`,
      `"tank_range_mi"`, `"starting_fuel"` (Decimal values) -- `None`/
      absent renders `vehicle: null`.
    - `"legs"` (optional): a list of `routing.services.legs.Leg` --
      absent renders `legs: []`.
    - `"savings"` (optional): a `routing.services.naive_baseline.Savings`
      or `None` -- `None`/absent renders `savings: null`.
    - `"savings_note"` (optional): a string explaining a `None` savings
      (e.g. `"naive_plan_infeasible"`), or `None`.
    - `"alternatives"` (optional): a list of plain dicts, each with
      `"total_route_mi"`, `"duration_s"`, `"total_cost"` (or `None` when
      infeasible), `"chosen"`, `"feasible"` -- absent renders
      `alternatives: []` and `alternatives_considered: 0`.

    `self.context` may carry `"stop_coords"` (see `FuelStopSerializer`),
    `"start_coords"`, and `"finish_coords"` (each a
    `{"latitude", "longitude"}` dict), all injected by the orchestrator.
    It may also carry `"candidates"` (the winning alternative's
    corridor-filtered `Candidate` list) and `"candidate_coords"` (an
    opis_id-keyed `{"latitude", "longitude"}` map, same shape as
    `stop_coords`) -- both render the additive `candidate_stations[]`
    array (D-09/D-10: an amendment to Phase 7 D-11's "no station lists"
    stance, scoped to corridor candidates for map rendering, not the
    alternatives array). Absent context renders `candidate_stations: []`.

    Every new key is read from `instance` with a `.get()` default, so an
    instance shaped with only the v1.0 keys (`"route"`, `"plan"`,
    `"map_url"`) still serializes rather than raising -- this is what
    keeps a v1.0 client's response shape additive-only.

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

        vehicle = instance.get("vehicle")
        legs = instance.get("legs") or []
        savings = instance.get("savings")
        alternatives = instance.get("alternatives") or []
        candidates = self.context.get("candidates") or []
        candidate_coords = self.context.get("candidate_coords") or {}
        freshness = price_freshness()

        return {
            "start": _location_repr(self.context.get("start_coords")),
            "finish": _location_repr(self.context.get("finish_coords")),
            "route_geometry": simplify_geometry(route),
            "total_route_mi": _quantize_miles(route.total_route_mi),
            "fuel_stops": fuel_stops,
            "total_cost": _quantize_money(plan.total_cost),
            "total_gallons": _quantize_gallons(plan.total_gallons),
            "map_url": instance.get("map_url"),
            "vehicle": _vehicle_repr(vehicle) if vehicle is not None else None,
            "legs": _legs_repr(legs),
            "total_duration_s": _duration_repr(route.duration_s),
            "fuel_stop_count": len(plan.stops),
            "savings": _savings_repr(savings),
            "savings_note": instance.get("savings_note"),
            "alternatives_considered": len(alternatives),
            "alternatives": _alternatives_repr(alternatives),
            "candidate_stations": _candidate_stations_repr(
                candidates, candidate_coords
            ),
            "price_as_of": freshness["price_as_of"],
            "price_data_note": freshness["price_data_note"],
        }

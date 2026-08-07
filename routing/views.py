"""RouteView: the `POST /api/route` orchestrator.

A composition of already-tested seams -- validate, cache-aside, resolve
endpoints (coordinate pass-through or geocode + bounds re-check) for
`start`, `finish`, and every intermediate `waypoints[]` entry in visit
order, `get_routes` (one Mapbox Directions call over the whole ordered
stop list, returning up to three route alternatives), a corridor pass
and solve per alternative, deterministic winner selection, a baseline
pass on the winner, leg assembly, build_map_url, serialize, cache.set.
An all-address multi-stop request resolves each waypoint through its
own `geocode()` call (bounded by the serializer's `waypoints`
`max_length`), still only ever one Directions call regardless of stop
count.

`post()` itself is never wrapped in try/except: domain exceptions
(`RouteNotFoundError`, `MapboxRequestError`, `InfeasibleRouteError`,
`InvalidRouteInputError`, `ImproperlyConfigured`) propagate uncaught to
`routing.exceptions.custom_exception_handler`, the sole translation layer
from those exceptions to HTTP. Two private helpers deliberately contain
the project's only two sanctioned try/except clauses, both narrowed to
`InfeasibleRouteError` alone -- never widened into a catch-all clause:
`_solve_all_alternatives` skips an infeasible alternative and re-raises
only the smallest-gap failure once every alternative has failed;
`_baseline_savings` catches a baseline-only infeasibility and returns a
`savings_note` instead, since a baseline failure must never break a
request that already has a valid optimized answer.

Per-stage durations are collected via `routing.timing.ServerTiming`
context managers, attached to `self._timing` at the top of `post()` --
one instance covers the whole request, from the `eia` factor-table
resolution through the cache lookup to the full solve, so both a
cache-hit response (`eia`, `cache`) and a cache-miss response (`eia`,
`cache`, `route`, `corridor`, `solver`, `baseline`, `total`) report the
`eia` stage. This also means DRF's exception-handler context
(`context["view"]`) can read partial timings after a domain exception
propagates. `_Stage.__exit__` records even on exception and never
suppresses it, so this instrumentation preserves the pipeline's
no-try/except shape. A stage entered more than once (e.g.
"corridor"/"solver" once per route alternative) accumulates into one
running total rather than overwriting.

`RouteView.throttle_classes` attaches rate limiting declaratively and
per-view (`routing.throttles.RouteBurstThrottle` +
`RouteSustainedThrottle`) rather than through a global DRF default, so
`HealthView`/`ReadyView` are never throttled.
"""
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.http import FileResponse, JsonResponse
from django.views import View
from drf_spectacular.utils import OpenApiExample, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.cache import build_cache_key
from routing.map_url import build_map_url
from routing.models import Station
from routing.pipeline.bbox import is_valid as bbox_is_valid
from routing.serializers import (
    RouteRequestSerializer,
    RouteResponseSerializer,
    price_freshness,
)
from routing.services import corridor, dp, eia, naive_baseline, regions, solver
from routing.services.exceptions import InfeasibleRouteError
from routing.services.legs import build_legs, build_waypoint_markers
from routing.services.mapbox import Route, geocode, get_routes
from routing.services.multi_leg import flatten_route
from routing.throttles import RouteBurstThrottle, RouteSustainedThrottle
from routing.timing import ServerTiming

# Standard Django per-module logger (18.1-06's D-08 breach signal is the
# first thing in this file to need one).
logger = logging.getLogger(__name__)

# Sentinel substituted for a `None` route duration in `_select_winner`'s
# tie-break key -- keeps the four-level comparison total even if a
# route ever carries no duration, without ever preferring it over a route
# with a real one.
_NULL_DURATION_SENTINEL = Decimal("Infinity")


@dataclass(frozen=True)
class _AlternativeResult:
    """One route alternative's solve outcome.

    `candidates` is the corridor-filtered candidate list used for this
    alternative's solve -- kept here so `_baseline_savings` never needs
    to recompute the corridor pass for the winner.
    """

    index: int
    route: Route
    plan: solver.FuelPlan | None
    feasible: bool
    candidates: list


class HealthView(APIView):
    """`GET /api/health` -- dependency-free liveness probe for the Docker
    Compose healthcheck. Deliberately touches no DB, cache, or Mapbox so it
    succeeds even with an empty database and no MAPBOX_TOKEN set.

    Also reports `commit`: the git SHA of the running build, read from the
    `BUILD_COMMIT` setting (Render's injected `RENDER_GIT_COMMIT`), or `None`
    wherever that variable is absent. Reading one already-resolved setting
    keeps the probe dependency-free -- no DB, cache, or network is touched.
    `.github/workflows/smoke.yml` polls this field to wait until the build
    under test is the one actually serving, instead of racing Render's
    out-of-band deploy with a fixed sleep and asserting against whichever
    container happens to answer.
    """

    def get(self, request):
        return Response({"status": "ok", "commit": settings.BUILD_COMMIT})


class ReadyView(APIView):
    """`GET /api/ready` -- the dependency-aware readiness probe Render
    gates traffic routing on, in contrast to `HealthView`'s
    dependency-free liveness check consumed by the Docker Compose
    healthcheck and the external keep-warm pinger.

    Reports three independent booleans -- db connectivity, cache
    round-trip, and Mapbox token configuration -- and never makes a
    live Mapbox call, so the probe stays sub-100ms and costs zero API
    budget even though Render polls it continuously.

    The db and cache checks are wrapped in narrow `except Exception`
    clauses that collapse any failure to `False` -- a third sanctioned,
    documented deviation from this app's no-try/except-in-views norm
    (alongside `RouteView`'s two). A DB or cache driver's exception
    message can carry a hostname, username, or connection string, and
    this endpoint's body is publicly reachable, so failures are
    reported as booleans and nothing else ever crosses that boundary.
    """

    def get(self, request):
        db_ok = self._check_db()
        cache_ok = self._check_cache()
        tokens_ok = self._check_tokens()

        station_count = Station.objects.count() if db_ok else None

        all_ok = db_ok and cache_ok and tokens_ok
        payload = {
            "status": "ready" if all_ok else "not_ready",
            "checks": {"db": db_ok, "cache": cache_ok, "tokens": tokens_ok},
            "station_count": station_count,
        }
        return Response(payload, status=200 if all_ok else 503)

    def _check_db(self):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return True
        except Exception:
            return False

    def _check_cache(self):
        try:
            probe_key = "_ready_probe"
            cache.set(probe_key, "ok", timeout=5)
            return cache.get(probe_key) == "ok"
        except Exception:
            return False

    def _check_tokens(self):
        token = settings.MAPBOX_TOKEN
        public_token = settings.MAPBOX_PUBLIC_TOKEN
        return bool(token) and bool(public_token) and public_token.startswith("pk.")


class ConfigView(APIView):
    """`GET /api/config` -- serves the browser-facing `pk.` public token so
    the SPA can initialize Mapbox GL JS at runtime, plus the fuel-price
    dataset vintage so the frontend needs no second request for it.

    Declares no `throttle_classes` attribute, exactly like `HealthView`/
    `ReadyView` -- this stays unthrottled by construction since it gates
    first paint and does no DB or Mapbox work.

    Reuses `routing/map_url.py`'s exact two-condition validation: raises
    `ImproperlyConfigured` when `MAPBOX_PUBLIC_TOKEN` is unset, and again
    when it does not start with `pk.` -- never caught here, so it
    propagates to `routing.exceptions.custom_exception_handler` per the
    no-try/except view-pipeline discipline (see module docstring). Reads
    only the public token setting; the secret `MAPBOX_TOKEN` is never
    referenced by this view.
    """

    def get(self, request):
        token = settings.MAPBOX_PUBLIC_TOKEN
        if not token:
            raise ImproperlyConfigured(
                "MAPBOX_PUBLIC_TOKEN is not set; /api/config cannot serve a token."
            )
        if not token.startswith("pk."):
            raise ImproperlyConfigured(
                "MAPBOX_PUBLIC_TOKEN must start with 'pk.'; a secret token may "
                "have been pasted into the public token slot."
            )
        freshness = price_freshness()
        return Response(
            {
                "mapbox_public_token": token,
                "price_as_of": freshness["price_as_of"],
                "price_data_note": freshness["price_data_note"],
            }
        )


class SpaFallbackView(View):
    """`GET <any path not under api/ or static/>` -- serves the built SPA's
    `index.html` so client-side routes (e.g. `/trip/abc123`) render the app
    instead of a Django 404.

    WhiteNoise's middleware already serves real files -- the SPA's hashed
    assets and `index.html` for exact directory requests, via
    `WHITENOISE_INDEX_FILE` -- directly from the WSGI stack, before the URL
    resolver is ever reached (see MIDDLEWARE ordering in `base.py`). This
    view exists only for the remainder: client-side routes that are not
    files on disk. Returning the file directly with `FileResponse`, rather
    than rendering it through `TemplateView`, keeps Vite's build output out
    of the Django template engine entirely -- and it means a fresh clone
    with no SPA build present degrades to a clear, explicit 404 instead of
    a `TemplateDoesNotExist` error.

    A plain `django.views.View`, not a DRF `APIView` -- this serves a
    static file, not an API resource, so it stays out of the DRF view
    hierarchy on purpose.
    """

    def get(self, request, *args, **kwargs):
        index_path = Path(settings.WHITENOISE_ROOT) / "index.html"
        if not index_path.exists():
            return JsonResponse(
                {
                    "error": {
                        "code": "spa_build_missing",
                        "message": (
                            "The SPA build is missing. Run `npm run build` "
                            "in frontend/ to generate frontend/dist."
                        ),
                    }
                },
                status=404,
            )
        return FileResponse(index_path.open("rb"), content_type="text/html")


# OpenAPI response documentation for RouteView.post.
# RouteResponseSerializer.to_representation (routing/serializers.py) builds
# its dict by hand rather than declaring fields, so drf-spectacular's
# auto-introspection sees nothing to walk and would otherwise document the
# response as an empty/opaque object. This tree mirrors that
# method's real top-level keys, and every nested helper's real keys, exactly
# -- it only documents the response, it never participates in producing it.
_FUEL_STOP_RATIONALE_SCHEMA = inline_serializer(
    name="FuelStopRationale",
    fields={
        "purchase_reason": serializers.CharField(),
        "reason_target_station_id": serializers.IntegerField(allow_null=True),
        "reason_target_name": serializers.CharField(allow_null=True),
        "skipped_count": serializers.IntegerField(),
        "skipped_avg_price": serializers.CharField(allow_null=True),
        "corridor_avg_price": serializers.CharField(allow_null=True),
        "price_percentile": serializers.FloatField(allow_null=True),
        "bypassed_estimate_count": serializers.IntegerField(),
        "bypassed_estimate_saving_forgone": serializers.CharField(allow_null=True),
    },
)

_FUEL_STOP_SCHEMA = inline_serializer(
    name="FuelStop",
    fields={
        "name": serializers.CharField(),
        "station_id": serializers.IntegerField(allow_null=True),
        "location": inline_serializer(
            name="FuelStopLocation",
            fields={
                "latitude": serializers.CharField(allow_null=True),
                "longitude": serializers.CharField(allow_null=True),
            },
            allow_null=True,
        ),
        "distance_from_start_mi": serializers.CharField(),
        "price_per_gallon": serializers.CharField(),
        "gallons": serializers.CharField(),
        "cost": serializers.CharField(),
        "rationale": _FUEL_STOP_RATIONALE_SCHEMA,
        "price_source": serializers.CharField(allow_null=True),
    },
    many=True,
)

_VEHICLE_SCHEMA = inline_serializer(
    # "VehicleResponse", not "Vehicle" -- avoids a component-name collision
    # with the request-side VehicleSerializer (routing/serializers.py),
    # which has a different shape (no starting_fuel_mi).
    name="VehicleResponse",
    fields={
        "mpg": serializers.CharField(),
        "tank_range_mi": serializers.CharField(),
        "starting_fuel": serializers.CharField(),
        "starting_fuel_mi": serializers.CharField(),
    },
    allow_null=True,
)

_LEG_SCHEMA = inline_serializer(
    name="Leg",
    fields={
        "from": serializers.CharField(),
        "to": serializers.CharField(),
        "distance_mi": serializers.CharField(),
        "duration_s": serializers.IntegerField(allow_null=True),
        "gallons": serializers.CharField(),
        "cost": serializers.CharField(),
    },
    many=True,
)

_SAVINGS_SCHEMA = inline_serializer(
    name="Savings",
    fields={
        "amount": serializers.CharField(),
        "percent": serializers.FloatField(allow_null=True),
        "naive_total_cost": serializers.CharField(),
        "naive_total_gallons": serializers.CharField(),
        "naive_stop_count": serializers.IntegerField(),
    },
    allow_null=True,
)

_ALTERNATIVE_SCHEMA = inline_serializer(
    name="RouteAlternative",
    fields={
        "total_route_mi": serializers.CharField(),
        "duration_s": serializers.IntegerField(allow_null=True),
        "total_cost": serializers.CharField(allow_null=True),
        "chosen": serializers.BooleanField(),
        "feasible": serializers.BooleanField(),
    },
    many=True,
)

_CANDIDATE_STATION_SCHEMA = inline_serializer(
    name="CandidateStation",
    fields={
        "station_id": serializers.IntegerField(),
        "lat": serializers.FloatField(),
        "lng": serializers.FloatField(),
        "price_per_gallon": serializers.CharField(),
        "distance_from_start_mi": serializers.CharField(),
    },
    many=True,
)

_WAYPOINT_SCHEMA = inline_serializer(
    name="Waypoint",
    fields={
        "label": serializers.CharField(),
        "name": serializers.CharField(),
        "lat": serializers.FloatField(),
        "lng": serializers.FloatField(),
        "distance_from_start_mi": serializers.CharField(),
        "duration_s": serializers.IntegerField(allow_null=True),
    },
    many=True,
)

_ROUTE_RESPONSE_SCHEMA = inline_serializer(
    name="RouteResponse",
    fields={
        "start": inline_serializer(
            name="StartLocation",
            fields={
                "latitude": serializers.CharField(allow_null=True),
                "longitude": serializers.CharField(allow_null=True),
            },
            allow_null=True,
        ),
        "finish": inline_serializer(
            name="FinishLocation",
            fields={
                "latitude": serializers.CharField(allow_null=True),
                "longitude": serializers.CharField(allow_null=True),
            },
            allow_null=True,
        ),
        "route_geometry": serializers.ListField(
            child=serializers.ListField(child=serializers.FloatField())
        ),
        "total_route_mi": serializers.CharField(),
        "fuel_stops": _FUEL_STOP_SCHEMA,
        "total_cost": serializers.CharField(),
        "total_gallons": serializers.CharField(),
        "map_url": serializers.CharField(allow_null=True),
        "vehicle": _VEHICLE_SCHEMA,
        "legs": _LEG_SCHEMA,
        "total_duration_s": serializers.IntegerField(allow_null=True),
        "fuel_stop_count": serializers.IntegerField(),
        "savings": _SAVINGS_SCHEMA,
        "savings_note": serializers.CharField(allow_null=True),
        "alternatives_considered": serializers.IntegerField(),
        "alternatives": _ALTERNATIVE_SCHEMA,
        "candidate_stations": _CANDIDATE_STATION_SCHEMA,
        "waypoints": _WAYPOINT_SCHEMA,
        "price_as_of": serializers.CharField(),
        "price_data_note": serializers.CharField(),
        "station_data_note": serializers.CharField(),
        "price_index_status": serializers.CharField(),
        "eia_week": serializers.CharField(allow_null=True),
        "trend_region": serializers.CharField(allow_null=True),
        "trend_delta_cents": serializers.IntegerField(allow_null=True),
        "solver_strategy": serializers.CharField(allow_null=True),
    },
)

# Real values, not invented: the request mirrors README's committed Dallas
# -> Los Angeles curl example; the response reuses that same example's
# committed fields (start/finish/route_geometry/total_route_mi/fuel_stops/
# total_cost/total_gallons/map_url) and fills in the v2.0-only fields
# (vehicle/legs/savings/alternatives/candidate_stations) with values
# consistent with that same request -- the API-default 10mpg/500mi vehicle,
# since the README example's request carries no "vehicle" key.
_ROUTE_RESPONSE_EXAMPLE = OpenApiExample(
    "Dallas to Los Angeles",
    value={
        "start": {"latitude": "32.7767", "longitude": "-96.7970"},
        "finish": {"latitude": "34.0522", "longitude": "-118.2437"},
        "route_geometry": [[-96.796754, 32.775944], [-96.845799, 32.764037]],
        "total_route_mi": "1437",
        "fuel_stops": [
            {
                "name": "One9 #1248",
                "station_id": 63669,
                "location": {"latitude": "32.59742800", "longitude": "-96.68090500"},
                "distance_from_start_mi": "63",
                "price_per_gallon": "2.76",
                "gallons": "0.07",
                "cost": "0.18",
                "rationale": {
                    "purchase_reason": "top_up_at_cheapest",
                    "reason_target_station_id": 66689,
                    "reason_target_name": "ROSCOE TRAVEL PLAZA",
                    "skipped_count": 0,
                    "skipped_avg_price": None,
                    "corridor_avg_price": "2.98",
                    "price_percentile": 8.0,
                    "bypassed_estimate_count": 0,
                    "bypassed_estimate_saving_forgone": None,
                },
                "price_source": "opis_indexed",
            },
            {
                "name": "ROSCOE TRAVEL PLAZA",
                "station_id": 66689,
                "location": {"latitude": "32.44193400", "longitude": "-100.53223100"},
                "distance_from_start_mi": "218",
                "price_per_gallon": "2.76",
                "gallons": "12.61",
                "cost": "34.80",
                "rationale": {
                    "purchase_reason": "cheapest_in_range",
                    "reason_target_station_id": None,
                    "reason_target_name": None,
                    "skipped_count": 2,
                    "skipped_avg_price": "2.91",
                    "corridor_avg_price": "2.98",
                    "price_percentile": 5.0,
                    "bypassed_estimate_count": 0,
                    "bypassed_estimate_saving_forgone": None,
                },
                "price_source": "opis_indexed",
            },
        ],
        "total_cost": "260.42",
        "total_gallons": "93.73",
        "map_url": (
            "https://api.mapbox.com/styles/v1/mapbox/streets-v12/static/"
            "pin-s-a+3b82f6(-96.7970,32.7767),pin-s-b+22c55e(-118.2437,34.0522),"
            "path-3+ef4444-0.8(...)/auto/600x400?access_token=pk.***REDACTED***"
        ),
        "vehicle": {
            "mpg": "10.00",
            "tank_range_mi": "500.0",
            "starting_fuel": "1.000",
            "starting_fuel_mi": "500",
        },
        "legs": [
            {
                "from": "START",
                "to": "One9 #1248",
                "distance_mi": "63",
                "duration_s": 3780,
                "gallons": "6.30",
                "cost": "17.39",
            },
            {
                "from": "One9 #1248",
                "to": "ROSCOE TRAVEL PLAZA",
                "distance_mi": "155",
                "duration_s": 9300,
                "gallons": "15.50",
                "cost": "42.78",
            },
        ],
        "total_duration_s": 86400,
        "fuel_stop_count": 5,
        "savings": {
            "amount": "18.30",
            "percent": 6.6,
            "naive_total_cost": "278.72",
            "naive_total_gallons": "93.73",
            "naive_stop_count": 4,
        },
        "savings_note": None,
        "alternatives_considered": 2,
        "alternatives": [
            {
                "total_route_mi": "1437",
                "duration_s": 86400,
                "total_cost": "260.42",
                "chosen": True,
                "feasible": True,
            },
            {
                "total_route_mi": "1462",
                "duration_s": 88200,
                "total_cost": "264.10",
                "chosen": False,
                "feasible": True,
            },
        ],
        "candidate_stations": [
            {
                "station_id": 63669,
                "lat": 32.597428,
                "lng": -96.680905,
                "price_per_gallon": "2.76",
                "distance_from_start_mi": "63",
            },
            {
                "station_id": 66689,
                "lat": 32.441934,
                "lng": -100.532231,
                "price_per_gallon": "2.76",
                "distance_from_start_mi": "218",
            },
        ],
        "waypoints": [
            {
                "label": "A",
                "name": "START",
                "lat": 32.7767,
                "lng": -96.7970,
                "distance_from_start_mi": "0",
                "duration_s": 0,
            },
            {
                "label": "B",
                "name": "FINISH",
                "lat": 34.0522,
                "lng": -118.2437,
                "distance_from_start_mi": "1437",
                "duration_s": 86400,
            },
        ],
        "price_as_of": "2025-01-01",
        "price_data_note": (
            "Fuel prices come from a static OPIS truck-stop snapshot with no "
            "per-row timestamp. Price levels are consistent with U.S. retail "
            "diesel of late 2024-early 2025."
        ),
        "station_data_note": "6,290 stations — all with recorded prices.",
        "price_index_status": "frozen",
        "eia_week": None,
        "trend_region": None,
        "trend_delta_cents": None,
        "solver_strategy": "exact_dp",
    },
    request_only=False,
    response_only=True,
)

_ROUTE_REQUEST_EXAMPLE = OpenApiExample(
    "Request with a vehicle override (Semi-loaded preset)",
    value={
        "start": "32.7767,-96.7970",
        "finish": "34.0522,-118.2437",
        "vehicle": {"mpg": "6.50", "tank_range_mi": "1050.0", "starting_fuel": "1.000"},
    },
    request_only=True,
    response_only=False,
)


class RouteView(APIView):
    """`POST /api/route` -- see module docstring."""

    throttle_classes = [RouteBurstThrottle, RouteSustainedThrottle]

    @extend_schema(
        request=RouteRequestSerializer,
        responses={200: _ROUTE_RESPONSE_SCHEMA},
        examples=[_ROUTE_RESPONSE_EXAMPLE, _ROUTE_REQUEST_EXAMPLE],
    )
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        self._timing = ServerTiming()
        with self._timing.stage("eia"):
            factor_table, price_index_status = eia.get_factor_table()
        eia_vintage = (
            factor_table["week"] if price_index_status != "frozen" else "frozen"
        )

        cache_key = build_cache_key(
            validated,
            eia_vintage=eia_vintage,
            penalty=settings.FUEL_STOP_PENALTY_USD,
            trust_margin=settings.TRUST_MARGIN_USD,
        )
        with self._timing.stage("cache"):
            cached = cache.get(cache_key)
        if cached is not None:
            response = Response(cached)
            response["Server-Timing"] = self._timing.header_value()
            return response

        with self._timing.stage("total"):
            vehicle = validated["vehicle"]
            start_coords = self._resolve_endpoint(validated["start"])
            finish_coords = self._resolve_endpoint(validated["finish"])
            waypoint_coords = [
                self._resolve_endpoint(waypoint)
                for waypoint in validated["waypoints"]
            ]

            with self._timing.stage("route"):
                ordered_coords = [
                    (start_coords["latitude"], start_coords["longitude"]),
                    *(
                        (w["latitude"], w["longitude"])
                        for w in waypoint_coords
                    ),
                    (finish_coords["latitude"], finish_coords["longitude"]),
                ]
                routes = get_routes(ordered_coords)

            factor_for = eia.make_factor_lookup(factor_table)
            results = self._solve_all_alternatives(
                routes, vehicle, factor_for, ordered_coords
            )
            winner = self._select_winner(results)

            stop_coords = self._stop_coords(winner.plan)
            ordered_stop_coords = [
                (stop_coords[s.opis_id]["latitude"], stop_coords[s.opis_id]["longitude"])
                for s in winner.plan.stops
                if s.opis_id is not None and s.opis_id in stop_coords
            ]

            candidate_opis_ids = [
                c.opis_id for c in winner.candidates if c.opis_id is not None
            ]
            candidate_coords = self._coords_for_opis_ids(candidate_opis_ids)
            region = self._route_dominant_region(winner, candidate_coords)
            trend_region, trend_delta_cents = self._region_trend(
                factor_table, price_index_status, region
            )

            map_url = build_map_url(
                winner.route,
                (start_coords["latitude"], start_coords["longitude"]),
                (finish_coords["latitude"], finish_coords["longitude"]),
                ordered_stop_coords,
            )

            legs = build_legs(winner.route, winner.plan)
            waypoint_markers = build_waypoint_markers(
                winner.route, ordered_coords, self._leg_boundaries_mi(winner.route)
            )
            savings, savings_note = self._baseline_savings(winner, vehicle)
            alternatives = [
                {
                    "total_route_mi": r.route.total_route_mi,
                    "duration_s": r.route.duration_s,
                    "total_cost": r.plan.total_cost if r.feasible else None,
                    "chosen": r.index == winner.index,
                    "feasible": r.feasible,
                }
                for r in results
            ]

            response_serializer = RouteResponseSerializer(
                {
                    "route": winner.route,
                    "plan": winner.plan,
                    "map_url": map_url,
                    "vehicle": vehicle,
                    "legs": legs,
                    "savings": savings,
                    "savings_note": savings_note,
                    "alternatives": alternatives,
                    "price_index_status": price_index_status,
                    "eia_week": factor_table["week"],
                    "trend_region": trend_region,
                    "trend_delta_cents": trend_delta_cents,
                    "waypoints": waypoint_markers,
                },
                context={
                    "start_coords": start_coords,
                    "finish_coords": finish_coords,
                    "stop_coords": stop_coords,
                    "candidates": winner.candidates,
                    "candidate_coords": candidate_coords,
                    "price_source_counts": corridor.price_source_counts(),
                },
            )
            payload = response_serializer.data

            cache.set(cache_key, payload, timeout=settings.CACHE_TTL_SECONDS)

        response = Response(payload)
        response["Server-Timing"] = self._timing.header_value()
        return response

    def _solve_all_alternatives(self, routes, vehicle, factor_for, ordered_stop_coords=None):
        """Solve every route alternative Mapbox returned, catching only
        `InfeasibleRouteError` per alternative -- the project's one
        sanctioned deviation from the no-try/except pipeline shape,
        contained here rather than smeared through `post()`. Any other
        exception type propagates uncaught.

        `factor_for`: the `state -> Decimal` EIA-index closure (see
        `routing.services.eia.make_factor_lookup`), threaded into every
        alternative's corridor pass so each candidate's price is
        indexed identically regardless of which alternative wins.

        `ordered_stop_coords`: the resolved `[start, *waypoints, finish]`
        coordinate list (additive, optional -- `None` for a caller with
        no multi-stop context), passed through to `_enrich_infeasible_leg`
        so the re-raised smallest-gap error can additively name its
        failing leg (D-07).

        Returns a list of `_AlternativeResult`, one per route, in Mapbox
        order. When every alternative is infeasible, re-raises the
        smallest-gap `InfeasibleRouteError` seen across all of them
        (enriched per `_enrich_infeasible_leg`), so the request reports
        the closest miss rather than an arbitrary one.
        """
        # The one-time process-level station-index build (Neon fetch +
        # point construction + STRtree build) is forced here, in its own
        # stage, BEFORE the per-alternative loop below. Previously this
        # build ran lazily inside the first `corridor.candidates()` call,
        # so its one-time cost was silently billed against the "corridor"
        # stage alongside genuine per-route geometry work -- a single
        # conflated number that could not be attributed to either cause.
        # With the index already warm, every per-alternative "corridor"
        # stage below measures geometry work only.
        with self._timing.stage("index"):
            corridor.warm_index()

        results = []
        smallest_gap_exc = None
        smallest_gap_route = None
        smallest_gap_cands = None
        for index, route in enumerate(routes):
            with self._timing.stage("corridor"):
                cands = corridor.candidates(route, factor_for=factor_for)
            try:
                with self._timing.stage("solver"):
                    plan = solver.solve(
                        cands,
                        route.total_route_mi,
                        tank_range_mi=vehicle["tank_range_mi"],
                        mpg=vehicle["mpg"],
                        starting_fuel=vehicle["starting_fuel"],
                        penalty=settings.FUEL_STOP_PENALTY_USD,
                        trust_margin=settings.TRUST_MARGIN_USD,
                    )
                if plan.deadline_breached:
                    # D-08: the breach is visible via a Server-Timing
                    # metric and a structured log line ONLY -- no response
                    # field, no new `solver_strategy` value, no frontend
                    # touch. The demoted-vs-breached distinction is already
                    # DERIVABLE from the (estimate, strategy) pair (see
                    # `FuelPlan`/`DeadlineExceededError`'s own docstrings
                    # for the full reasoning), so nothing needs to carry it
                    # on the wire -- a response field would store what can
                    # already be derived, the weakest possible
                    # justification for permanent public API surface under
                    # an additive-only contract. Named, accepted cost: a
                    # cached breach plan carries no marker, because
                    # Server-Timing is per-request and a cache hit performs
                    # no solve.
                    self._timing.record(
                        "dp_deadline_breach",
                        plan.deadline_breach_elapsed_s * 1000,
                    )
                    # Hygiene (matches probe_live_latency.py's own rule
                    # that only status, wall time and parsed stage
                    # durations ever reach stdout/logs): only the
                    # route-alternative index and the exception's own
                    # measured numbers are logged -- no request body, no
                    # endpoint coordinates, no address string, no Mapbox
                    # token.
                    logger.warning(
                        "DP deadline breach on route alternative %d: "
                        "%.3fs elapsed against a %ss deadline",
                        index,
                        plan.deadline_breach_elapsed_s,
                        dp.DP_DEADLINE_SECONDS,
                    )
            except InfeasibleRouteError as exc:
                results.append(
                    _AlternativeResult(
                        index=index,
                        route=route,
                        plan=None,
                        feasible=False,
                        candidates=cands,
                    )
                )
                if smallest_gap_exc is None or exc.gap_mi < smallest_gap_exc.gap_mi:
                    smallest_gap_exc = exc
                    smallest_gap_route = route
                    smallest_gap_cands = cands
                continue
            results.append(
                _AlternativeResult(
                    index=index, route=route, plan=plan, feasible=True, candidates=cands
                )
            )

        if not any(r.feasible for r in results):
            raise self._enrich_infeasible_leg(
                smallest_gap_exc, smallest_gap_route, smallest_gap_cands, ordered_stop_coords
            )

        return results

    def _leg_boundaries_mi(self, route):
        """Cumulative-miles-before-each-leg scale for `route`, mirroring
        `corridor.candidates()`'s own single-vs-multi-leg dispatch: a
        genuinely multi-leg route (more than one Mapbox leg) delegates
        to `flatten_route` (13-01); a single-leg route -- a plain
        2-stop trip, or any `Route` predating the leg fields (a common
        shape in this test suite's hand-built doubles) -- never needs
        the flattening machinery, since it has exactly one leg whose
        boundary is always mile 0. Calling `flatten_route` on a
        single-leg route is also unsafe when that route's annotation
        arrays are empty (a legitimate "Mapbox returned no
        per-segment detail" shape, see `EmptyAnnotationTests` in
        `test_legs.py`): its coordinate slice would degenerate to a
        single point, which shapely's `LineString` rejects.
        """
        if len(route.leg_distances_mi) > 1:
            return flatten_route(route).leg_boundaries_mi
        return [Decimal(0)]

    def _enrich_infeasible_leg(self, exc, route, cands, ordered_stop_coords):
        """D-07: additively enrich the smallest-gap `InfeasibleRouteError`
        with the user-stop leg boundary its gap falls within -- computed
        here, in the view layer, from the same flattened distance scale
        `corridor.candidates()` used for `route` (13-01's
        `flatten_route`). The solver itself never sees or produces this
        data (AST purity gate, `routing/tests/test_boundaries.py`).

        Resolves the gap's start position (`from_mi`) from data already
        available at this call site: `to_mi` is `route.total_route_mi`
        when the gap's `to_station` is the "FINISH" sentinel, otherwise
        the nearest same-named candidate's own `distance_from_start_mi`
        (`solver.solve` always names its next-node target by a real
        candidate or the "FINISH" sentinel, never "START"); `from_mi` is
        then `to_mi - exc.gap_mi`, exact by construction (`gap_mi` is
        defined as exactly that difference in `solver.solve`).

        Returns `exc` UNCHANGED (`leg_index`/`leg_coords` stay `None`)
        when there is nothing to enrich against: a single-leg route (a
        plain 2-stop trip has exactly one Mapbox leg -- naming "the"
        leg adds no information), no `ordered_stop_coords` (a caller
        with no multi-stop context, e.g. an existing direct unit test),
        or a `to_station` that cannot be resolved to a real position.
        """
        if not ordered_stop_coords or len(route.leg_distances_mi) <= 1:
            return exc

        if exc.to_station == "FINISH":
            to_mi = route.total_route_mi
        else:
            matches = [c.distance_from_start_mi for c in cands if c.name == exc.to_station]
            if not matches:
                return exc
            to_mi = min(matches)

        from_mi = to_mi - exc.gap_mi

        leg_boundaries_mi = self._leg_boundaries_mi(route)
        leg_index = 0
        for i, boundary in enumerate(leg_boundaries_mi):
            if from_mi >= boundary:
                leg_index = i
        leg_index = min(leg_index, len(ordered_stop_coords) - 2)

        return InfeasibleRouteError(
            from_station=exc.from_station,
            to_station=exc.to_station,
            gap_mi=exc.gap_mi,
            max_range_mi=exc.max_range_mi,
            leg_index=leg_index,
            leg_coords=(
                (
                    float(ordered_stop_coords[leg_index][0]),
                    float(ordered_stop_coords[leg_index][1]),
                ),
                (
                    float(ordered_stop_coords[leg_index + 1][0]),
                    float(ordered_stop_coords[leg_index + 1][1]),
                ),
            ),
        )

    def _select_winner(self, results):
        """Return the feasible `_AlternativeResult` minimal under the
        four-level deterministic key: total cost, then total route
        miles, then duration seconds, then Mapbox's own ordinal index.
        A plain `min(...)` over a tuple key -- no weighted scoring, so
        the same request always resolves to the same winner."""
        feasible = [r for r in results if r.feasible]
        return min(
            feasible,
            key=lambda r: (
                r.plan.total_cost,
                r.route.total_route_mi,
                r.route.duration_s
                if r.route.duration_s is not None
                else _NULL_DURATION_SENTINEL,
                r.index,
            ),
        )

    def _baseline_savings(self, winner, vehicle):
        """Run the price-blind naive baseline against the winning
        alternative's candidate set only, isolating fueling strategy as
        the sole variable against the optimized plan. The project's
        second and last sanctioned try/except: a baseline
        `InfeasibleRouteError` must never break a request that already
        has a valid optimized answer -- it returns an explicit
        `savings_note` instead of propagating."""
        try:
            with self._timing.stage("baseline"):
                naive_plan = naive_baseline.solve(
                    winner.candidates,
                    winner.route.total_route_mi,
                    tank_range_mi=vehicle["tank_range_mi"],
                    mpg=vehicle["mpg"],
                    starting_fuel=vehicle["starting_fuel"],
                )
        except InfeasibleRouteError:
            return None, "naive_plan_infeasible"
        return naive_baseline.compute_savings(winner.plan, naive_plan), None

    def _resolve_endpoint(self, endpoint):
        """Resolve a validated `{"kind": "coordinate"|"address", ...}`
        endpoint into a `{"latitude", "longitude"}` Decimal dict.

        Coordinate inputs are already bounds-checked at the serializer
        (`LocationField`). Address inputs are resolved via exactly
        one `mapbox.geocode()` call and re-bounds-checked here,
        since a geocoded result can resolve outside the continental US
        independent of Mapbox's own `country=us` filter.
        """
        if endpoint["kind"] == "coordinate":
            return {"latitude": endpoint["lat"], "longitude": endpoint["lng"]}

        with self._timing.stage("geocode"):
            lat, lng = geocode(endpoint["value"])
        if not bbox_is_valid(lat, lng):
            raise serializers.ValidationError(
                f"Geocoded address resolved outside the continental US: "
                f"({lat}, {lng})."
            )
        return {"latitude": lat, "longitude": lng}

    def _stop_coords(self, plan):
        """One indexed `filter(opis_id__in=...)` query for every stop's
        lat/lng -- never a per-stop `.get()` in a loop."""
        opis_ids = [s.opis_id for s in plan.stops if s.opis_id is not None]
        return self._coords_for_opis_ids(opis_ids)

    def _coords_for_opis_ids(self, opis_ids):
        """Sibling of `_stop_coords`, generalized to any list of
        `opis_id`s -- same one indexed `filter(opis_id__in=...)` query
        shape, reused for the corridor's (potentially hundreds-long)
        `candidate_stations[]` lookup so that pass never runs a
        per-candidate query either. Also carries `state` (additive) --
        `_route_dominant_region` maps it to an EIA region for the trend
        chip; existing `latitude`/`longitude` consumers are unaffected.
        """
        if not opis_ids:
            return {}
        stations = Station.objects.filter(opis_id__in=opis_ids)
        return {
            station.opis_id: {
                "latitude": station.latitude,
                "longitude": station.longitude,
                "state": station.state,
            }
            for station in stations
        }

    def _route_dominant_region(self, winner, candidate_coords):
        """Route-dominant EIA region (D-06): the plurality region among
        the winning plan's chosen-stop states (in stop order), falling
        back to the state of the closest-to-start candidate when the
        plan has zero stops, and to `None` when there are no candidates
        at all. A stop/candidate whose state maps to no known region
        (D-03) is excluded from the plurality vote rather than counted
        as its own "unknown" region.
        """
        stop_region_codes = [
            region
            for region in (
                regions.region_for_state(candidate_coords[s.opis_id]["state"])
                for s in winner.plan.stops
                if s.opis_id is not None and s.opis_id in candidate_coords
            )
            if region is not None
        ]
        if stop_region_codes:
            return regions.dominant_region(stop_region_codes)

        if not winner.candidates:
            return None
        closest = min(winner.candidates, key=lambda c: c.distance_from_start_mi)
        coords = candidate_coords.get(closest.opis_id)
        if coords is None:
            return None
        return regions.region_for_state(coords["state"])

    def _region_trend(self, factor_table, price_index_status, region):
        """Trend chip data (D-07/D-08) for the route-dominant `region`.
        A trend value is a currency claim (D-10), so both values stay
        `None` unless the factor table is genuinely `"current"` AND
        that region has an observed week-over-week delta -- a stale or
        frozen response, or a region with no delta, hides the chip
        entirely rather than showing a stale/neutral number."""
        if price_index_status != "current" or region is None:
            return None, None
        delta_cents = factor_table["deltas_cents"].get(region)
        if delta_cents is None:
            return None, None
        return regions.REGION_LABELS.get(region), delta_cents

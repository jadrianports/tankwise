"""RouteView: the `POST /api/route` orchestrator.

A composition of already-tested seams -- validate, cache-aside, resolve
endpoints (coordinate pass-through or geocode + bounds re-check),
`get_routes` (one Mapbox Directions call returning up to three route
alternatives), a corridor pass and solve per alternative, deterministic
winner selection, a baseline pass on the winner, leg assembly,
build_map_url, serialize, cache.set.

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
context managers, attached to `self._timing` on a cache miss so DRF's
exception-handler context (`context["view"]`) can read partial timings
after a domain exception propagates. `_Stage.__exit__` records even on
exception and never suppresses it, so this instrumentation preserves the
pipeline's no-try/except shape. A stage entered more than once (e.g.
"corridor"/"solver" once per route alternative) accumulates into one
running total rather than overwriting.

`RouteView.throttle_classes` attaches rate limiting declaratively and
per-view (`routing.throttles.RouteBurstThrottle` +
`RouteSustainedThrottle`) rather than through a global DRF default, so
`HealthView`/`ReadyView` are never throttled.
"""
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import FileResponse, JsonResponse
from django.views import View
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.cache import build_cache_key
from routing.map_url import build_map_url
from routing.models import Station
from routing.pipeline.bbox import is_valid as bbox_is_valid
from routing.serializers import RouteRequestSerializer, RouteResponseSerializer
from routing.services import corridor, naive_baseline, solver
from routing.services.exceptions import InfeasibleRouteError
from routing.services.legs import build_legs
from routing.services.mapbox import Route, geocode, get_routes
from routing.throttles import RouteBurstThrottle, RouteSustainedThrottle
from routing.timing import ServerTiming

# Sentinel substituted for a `None` route duration in `_select_winner`'s
# tie-break key (D-10) -- keeps the four-level comparison total even if a
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
    succeeds even with an empty database and no MAPBOX_TOKEN set."""

    def get(self, request):
        return Response({"status": "ok"})


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


class SpaFallbackView(View):
    """`GET <any path not under api/ or static/>` -- serves the built SPA's
    `index.html` so client-side routes (e.g. `/trip/abc123`) render the app
    instead of a Django 404 (D-09).

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


class RouteView(APIView):
    """`POST /api/route` -- see module docstring."""

    throttle_classes = [RouteBurstThrottle, RouteSustainedThrottle]

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        cache_key = build_cache_key(validated)
        cache_timing = ServerTiming()
        with cache_timing.stage("cache"):
            cached = cache.get(cache_key)
        if cached is not None:
            response = Response(cached)
            response["Server-Timing"] = cache_timing.header_value()
            return response

        self._timing = ServerTiming()
        with self._timing.stage("total"):
            vehicle = validated["vehicle"]
            start_coords = self._resolve_endpoint(validated["start"])
            finish_coords = self._resolve_endpoint(validated["finish"])

            with self._timing.stage("route"):
                routes = get_routes(
                    (start_coords["latitude"], start_coords["longitude"]),
                    (finish_coords["latitude"], finish_coords["longitude"]),
                )

            results = self._solve_all_alternatives(routes, vehicle)
            winner = self._select_winner(results)

            stop_coords = self._stop_coords(winner.plan)
            ordered_stop_coords = [
                (stop_coords[s.opis_id]["latitude"], stop_coords[s.opis_id]["longitude"])
                for s in winner.plan.stops
                if s.opis_id is not None and s.opis_id in stop_coords
            ]

            map_url = build_map_url(
                winner.route,
                (start_coords["latitude"], start_coords["longitude"]),
                (finish_coords["latitude"], finish_coords["longitude"]),
                ordered_stop_coords,
            )

            legs = build_legs(winner.route, winner.plan)
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
                },
                context={
                    "start_coords": start_coords,
                    "finish_coords": finish_coords,
                    "stop_coords": stop_coords,
                },
            )
            payload = response_serializer.data

            cache.set(cache_key, payload, timeout=settings.CACHE_TTL_SECONDS)

        response = Response(payload)
        response["Server-Timing"] = self._timing.header_value()
        return response

    def _solve_all_alternatives(self, routes, vehicle):
        """Solve every route alternative Mapbox returned, catching only
        `InfeasibleRouteError` per alternative -- the project's one
        sanctioned deviation from the no-try/except pipeline shape,
        contained here rather than smeared through `post()`. Any other
        exception type propagates uncaught.

        Returns a list of `_AlternativeResult`, one per route, in Mapbox
        order. When every alternative is infeasible, re-raises the
        smallest-gap `InfeasibleRouteError` seen across all of them, so
        the request reports the closest miss rather than an arbitrary
        one.
        """
        results = []
        smallest_gap_exc = None
        for index, route in enumerate(routes):
            with self._timing.stage("corridor"):
                cands = corridor.candidates(route)
            try:
                with self._timing.stage("solver"):
                    plan = solver.solve(
                        cands,
                        route.total_route_mi,
                        tank_range_mi=vehicle["tank_range_mi"],
                        mpg=vehicle["mpg"],
                        starting_fuel=vehicle["starting_fuel"],
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
                continue
            results.append(
                _AlternativeResult(
                    index=index, route=route, plan=plan, feasible=True, candidates=cands
                )
            )

        if not any(r.feasible for r in results):
            raise smallest_gap_exc

        return results

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
        if not opis_ids:
            return {}
        stations = Station.objects.filter(opis_id__in=opis_ids)
        return {
            station.opis_id: {
                "latitude": station.latitude,
                "longitude": station.longitude,
            }
            for station in stations
        }

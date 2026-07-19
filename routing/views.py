"""RouteView: the `POST /api/route` orchestrator.

A thin composition of already-tested seams -- validate, cache-aside,
resolve endpoints (coordinate pass-through or geocode + bounds re-check),
get_route, corridor.candidates, solver.solve, build_map_url, serialize,
cache.set. The pipeline is never wrapped in try/except: domain exceptions
(`RouteNotFoundError`, `MapboxRequestError`, `InfeasibleRouteError`,
`InvalidRouteInputError`, `ImproperlyConfigured`) propagate uncaught to
`routing.exceptions.custom_exception_handler`, the sole translation layer
from those exceptions to HTTP.

Per-stage durations are collected via `routing.timing.ServerTiming`
context managers, attached to `self._timing` on a cache miss so DRF's
exception-handler context (`context["view"]`) can read partial timings
after a domain exception propagates. `_Stage.__exit__` records even on
exception and never suppresses it, so this instrumentation preserves the
pipeline's no-try/except shape.
"""
from django.conf import settings
from django.core.cache import cache
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.cache import build_cache_key
from routing.map_url import build_map_url
from routing.models import Station
from routing.pipeline.bbox import is_valid as bbox_is_valid
from routing.serializers import RouteRequestSerializer, RouteResponseSerializer
from routing.services import corridor, solver
from routing.services.mapbox import geocode, get_route
from routing.timing import ServerTiming


class HealthView(APIView):
    """`GET /api/health` -- dependency-free liveness probe for the Docker
    Compose healthcheck. Deliberately touches no DB, cache, or Mapbox so it
    succeeds even with an empty database and no MAPBOX_TOKEN set."""

    def get(self, request):
        return Response({"status": "ok"})


class RouteView(APIView):
    """`POST /api/route` -- see module docstring."""

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
            start_coords = self._resolve_endpoint(validated["start"])
            finish_coords = self._resolve_endpoint(validated["finish"])

            with self._timing.stage("route"):
                route = get_route(
                    (start_coords["latitude"], start_coords["longitude"]),
                    (finish_coords["latitude"], finish_coords["longitude"]),
                )
            with self._timing.stage("corridor"):
                cands = corridor.candidates(route)
            with self._timing.stage("solver"):
                plan = solver.solve(cands, route.total_route_mi)

            stop_coords = self._stop_coords(plan)
            ordered_stop_coords = [
                (stop_coords[s.opis_id]["latitude"], stop_coords[s.opis_id]["longitude"])
                for s in plan.stops
                if s.opis_id is not None and s.opis_id in stop_coords
            ]

            map_url = build_map_url(
                route,
                (start_coords["latitude"], start_coords["longitude"]),
                (finish_coords["latitude"], finish_coords["longitude"]),
                ordered_stop_coords,
            )

            response_serializer = RouteResponseSerializer(
                {"route": route, "plan": plan, "map_url": map_url},
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

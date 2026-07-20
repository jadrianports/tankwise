"""Rate limiting for `POST /api/route` -- the only Mapbox-calling,
expensive endpoint in this project.

Two `AnonRateThrottle` subclasses, each with its own `scope`, are listed
together on `RouteView.throttle_classes`. A single `ScopedRateThrottle`
cannot express this: a scope maps to exactly one rate string in
`DEFAULT_THROTTLE_RATES`, so a burst ceiling and a sustained ceiling
cannot coexist under one scope. DRF checks every entry in
`throttle_classes` in turn and rejects the request if *any* one of them
rejects it, so two independently-scoped classes give a stacked burst +
sustained pair for free without any extra logic here -- the rate
strings themselves live in `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`
(`config/settings/base.py`), not on these classes.

Throttling is attached per-view (`RouteView.throttle_classes`), never
via a global `DEFAULT_THROTTLE_CLASSES` default -- a global default
would also throttle `/api/health` and `/api/ready`, starving the
keep-warm pinger and breaking Render's own readiness gate.
"""
from rest_framework.throttling import AnonRateThrottle


class RouteBurstThrottle(AnonRateThrottle):
    scope = "route_burst"


class RouteSustainedThrottle(AnonRateThrottle):
    scope = "route_sustained"

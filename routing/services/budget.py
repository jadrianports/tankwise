"""Global outbound-call budget for the Mapbox seam.

The DRF throttles on `RouteView` are `AnonRateThrottle` subclasses, so
their 20/min and 200/day ceilings are counted PER CLIENT IP. That bounds
what any single caller can do, but it puts no ceiling on the total: N
distinct addresses each get their own 200/day allowance, so a rotating
proxy, a small botnet, or genuine viral traffic can still drive unbounded
spend against the Mapbox token. This module supplies the missing global
ceiling.

Counting lives in the shared cache (Redis in the containerized/deployed
setup) rather than in memory, because gunicorn runs multiple workers and a
per-process counter would let the real total reach `workers x cap`. Keys
are period-stamped from the wall clock (`mapbox:budget:{kind}:{period}`)
and expire on their own, so no reset job is needed and a window rolls over
by simply addressing a new key.

Two deliberate choices worth stating:

* **Fails OPEN.** If the cache is unreachable the request is allowed. A
  cache blip taking the whole demo offline is a worse outcome than a small
  number of uncounted calls, and the caps below sit far enough under the
  free tier to absorb that.
* **Reserved before the call, not after.** `consume()` increments before
  the HTTP request is issued, so a failing or timing-out upstream still
  costs budget. That is the point: an endpoint erroring in a retry loop is
  exactly the shape of abuse this guards, and it must not be free.

This bounds only calls the SERVER makes (Directions, Geocoding). Map loads
and terrain/raster tiles are fetched by the browser against the public
token and cannot be gated here -- those need Mapbox-side controls (usage
alerts, or URL restrictions on the `pk.` token, with the `map_url` caveat
in the README).
"""
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


class UpstreamBudgetExhaustedError(Exception):
    """The project-wide ceiling on outbound Mapbox calls for the current
    window is used up, so no new upstream call may be issued.

    Deliberately NOT a `SolverError`: nothing about the fuel-stop solve
    failed, and `routing/services/exceptions.py` is scoped to the solver's
    own hierarchy. It is also distinct from a DRF throttle rejection -- a
    throttle means THIS caller was too chatty and should back off, while
    this means the deployment as a whole has spent its allowance for the
    window regardless of who asked. The HTTP layer maps it to 503 rather
    than 429 for that reason. An already-cached route is unaffected: the
    view reads cache before any upstream call.

    Attributes:
        kind: which outbound product was capped ("directions"/"geocoding").
        window: the exhausted window ("day" or "month").
        cap: the configured ceiling for that window.
        used: the count observed at rejection time.
    """

    def __init__(self, *, kind, window, cap, used):
        self.kind = kind
        self.window = window
        self.cap = cap
        self.used = used
        super().__init__(
            f"Upstream {kind} budget exhausted for this {window}: "
            f"{used}/{cap} calls used"
        )


KEY_PREFIX = "mapbox:budget"

# Kinds are counted independently because they bill against separate
# Mapbox products (and separate free tiers).
DIRECTIONS = "directions"
GEOCODING = "geocoding"

# Generous headroom over the daily key so a rolled-over month cannot be
# blocked by a stale counter.
DAY_TTL_SECONDS = 60 * 60 * 48
MONTH_TTL_SECONDS = 60 * 60 * 24 * 40


def _periods(now):
    """(label, period_key, cap_setting, ttl) for each enforced window."""
    return (
        (
            "day",
            now.strftime("%Y-%m-%d"),
            int(getattr(settings, "MAPBOX_DAILY_CALL_CAP", 0)),
            DAY_TTL_SECONDS,
        ),
        (
            "month",
            now.strftime("%Y-%m"),
            int(getattr(settings, "MAPBOX_MONTHLY_CALL_CAP", 0)),
            MONTH_TTL_SECONDS,
        ),
    )


def _bump(key, ttl):
    """Increment `key`, creating it at 1 when absent. `cache.add` only
    writes when the key is missing and reports whether it did, so two
    workers racing the first call of a window resolve to 1 and 2 rather
    than both to 1."""
    if cache.add(key, 1, ttl):
        return 1
    return cache.incr(key)


def usage(kind):
    """Current (count, cap) per window for `kind` -- read-only, never
    increments. Returns `{}` when the cache is unreachable."""
    now = timezone.now()
    out = {}
    try:
        for label, period, cap, _ttl in _periods(now):
            out[label] = (cache.get(f"{KEY_PREFIX}:{kind}:{period}", 0), cap)
    except Exception:  # noqa: BLE001 - reporting only, never breaks a request
        return {}
    return out


def consume(kind):
    """Reserve one outbound call of `kind` against every enforced window.

    Raises `UpstreamBudgetExhaustedError` when a window is already at or
    over its cap. A cap of 0 (or a negative one) disables that window.
    Never raises anything else: any cache failure fails open.
    """
    if not getattr(settings, "MAPBOX_BUDGET_ENABLED", True):
        return

    now = timezone.now()
    try:
        for label, period, cap, ttl in _periods(now):
            if cap <= 0:
                continue
            key = f"{KEY_PREFIX}:{kind}:{period}"
            current = cache.get(key, 0) or 0
            if current >= cap:
                raise UpstreamBudgetExhaustedError(
                    kind=kind, window=label, cap=cap, used=current
                )
            _bump(key, ttl)
    except UpstreamBudgetExhaustedError:
        raise
    except Exception:  # noqa: BLE001 - see module docstring: fails open
        return

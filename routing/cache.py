"""Cache-key normalizer for the /api/route response cache.

Small, module-level pure helpers -- mirrors `routing/services/corridor.py`'s
`_as_decimal`/helper style. Coordinates are rounded to 5 decimal places
(~1 m) so trivial float differences still hit; addresses are casefolded
and whitespace-collapsed so an exact-repeat address (modulo case/spacing)
skips all outbound calls. Explicit `c:`/`a:`/`v:`/`e:` prefixes give each
component its own namespace so a coordinate token, an address token, the
vehicle token, and the EIA-vintage token can never collide (mitigates
cross-domain cache-key collisions).

The key was versioned `route:v3:` because every v3 response field
(`price_index_status`, `eia_week`, `trend_region`, `trend_delta_cents`,
plus the fact that `fuel_stops`/`total_cost` themselves may now be
EIA-indexed) changed the cached payload shape -- an entry keyed under
the previous prefix is not merely stale but structurally wrong for a v3
consumer. Bumping the prefix makes those entries unreachable rather than
mis-served (it also means a misconfigured deploy can never silently
return an old-shaped payload through the new code path). The `e:` token
additionally ties a cached payload to the EIA week it was priced under --
a week rollover produces a new key, so no plan priced under one EIA week
is ever served under a newer week's disclaimer (EIA-01).

Still `route:v6:` as of the additive `solver_strategy` response field
(Phase 18-04c) -- deliberately NOT bumped to `route:v7:`. Two reasons,
together: (1) `solver_strategy` is a pure, deterministic function of
exactly the inputs `route:v6:`'s own key already namespaces (the stop
chain, vehicle profile, EIA vintage, penalty -- see
`routing.services.solver.solve()`'s own docstring), so the SAME key
already only ever maps to the ONE strategy a fresh solve would choose;
no new key component is needed for the same reason `e:`/`p:` exist for
genuinely input-varying facts but `solver_strategy` is not one -- it is
an OUTPUT, not an input. (2) as of this change, `route:v6:` itself has
never been deployed (this repository's Phase 18 work is still local,
unpushed) -- there is no live Upstash entry anywhere carrying the
`route:v6:` prefix without a `solver_strategy` key for a stale payload
to collide with, so amending v6's payload shape in place carries none of
the "old-shaped payload silently served under new code" risk the v3/v6
bumps above exist to prevent. A future response-shape change LANDING
AFTER this prefix has shipped to production must still bump the prefix
per the same reasoning as every version above.

It was versioned `route:v6:` because two independent things changed
under the same deploy (D-33): (a) the objective changed -- plans are now
chosen under fuel dollars plus a per-stop penalty rather than fuel dollars
alone, so a payload cached under the previous prefix is a plan computed
under a superseded objective (INTG-03), and (b) `skipped_count`'s meaning
changed under D-04 from positionally-passed stations to
genuinely-rejected candidates, so the cached value is *wrong* for the new
consumer, not merely old -- the same argument that drove the previous
bump. The new `p:` penalty token (see `_penalty_token` below) additionally
ties a cached payload to the penalty it was priced under:
`CACHE_TTL_SECONDS` is 86400 and production Redis is Upstash, which
persists across deploys, so changing `FUEL_STOP_PENALTY_USD` and
redeploying would otherwise serve plans computed under the old penalty for
up to 24 hours after deploy -- INTG-03's exact failure mode, displaced one
commit past the phase boundary that introduced the setting. A documented
bump-on-change rule was considered and rejected here too, for the same
reason the `e:` token exists: it makes correctness depend on a human
remembering a note.

It was versioned `route:v5:` because the meaning of a cached
`savings` payload changed: the price-blind baseline no longer tops the
tank on its final stop, so a plan cached under `v4` reports a materially
larger saving (measured ~3.7x across 56 routes) than the same request
computed today. The value is numerically wrong for a v5 consumer rather
than merely old, and with `CACHE_TTL_SECONDS` at 24h a rollout without a
bump would keep serving the pre-fix figure for a full day after deploy --
including to anyone comparing the site against the fix. Same reasoning
that drove the v3 -> v4 bump, applied to a changed value rather than a
changed shape.

It was versioned `route:v4:` because the key shape itself changed:
`start`/`finish` are joined with any intermediate `waypoints[]` into one
ordered N-token chain (still each token from the unchanged
`_endpoint_token()`, joined `->`, never sorted or deduped) so a
multi-stop request's visit order is part of the key -- A->B->C and
A->C->B must never share a cache entry (Pitfall 13). A 2-endpoint
request produces the same token chain shape as before, just under the
new prefix.
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


def _eia_token(eia_vintage) -> str:
    """Namespaced `e:` token for the EIA-week vintage a cached payload
    was priced under. `eia_vintage` is the EIA week's raw ISO date
    string when priced under live/stale factors, or the literal status
    token (`"frozen"`) when priced under the permanent frozen-snapshot
    fallback -- so a frozen-mode plan and a current-mode plan for the
    same route/vehicle can never collide. Omitted (`None`, a legacy or
    test caller) resolves to a fixed, stable literal rather than a
    varying key shape."""
    return f"e:{eia_vintage if eia_vintage is not None else 'none'}"


def _penalty_token(penalty) -> str:
    """Namespaced `p:` token for the flat per-stop `penalty` (dollars) a
    cached payload was priced under. `CACHE_TTL_SECONDS` is 86400 and
    production Redis is Upstash, which persists across deploys, so
    changing `FUEL_STOP_PENALTY_USD` and redeploying would otherwise serve
    plans computed under the old penalty for up to 24 hours after deploy
    -- this token exists so that failure mode is structurally impossible
    rather than merely documented. Quantized to 2 decimal places,
    deterministically, exactly as the vehicle token quantizes its own
    fields. Omitted (`None`, a legacy or test caller that never threads a
    penalty through) resolves to a fixed, stable literal rather than a
    varying key shape -- the same treatment `_eia_token` gives its own
    `None`. A documented bump-on-change rule was considered and rejected
    here, because it makes correctness depend on a human remembering a
    note, which is exactly what this token (and the `e:` token before it)
    exists to stop relying on."""
    if penalty is None:
        return "p:none"
    penalty_value = penalty if isinstance(penalty, Decimal) else Decimal(str(penalty))
    return f"p:{round(penalty_value, 2)}"


def build_cache_key(validated_data, *, eia_vintage=None, penalty=None) -> str:
    """Build the cache key for a validated
    `{"start": ..., "finish": ..., "vehicle": ..., "waypoints": ...}`
    payload (the `RouteRequestSerializer.validated_data` shape).

    Each of `start`/`finish`/`waypoints[*]` is
    `{"kind": "coordinate", "lat", "lng"}` or
    `{"kind": "address", "value"}`. `waypoints` is optional here --
    a caller that omits it (existing tests, a pre-waypoints-shaped
    request) is treated as `[]`, so the ordered chain collapses to
    the original two-endpoint shape. `vehicle` is optional here -- see
    `_vehicle_token` -- so callers that omit it (existing tests, a
    v1.0-shaped request that resolved to defaults) still get a stable
    key. `eia_vintage` (see `_eia_token`) and `penalty` (see
    `_penalty_token`) are likewise optional, so a caller that never
    threads either through still gets a stable key.

    The ordered chain is `start -> *waypoints -> finish`, each stop's
    token built by the unchanged `_endpoint_token()` -- never sorted,
    never deduped, so visit order is part of the key (A->B->C != A->C->B,
    Pitfall 13). Composed as a simple string, not a hash -- no need to
    hand-roll one at this scale."""
    waypoints = validated_data.get("waypoints") or []
    stops = [validated_data["start"], *waypoints, validated_data["finish"]]
    stops_token = "->".join(_endpoint_token(stop) for stop in stops)
    vehicle_token = _vehicle_token(validated_data.get("vehicle"))
    eia_token = _eia_token(eia_vintage)
    penalty_token = _penalty_token(penalty)
    return f"route:v6:{stops_token}|{vehicle_token}|{eia_token}|{penalty_token}"

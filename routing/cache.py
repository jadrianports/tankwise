"""Cache-key normalizer for the /api/route response cache.

Small, module-level pure helpers -- mirrors `routing/services/corridor.py`'s
`_as_decimal`/helper style. Coordinates are rounded to 5 decimal places
(~1 m) so trivial float differences still hit; addresses are casefolded
and whitespace-collapsed so an exact-repeat address (modulo case/spacing)
skips all outbound calls. Explicit `c:`/`a:`/`v:`/`e:` prefixes give each
component its own namespace so a coordinate token, an address token, the
vehicle token, and the EIA-vintage token can never collide (mitigates
cross-domain cache-key collisions).

It was versioned `route:v8:` (plan 18.1-05) because the dispatch policy
itself grew a second layer (D-01): a wall-clock `DP_DEADLINE_SECONDS` now
backstops the pre-flight `DP_TRANSITION_BUDGET` estimate, catching
whatever the estimate alone cannot -- an entry cached under `v7` records
only half the policy that produced it, the estimate-vs-budget half, with
no record of the deadline that decided whether an ADMITTED cell's exact
DP was actually allowed to finish. `_dispatch_policy_token` is extended
to derive from BOTH constants together (see its own docstring), so this
bump and that extension land in the SAME change, per this project's own
standing same-change rule (INTG-03, the `v6` -> `v7` precedent directly
above). A plan produced by a deadline breach is cached normally, not
specially marked or excluded: a breach is a property of the CELL (how
long its exact DP genuinely takes on this hardware), not of the moment
the request happened to arrive, so caching it means the slowest routes
pay the full deadline once and are fast thereafter -- not caching it
would leave the cells with the worst worst-case as the only ones that
never get any cache relief at all. Because the token DERIVES from the
constants rather than encoding this plan's own finding, a later
re-derivation of either `DP_TRANSITION_BUDGET` or `DP_DEADLINE_SECONDS`
(plan 18.1-08) changes every cache key automatically, with no matching
edit needed here -- the entire point of deriving rather than documenting
a bump-on-change convention, exactly as the `v7` paragraph below already
argues for the budget alone.

It was versioned `route:v7:` because the dispatch-policy token described
below (`_dispatch_policy_token`) is new: an entry cached under `v6` carries
no record of which predictor/`DP_TRANSITION_BUDGET` combination decided
`solver_strategy`, so a future dispatch-policy change could in principle
still serve a `v6`-keyed entry computed under the SUPERSEDED policy even
though `route:v6:`'s own key shape never changed for one --
`18-VERIFICATION.md`'s own finding (`route:v6:` keys on the penalty but NOT
on the dispatch policy). The paragraph below claiming the cache "needs no
separate strategy token as a result" (`routing.services.solver.solve()`'s
own docstring made the identical claim, now corrected there too) was true
only WITHIN one build, where dispatch is a pure function of exactly the
inputs the key already namespaces -- and false ACROSS builds whose policy
constants differ, which is exactly the unguarded coupling this bump
closes. This lands in the SAME change as the dispatch-policy gap-closure
finding it protects (plan 18-12), per this project's own standing rule
that a cache-key change cannot be staged separately from the behaviour
change it guards (D-33's INTG-03 precedent) -- even though this
gap-closure's own finding was NOT to change the policy's value (see
`routing.services.dp`'s "Why the transition-count estimate was not
replaced" module docstring section): the token guards against a FUTURE
change, not only this one.

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

Still `route:v6:` as of Phase 18-04d, which swapped the DP's fallback
target from the fixed-charge-blind pre-Phase-18 greedy
(`routing.services.greedy.solve_greedy`) to a penalty-aware heuristic
(`routing.services.heuristic.solve_penalty_aware_heuristic`) and
renamed the wire value that names which one ran from `"greedy_fallback"`
to `"penalty_aware_heuristic"` -- also deliberately NOT bumped, for the
identical reasons the paragraph below gives for the field's original
addition: which branch fires, and what it computes, are both still pure
deterministic functions of exactly the inputs `route:v6:`'s key already
namespaces, and `route:v6:` remains undeployed (still local, unpushed),
so there is still no live entry anywhere a stale, differently-computed
fallback plan could collide with. The same "a future change landing
AFTER this prefix has shipped must still bump it" rule applies here too.

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


def _dispatch_policy_token() -> str:
    """Namespaced `d:` token recording which dispatch policy decided
    `solver_strategy` for a cached plan. DERIVED from the policy constants
    themselves (`dp.DP_TRANSITION_BUDGET`'s value, `dp.DP_DEADLINE_SECONDS`'s
    value, the predictor's own name) rather than a documented
    bump-on-change convention -- the same argument `_penalty_token`'s own
    docstring already gives for rejecting that convention ("makes
    correctness depend on a human remembering a note"), applied one layer
    down: `18-VERIFICATION.md` found that `route:v6:` keys on the penalty
    but NOT on the dispatch policy, so an entry cached before a future
    `DP_TRANSITION_BUDGET` or `DP_DEADLINE_SECONDS` change would still be
    served under the OLD dispatch policy without this token.

    Imported locally, not at module scope, for the same reason
    `_vehicle_token` defers its own `routing.serializers` import: keeps
    this module's import order irrelevant relative to
    `routing.services.dp`. Safe to import here: `dp.py` is one of
    `SOLVER_FILES` (`routing/tests/test_boundaries.py`) and is itself free
    of Django/ORM/HTTP imports (`SolverPurityTest`), so importing FROM it
    into this (non-`SOLVER_FILES`) module drags nothing Django-shaped into
    the solver's own purity boundary -- the import direction runs strictly
    cache.py -> dp.py, never the reverse.

    Unlike `_eia_token`/`_penalty_token`, this token has no caller-supplied
    "omitted" input to resolve: a dispatch policy is a build-time module
    constant, always known, never a per-request fact a caller might not
    have threaded through yet -- so there is no "none" case to produce a
    fixed literal for. What stays constant across every call, matching
    those tokens' own spirit, is that the SAME policy always yields the
    SAME token regardless of what the caller passes (see
    `test_cache.py::DispatchPolicyCacheKeyTests`).

    As of this token's introduction (plan 18-12), the predictor is still
    `estimate_transition_count` (18-10's closed predictor family found no
    qualifying replacement) and `DP_TRANSITION_BUDGET` is still 50,000
    (the same gap-closure found no deployed-hardware-justified value to
    move it to) -- see `routing.services.dp`'s own module docstring, "Why
    the transition-count estimate was not replaced", for the full finding.
    The token still exists and still changes automatically the moment
    either fact stops being true, which is the entire point: this plan's
    own finding was NOT to change the policy, but the token does not
    encode that finding -- it encodes the CONSTANTS, so a later change
    needs no matching update here.

    As of plan 18.1-05, the dispatch policy grew a second layer (D-01):
    a wall-clock `DP_DEADLINE_SECONDS` now backstops the pre-flight
    `DP_TRANSITION_BUDGET` estimate, catching what the estimate alone
    cannot. `DP_DEADLINE_SECONDS` is folded into this token for the
    identical reason `DP_TRANSITION_BUDGET` is: a plan produced under one
    deadline value must never be served, unchanged, to a request the
    CURRENT build's deadline would have handled differently. Deriving the
    token from BOTH constants (rather than adding a second, separate
    token) means plan 18.1-08's later re-derivation of either one changes
    every cache key automatically, with no matching edit required here --
    exactly the same "encodes the CONSTANTS, not the finding" property
    this token already had for `DP_TRANSITION_BUDGET` alone."""
    from routing.services.dp import DP_DEADLINE_SECONDS, DP_TRANSITION_BUDGET

    predictor_name = "estimate_transition_count"
    return f"d:{predictor_name}:{DP_TRANSITION_BUDGET}:{DP_DEADLINE_SECONDS}"


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
    hand-roll one at this scale.

    The key now also carries a dispatch-policy token (`_dispatch_policy_token`,
    the `d:` segment between `eia_token` and `penalty_token`) DERIVED from
    `routing.services.dp`'s own dispatch-policy constants (as of plan
    18.1-05, both `DP_TRANSITION_BUDGET` and `DP_DEADLINE_SECONDS`), so a
    cached plan can never outlive the dispatch policy that produced it
    (plan 18-12, closing the coupling `18-VERIFICATION.md` found
    unguarded; plan 18.1-05, extending it to the deadline)."""
    waypoints = validated_data.get("waypoints") or []
    stops = [validated_data["start"], *waypoints, validated_data["finish"]]
    stops_token = "->".join(_endpoint_token(stop) for stop in stops)
    vehicle_token = _vehicle_token(validated_data.get("vehicle"))
    eia_token = _eia_token(eia_vintage)
    dispatch_token = _dispatch_policy_token()
    penalty_token = _penalty_token(penalty)
    return (
        f"route:v8:{stops_token}|{vehicle_token}|{eia_token}|"
        f"{dispatch_token}|{penalty_token}"
    )

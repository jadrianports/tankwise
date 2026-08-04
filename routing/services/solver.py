"""Pure fuel-stop solver: cheapest feasible fueling plan.

Request-path math only -- no Django, no DB, no HTTP client. All
money and gallon values are exact, unrounded `Decimal`; rounding to cents
happens only at the HTTP response serialization boundary.
"""
from dataclasses import dataclass
from decimal import Decimal

from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError


class PurchaseReason:
    """String-enum constants for `FuelStop.purchase_reason`.

    Each value names the exact edge that was chosen -- recorded at the
    moment that decision was made, never re-derived afterward by
    inspecting the finished plan. Wire values are the lowercase strings.

    `BYPASS_CHEAPER_NOT_WORTH_STOP` names the case the greedy structurally
    could not produce: a stop taken here while a strictly cheaper station
    was reachable, because routing through that station would have cost
    more in stop penalty than it saved in fuel. The greedy never emits
    this value.
    """

    REACH_CHEAPER_STOP = "reach_cheaper_stop"
    FILL_TO_CONTINUE = "fill_to_continue"
    REACH_FINISH = "reach_finish"
    TOP_UP_AT_CHEAPEST = "top_up_at_cheapest"
    BYPASS_CHEAPER_NOT_WORTH_STOP = "bypass_cheaper_not_worth_stop"


class SolverStrategy:
    """String-enum constants for `FuelPlan.strategy` -- which algorithm
    actually produced a given plan. `solve()` sets this on every plan it
    returns (see its own docstring's dispatch description); it is `None`
    only for a `FuelPlan` built by something other than `solve()` (a raw
    `dp.solve_fixed_charge`/`heuristic.solve_penalty_aware_heuristic`
    call, or a hand-built test double), which never happens on the
    production request path.

    `PENALTY_AWARE_HEURISTIC` (Phase 18-04d) replaced the prior
    `GREEDY_FALLBACK` dispatch target -- the wire value changed from
    `"greedy_fallback"` to `"penalty_aware_heuristic"` alongside it, since
    the fallback algorithm itself changed from the fixed-charge-blind
    pre-Phase-18 greedy (`greedy.solve_greedy`, still used only by its own
    differential test) to a heuristic that approximates the same
    fixed-charge objective the DP minimises exactly (see
    `routing.services.heuristic`'s module docstring). `"greedy_fallback"`
    is never emitted by a live `solve()` call as of this change.
    """

    EXACT_DP = "exact_dp"
    PENALTY_AWARE_HEURISTIC = "penalty_aware_heuristic"


@dataclass(frozen=True)
class Candidate:
    """A candidate fuel stop positioned along the route."""

    name: str
    opis_id: int
    price_per_gallon: Decimal
    distance_from_start_mi: Decimal


@dataclass(frozen=True)
class FuelStop:
    """A purchase recorded at a real, along-route station.

    The rationale fields (`purchase_reason` onward) are additive and
    default to `None`/`0` so callers constructing a `FuelStop` with only
    the original six fields keep working unchanged.

    `bypassed_cheaper_count` and `bypassed_saving_forgone` are also
    additive and default to `0`/`None`. `bypassed_cheaper_count` is how
    many strictly-cheaper reachable stations the recurrence evaluated as
    successors from this stop and did not take; `bypassed_saving_forgone`
    is the fuel-dollar saving those rejections gave up. They carry the
    quantitative half of `BYPASS_CHEAPER_NOT_WORTH_STOP`'s story, while
    the backend emits no prose.
    """

    name: str
    opis_id: int
    price_per_gallon: Decimal
    distance_from_start_mi: Decimal
    gallons: Decimal
    cost: Decimal
    purchase_reason: str | None = None
    reason_target_opis_id: int | None = None
    reason_target_name: str | None = None
    skipped_count: int = 0
    skipped_avg_price: Decimal | None = None
    price_percentile: Decimal | None = None
    corridor_avg_price: Decimal | None = None
    bypassed_cheaper_count: int = 0
    bypassed_saving_forgone: Decimal | None = None


@dataclass(frozen=True)
class FuelPlan:
    """The cheapest feasible fueling plan for a route.

    `penalised_objective` and `penalty_applied` are additive and default
    to `None`. `total_cost` remains fuel dollars only (INTG-02) and never
    includes the penalty; `penalised_objective` is
    `total_cost + penalty_applied * len(stops)`, the quantity the DP
    actually minimises. Both are internal-only and deliberately not
    serialized this phase -- exposing them is a product decision that
    belongs to Phase 19.

    `strategy` is additive, defaults to `None`, and IS serialized (unlike
    the two fields above): which of `SolverStrategy.EXACT_DP` /
    `SolverStrategy.PENALTY_AWARE_HEURISTIC` actually produced this plan --
    `solve()` sets it on every plan it returns (see its own docstring),
    the API response's honesty requirement for which algorithm ran.

    `deadline_breached` and `deadline_breach_elapsed_s` are additive and
    default to `False`/`None`, exactly like `penalised_objective` and
    `penalty_applied` above -- internal-only and deliberately NOT
    serialized. `RouteResponseSerializer` declares its fields explicitly,
    so an undeclared dataclass field cannot leak onto the wire.

    Why the breach is not on the wire at all (D-08's substance, not just
    its verdict): the demoted-versus-breached distinction is already
    DERIVABLE from the pair of the estimate and the strategy, because
    dispatch has exactly two deterministic inputs -- an estimate above
    budget with the heuristic strategy means demoted outright and never
    attempted, an estimate at or below budget with the heuristic strategy
    means attempted and breached -- and the estimate is recomputable
    offline from the same committed dataset and route. A response field
    would store what can already be derived, which is the weakest
    possible justification for permanent public API surface under an
    additive-only contract.

    The named, accepted cost: a cached breach plan carries no marker in
    its payload, because Server-Timing is per-request and there is no
    solve on a cache hit. This is acceptable precisely because the
    (estimate, strategy) pair reconstructs it.

    `deadline_breach_elapsed_s` is a plain `float`, unlike every money,
    gallon and position value elsewhere in this dataclass, which is exact
    `Decimal` -- it is diagnostic only (`time.monotonic()` differences,
    read off `dp.DeadlineExceededError.elapsed_seconds`), and never enters
    a monetary, gallon or position computation.
    """

    stops: list[FuelStop]
    total_cost: Decimal
    total_gallons: Decimal
    penalised_objective: Decimal | None = None
    penalty_applied: Decimal | None = None
    strategy: str | None = None
    deadline_breached: bool = False
    deadline_breach_elapsed_s: float | None = None


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validate(candidates, total_route_mi, tank_range_mi, mpg, starting_fuel):
    if total_route_mi <= 0:
        raise InvalidRouteInputError(
            f"total_route_mi must be positive, got {total_route_mi}"
        )
    if tank_range_mi <= 0:
        raise InvalidRouteInputError(
            f"tank_range_mi must be positive, got {tank_range_mi}"
        )
    if mpg <= 0:
        raise InvalidRouteInputError(f"mpg must be positive, got {mpg}")
    if starting_fuel < 0 or starting_fuel > 1:
        raise InvalidRouteInputError(
            f"starting_fuel must be within [0, 1], got {starting_fuel}"
        )
    for c in candidates:
        if c.price_per_gallon < 0:
            raise InvalidRouteInputError(
                f"price_per_gallon must not be negative, got {c.price_per_gallon} "
                f"for candidate {c.name!r}"
            )
        if c.distance_from_start_mi < 0 or c.distance_from_start_mi > total_route_mi:
            raise InvalidRouteInputError(
                f"distance_from_start_mi ({c.distance_from_start_mi}) for candidate "
                f"{c.name!r} must be within [0, total_route_mi={total_route_mi}]"
            )


# Imported here -- after `Candidate`/`FuelStop`/`FuelPlan`/`PurchaseReason`
# are already defined above -- rather than at module top, because
# `routing.services.dp`, `routing.services.heuristic`, and
# `routing.services.prune` all import those names back from this module.
# Placing the import here means Python has already populated this
# module's namespace with those names by the time any sibling module's
# own `from routing.services.solver import ...` line runs, so the
# two-way reference resolves without a circular-import error.
#
# `routing.services.greedy` is deliberately NOT imported here as of Phase
# 18-04d -- it is no longer a dispatch target (see
# `SolverStrategy.PENALTY_AWARE_HEURISTIC`'s own docstring), and remains
# reachable only from its own differential test
# (`routing/tests/test_greedy.py`) and `routing/tests/frozen_greedy.py`.
from routing.services import dp, heuristic  # noqa: E402
from routing.services.prune import prune_dominated_candidates  # noqa: E402


def _rejected_context(candidates, tank_range_mi, stop_position, stop_price, boundary_position):
    """The genuinely-rejected candidates for one stop (D-04): full-list
    candidates strictly ahead of `stop_position`, strictly cheaper than
    `stop_price`, reachable within one tank of `stop_position`, and
    strictly before `boundary_position` (the next stop's position, or
    `total_route_mi` for the last stop) -- the strictly-cheaper stations
    this purchase's own fuel carried the vehicle past without stopping.
    Computed over the FULL candidate list (D-20), never the pruned search
    set, so a stop's rejected count/average never depends on whether the
    prune happened to keep or drop the rejected station.
    """
    rejected = [
        c
        for c in candidates
        if stop_position < c.distance_from_start_mi < boundary_position
        and c.distance_from_start_mi - stop_position <= tank_range_mi
        and c.price_per_gallon < stop_price
    ]
    rejected_count = len(rejected)
    rejected_avg_price = (
        sum((c.price_per_gallon for c in rejected), Decimal(0)) / rejected_count
        if rejected_count
        else None
    )
    return rejected_count, rejected_avg_price


def solve(
    candidates,
    total_route_mi,
    *,
    tank_range_mi=Decimal(500),
    mpg=Decimal(10),
    starting_fuel=Decimal(1),
    penalty=Decimal(0),
    prune=True,
    deadline=dp.DP_DEADLINE_SECONDS,
) -> FuelPlan:
    """Return the cheapest feasible fueling plan for a route of
    ``total_route_mi`` miles, given an iterable of ``Candidate`` stations.

    ``starting_fuel`` is a 0.0-1.0 fraction of ``tank_range_mi`` already in
    the tank at the origin (default ``1`` -- a full tank). START is a
    non-purchasable node: it can never be billed for fuel, so the
    reachable set at START is bounded by the fuel actually on board
    (``starting_fuel * tank_range_mi``), not by the tank's full capacity.
    At every real station the tank can be topped off, so the bound there
    stays ``tank_range_mi``.

    This function is now an orchestration seam (D-08) over
    ``routing.services.dp.solve_fixed_charge`` -- it no longer runs a
    search itself. Its body runs, in order: (1) validate; (2)
    ``dp.preflight_gap_check`` over the UNPRUNED ``candidates`` -- SOLV-05,
    D-17, and Phase 17's D-12: a pruned list can hide the very station
    whose absence makes a gap detectable, so this check always runs first,
    on the complete input; (3) prune the search set with
    ``prune_dominated_candidates`` -- only the set the *search* explores is
    ever pruned; (4) compute ``dp.estimate_transition_count`` over that
    same search set and compare it against ``dp.DP_TRANSITION_BUDGET`` --
    at or under budget, attempt ``dp.solve_fixed_charge`` under a
    wall-clock ``deadline`` (``strategy=SolverStrategy.EXACT_DP``); over
    budget, delegate instead to ``heuristic.solve_penalty_aware_heuristic``
    over the FULL, unpruned ``candidates``
    (``strategy=SolverStrategy.PENALTY_AWARE_HEURISTIC``). This is a
    two-layer policy (D-01): the budget comparison decides whether the
    exact DP is attempted at all, and the deadline decides how long an
    attempted cell may run before it is abandoned. When the attempted DP
    raises ``dp.DeadlineExceededError`` before reaching FINISH, that
    exception is caught here -- never leaked to this function's caller --
    and this branch falls back to the IDENTICAL
    ``heuristic.solve_penalty_aware_heuristic`` call the over-budget branch
    already makes, over the full unpruned ``candidates``, with
    ``strategy`` demoted to ``SolverStrategy.PENALTY_AWARE_HEURISTIC``
    (which is true: the heuristic did produce the returned plan) and the
    breach recorded on the two internal-only ``FuelPlan`` fields
    ``deadline_breached``/``deadline_breach_elapsed_s`` (see ``FuelPlan``'s
    own docstring for why those never reach the wire). See
    ``routing.services.dp``'s "Pre-flight transition-count estimate
    and the penalty-aware fallback" module docstring section for why this
    dispatch exists and how its threshold was calibrated, and
    ``routing.services.heuristic``'s own module docstring for what the
    fallback algorithm itself does and does not guarantee. The predictor
    (``dp.estimate_transition_count``) and the budget
    (``dp.DP_TRANSITION_BUDGET``, 50,000) are both unchanged as of plan
    18-12's gap-closure: no replacement predictor qualified (18-10) and no
    deployed-hardware figure justified moving the budget (18-11) -- see
    ``routing.services.dp``'s "Why the transition-count estimate was not
    replaced" module docstring section for the full, dated finding; (5) rebuild every
    returned stop's reporting statistics (``corridor_avg_price``,
    ``price_percentile``, ``skipped_count``, ``skipped_avg_price``) over
    the FULL, unpruned ``candidates`` argument (D-20, inherited from Phase
    17 and load-bearing) -- the winning branch's own ``purchase_reason``,
    ``reason_target_opis_id``, ``reason_target_name``,
    ``bypassed_cheaper_count`` and ``bypassed_saving_forgone`` are carried
    through untouched regardless of which branch produced them.

    The dispatch decision is a pure function of ``search_set``,
    ``total_route_mi``, ``tank_range_mi`` and ``starting_fuel`` -- AND of
    the current dispatch-policy constants (``dp.estimate_transition_count``'s
    identity and ``dp.DP_TRANSITION_BUDGET``'s value): the same request
    under the same policy always chooses the same strategy and produces
    the same plan. What this docstring previously claimed here (through
    plan 18-04c/18-04d) was that the response cache "needs no separate
    strategy token as a result" because dispatch is a pure function of
    exactly the inputs ``routing/cache.py``'s ``build_cache_key`` already
    keys on -- true WITHIN one build, where the policy constants never
    change mid-request, but FALSE ACROSS builds whose policy constants
    differ: a plan cached under an old budget can outlive a policy change
    and be served to a request the CURRENT build would dispatch
    differently (the unguarded coupling ``18-VERIFICATION.md`` found).
    ``build_cache_key`` now also carries a dispatch-policy token
    (``_dispatch_policy_token``) DERIVED from those same constants, so a
    future change to either one changes every cache key automatically
    (plan 18-12).

    ``penalty`` is a plain ``Decimal`` this function never resolves itself
    -- it defaults to ``Decimal(0)``, at which the DP is plan-identical to
    this function's pre-Phase-18 greedy behaviour (proven in plan 18-03).
    Resolving ``penalty`` from a live setting is the caller's job
    (``routing/views.py``, AST-gated by ``SolvePenaltyKwargGateTest``) --
    this module stays pure.

    ``prune`` is Phase 17 D-21's rollback hatch: set it to ``False`` to run
    the DP over the complete, unpruned candidate set (sorted by the same
    total order the prune would have used) without a revert.

    ``deadline`` is D-05's wall-clock hatch, keyword-only and mirroring
    ``prune``'s own shape exactly: it defaults to
    ``dp.DP_DEADLINE_SECONDS``, production-on rather than opt-in, so an
    attempted exact-DP solve always runs under a wall-clock cap unless a
    caller explicitly passes ``deadline=None``. RESEARCH.md flagged the
    opposite default (``None``, which would shrink the call-site audit to
    ``routing/views.py`` alone) as an option; this default was chosen
    instead for two reasons: it is exactly the shape ``prune=True``'s own
    rollback hatch already established -- enabled by default, opted out of
    explicitly and visibly -- and D-02 keeps the deadline out of the
    request layer entirely, so ``views.py`` is never the place the
    production value gets supplied; it has to live here, as this
    function's own default, or nowhere. The cost of this choice, named
    plainly: every existing caller of ``solve()`` -- test or management
    command -- inherits the production cap unless it explicitly opts out
    with ``deadline=None``, which is exactly what Task 2's call-site audit
    exists to do safely, call site by call site, rather than leaving any
    of them to inherit it by accident.

    Infeasibility has exactly one source in this codebase --
    ``dp.preflight_gap_check`` above -- so ``dp.solve_fixed_charge`` itself
    is total and never raises (D-17); it is only ever called once this
    check has already passed.

    Raises ``InvalidRouteInputError`` on malformed input (including a
    ``starting_fuel`` outside ``[0, 1]``) and ``InfeasibleRouteError`` when
    no feasible plan exists (a gap between two along-route nodes exceeds
    the usable range at that node).
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    mpg = _as_decimal(mpg)
    starting_fuel = _as_decimal(starting_fuel)
    penalty = _as_decimal(penalty)

    candidates = list(candidates)

    _validate(candidates, total_route_mi, tank_range_mi, mpg, starting_fuel)

    dp.preflight_gap_check(
        candidates,
        total_route_mi=total_route_mi,
        tank_range_mi=tank_range_mi,
        starting_fuel=starting_fuel,
    )

    total_order_key = lambda c: (  # noqa: E731
        c.distance_from_start_mi,
        c.price_per_gallon,
        c.opis_id,
    )
    search_set = (
        prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )
        if prune
        else sorted(candidates, key=total_order_key)
    )

    estimate = dp.estimate_transition_count(
        search_set,
        total_route_mi=total_route_mi,
        tank_range_mi=tank_range_mi,
        starting_fuel=starting_fuel,
    )
    deadline_breached = False
    deadline_breach_elapsed_s = None
    if estimate <= dp.DP_TRANSITION_BUDGET:
        strategy = SolverStrategy.EXACT_DP
        try:
            raw_plan = dp.solve_fixed_charge(
                search_set,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=penalty,
                deadline=deadline,
            )
        except dp.DeadlineExceededError as exc:
            # D-04 BREACH half: the exact DP ran out of wall-clock time
            # before reaching FINISH. Caught here specifically -- never a
            # bare `except Exception`, which would also swallow a genuine
            # bug -- and never leaked past this function's own boundary.
            # Falls back to the IDENTICAL heuristic call the over-budget
            # branch above makes, over the FULL unpruned `candidates` (not
            # `search_set`), and demotes `strategy` to the same value the
            # over-budget branch already reports -- true, because the
            # heuristic did produce the plan being returned.
            strategy = SolverStrategy.PENALTY_AWARE_HEURISTIC
            raw_plan = heuristic.solve_penalty_aware_heuristic(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=penalty,
            )
            deadline_breached = True
            deadline_breach_elapsed_s = exc.elapsed_seconds
    else:
        strategy = SolverStrategy.PENALTY_AWARE_HEURISTIC
        raw_plan = heuristic.solve_penalty_aware_heuristic(
            candidates,
            total_route_mi,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
            penalty=penalty,
        )

    total_candidates = len(candidates)
    corridor_avg_price = (
        sum((c.price_per_gallon for c in candidates), Decimal(0)) / total_candidates
        if total_candidates
        else None
    )

    def _price_percentile(price):
        if not total_candidates:
            return None
        cheaper_count = sum(1 for c in candidates if c.price_per_gallon < price)
        return Decimal(cheaper_count) / Decimal(total_candidates)

    stops = []
    for index, raw_stop in enumerate(raw_plan.stops):
        boundary_position = (
            raw_plan.stops[index + 1].distance_from_start_mi
            if index + 1 < len(raw_plan.stops)
            else total_route_mi
        )
        skipped_count, skipped_avg_price = _rejected_context(
            candidates,
            tank_range_mi,
            raw_stop.distance_from_start_mi,
            raw_stop.price_per_gallon,
            boundary_position,
        )
        stops.append(
            FuelStop(
                name=raw_stop.name,
                opis_id=raw_stop.opis_id,
                price_per_gallon=raw_stop.price_per_gallon,
                distance_from_start_mi=raw_stop.distance_from_start_mi,
                gallons=raw_stop.gallons,
                cost=raw_stop.cost,
                purchase_reason=raw_stop.purchase_reason,
                reason_target_opis_id=raw_stop.reason_target_opis_id,
                reason_target_name=raw_stop.reason_target_name,
                skipped_count=skipped_count,
                skipped_avg_price=skipped_avg_price,
                price_percentile=_price_percentile(raw_stop.price_per_gallon),
                corridor_avg_price=corridor_avg_price,
                bypassed_cheaper_count=raw_stop.bypassed_cheaper_count,
                bypassed_saving_forgone=raw_stop.bypassed_saving_forgone,
            )
        )

    return FuelPlan(
        stops=stops,
        total_cost=raw_plan.total_cost,
        total_gallons=raw_plan.total_gallons,
        penalised_objective=raw_plan.penalised_objective,
        penalty_applied=raw_plan.penalty_applied,
        strategy=strategy,
        deadline_breached=deadline_breached,
        deadline_breach_elapsed_s=deadline_breach_elapsed_s,
    )

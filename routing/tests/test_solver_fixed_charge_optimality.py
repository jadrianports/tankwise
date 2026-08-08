"""Independent brute-force oracle for the fuel + penalty*stops objective.

The optimum this module computes is: the plan that minimizes fuel dollars
plus penalty times the count of stations where gallons purchased is greater
than zero, for a given route.

The optimum is found by enumerating every subset of candidate stations and
costing each one fully, in a plain itertools.combinations loop -- there is
no memo table, no state-keyed dictionary, and no (node, fuel) recurrence
anywhere in this module. A reviewer checking this module's independence
from the DP it will later judge can do so by reading that loop.

The only symbol this module imports from the routing.services package is
Candidate, plus InfeasibleRouteError from routing.services.exceptions,
needed only to turn an infeasible route into a boolean feasibility verdict.
Every other solver helper -- the plan and stop dataclasses, the
purchase-reason constants, and the solver's own private helpers -- is
deliberately not imported here.

D-10 retarget: every "shipped greedy" comparison in this module (the
penalty=0 anchor property, the flattened multi-leg anchor, and
measure_disagreement's greedy arm) calls routing.tests.frozen_greedy.solve
rather than routing.services.solver.solve. This is a call-target
substitution only -- at the point in Phase 18 this module was retargeted,
solve()'s body was still byte-unchanged, so no assertion moved. It matters
once Phase 18's later plan rewrites solve() to delegate to the DP: without
this retarget, ROADMAP criterion 1's "agrees with the shipped greedy on
every case at penalty=0 across the full 200-case Hypothesis run" would
silently become "agrees with itself". "Shipped greedy" now names the
pre-Phase-18 greedy, preserved verbatim under routing/tests/frozen_greedy.py,
whose provenance git-show SHA is recorded in that module's own docstring.

D-10 also adds a DP arm, DpOracleDifferentialTests below, comparing
routing.services.dp.solve_fixed_charge against this module's own oracle
across the full PENALTY_LADDER sweep -- imported from routing.services.dp,
never copied or re-derived here.

Tests use django.test.SimpleTestCase together with Hypothesis's @given,
never hypothesis.extra.django.TestCase: the solver under comparison is pure
and never touches the ORM, so the Django test-case integration would buy a
per-example database transaction for nothing.

This oracle is deliberately exponential and can never ship in production:
on the real ~508-candidate Dallas-Seattle corridor, subset enumeration is
2**508. It terminates in this test suite only because MAX_STATIONS bounds
how many candidate stations a single example may generate.

Measured disagreement evidence (D-13/D-17, ROADMAP criterion 2): at the
sourced $35 penalty, `manage.py measure_penalty_disagreement`'s default
200-route corpus (CORPUS_PARAMS, seed=20260730, mpg=6.5, tank_range_mi=1050,
routes drawn from [1400, 2600] mi, 6 stations/route, prices in
[$3.00, $5.50]) measured the shipped greedy beaten by this oracle on
69/200 routes -- a 34.50% disagreement rate. The in-suite
PenaltyDisagreementFloorTests guard, on the first GUARD_ROUTES=25 of that
same seeded corpus (a structural prefix, never a separate sample),
measured 9/25 -- 36.00% -- comfortably above its DISAGREEMENT_FLOOR of
20%. Both figures were measured once and recorded verbatim, never tuned
toward any target.

A separate, scoping-time measurement -- taken before this phase, on a
286-trial harness that no longer exists (nothing in git, nothing on disk)
-- found the greedy beaten in 157/286 randomized trials (55%). That
figure is cited here for the record, not reconciled with the fresh one
above: the two corpora, harnesses, and route distributions are different,
and reverse-engineering the old harness to make the numbers converge
would recreate exactly the shared-mental-model contamination the
Phase 16/18 split exists to prevent (D-17).

Independence check (D-05): after this module was written and its full
test suite passed, a one-time structural comparison was performed
against the shipped pure-fuel oracle in
routing/tests/test_solver_optimality.py. That module finds its optimum
via a memoized recursive search over (node_index, fuel_miles_remaining)
states, with an explicit memo dict collapsing partial searches that
reach the same state; this module instead loops itertools.combinations
over station subsets and, within each fixed subset, expands an iterative
frontier of complete partial purchase assignments that is never merged
or keyed by state. Nothing here originated from the shipped module: no
function name, signature, docstring sentence, or line of logic was
copied or adapted from it, and no memo table, cache, or state-keyed
dictionary was introduced as a result of this comparison. The two
modules' finite-purchase-amounts lemmas both rest on the same
fill-exactly-or-fill-to-capacity perturbation argument but are scoped
differently -- the shipped lemma ranges over every remaining candidate
in a shared recursion, while this module's lemma ranges only over a
fixed subset's own remaining members, because this module pre-selects
the whole subset before ever computing a purchase amount. The
comparison surfaced no correctness bug in this module.

Phase 21 addendum (PROV-03, D-13/D-14/D-15): this module now also imports
is_estimate_priced/trust_margin_for from routing.services.solver -- the
phase's single shared, total, per-candidate margin-charge lookup (see
solver.py's own docstrings, landed in this same phase's plan 21-04 Task
1). This is not an exception to the independence claim above: neither
function is part of the DP's fixed-charge recurrence or state machine --
each is a two-line, mechanically-total answer to "what flat dollar
charge does this one candidate carry," used here for the identical
reason dp.py/heuristic.py/prune.py all call through them rather than
re-deriving the read, so provenance decision logic stays concentrated at
exactly one seam (routing/tests/test_boundaries.py's
PriceSourceUsagePurityTest allowlist). Reimplementing an independent
copy of that lookup in this oracle would not buy additional
independence; it would only risk silently drifting from the pinned
flat-per-purchase definition D-06 fixes.

optimal_fixed_charge_plan() gained a keyword-only trust_margin=Decimal(0)
parameter: the extended objective is fuel dollars plus penalty*stop_count
plus the summed trust_margin_for(...) of the purchased stations
(OraclePlan.fuel_cost still carries fuel dollars only and never absorbs
either charge). At trust_margin=0, trust_margin_for(...) returns
Decimal(0) for every candidate regardless of price_source, so this
extension is provably inert on every pre-existing call site that never
passes trust_margin= (D-15) -- FixedChargeOracleAnchorTests and
MultiLegFlattenedFixedChargeTests each carry an explicit additional
assertion proving that passing trust_margin=Decimal(0) is byte-identical
to omitting it. single_leg_routes()/flattened_multi_leg_routes() also now
draw price_source per station (D-14), so every existing property class in
this module inherits mixed-provenance exploration even though most of
them never vary trust_margin away from its Decimal(0) default --
TrustMarginAnchorTests and TrustMarginPenaltyConsistencyTests below are
the classes that actually exercise trust_margin != 0. MARGIN_LADDER is
imported from routing.tests.test_trust_margin_rule, never retyped here --
see that module's own docstring for D-09's derivation-before-measurement
discipline. Per this plan's own scope fence, no DP-versus-oracle
differential at non-zero trust_margin is added in this module -- the DP
does not accept trust_margin yet (that lands in a later plan, alongside
TrustMarginOracleDifferentialTests).
"""
import random
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import chain, combinations

from django.test import SimpleTestCase
from hypothesis import example, given, settings
from hypothesis import strategies as st

from routing.services import Candidate
from routing.services.dp import preflight_gap_check, solve_fixed_charge
from routing.services.exceptions import InfeasibleRouteError
from routing.services.solver import (
    ESTIMATE_PRICE_SOURCE,
    is_estimate_priced,
    solve,
    trust_margin_for,
)
from routing.tests import frozen_greedy
from routing.tests.test_trust_margin_rule import MARGIN_LADDER

# The D-12 tuning knob. Pre-authorized to step 6 -> 5 -> 4 if a measured
# runtime breaches the ~30s ceiling for the oracle test classes. The
# 200-example count on the anchor property below is fixed and is NOT a
# knob -- ROADMAP criterion 1 names "the full 200-case Hypothesis run"
# explicitly, so trading examples away would quietly renegotiate it.
MAX_STATIONS = 6

# D-09's tolerance band: both sides accumulate purchases in a different
# order, and Decimal division at the default 28-digit context is inexact,
# so exact equality is not the right assertion. Four orders of magnitude
# tighter than a cent, so this band cannot mask a real optimality bug.
COST_TOLERANCE = Decimal("0.0001")

# The $35 fixed-charge penalty default, traced to ATRI operating-cost data
# (see STATE.md). Test bodies pass penalty explicitly; this is a
# convenience default for later test classes in this module.
DEFAULT_PENALTY = Decimal("35")

# D-10's sweep values: three strictly increasing penalties, the last being
# the sourced $35 figure. Deliberately short -- every rung costs one more
# full oracle solve per Hypothesis example, and FixedChargePenaltyConsistencyTests
# already solves the same drawn route at every rung.
PENALTY_LADDER = (Decimal(0), Decimal("10"), DEFAULT_PENALTY)

# D-14: the value st.sampled_from draws alongside ESTIMATE_PRICE_SOURCE for
# a station's provenance. This is Candidate.price_source's own dataclass
# default (routing/services/solver.py), transcribed here rather than
# imported -- no named constant for it exists in solver.py either; the
# default is inlined there as the same literal.
_RECORDED_PRICE_SOURCE = "opis_indexed"

# D-13's adopted sweep shape (Task 2's measured verdict -- three
# pre-extension wall-clock figures, the ~3x projection, and the module's
# own ~30s ceiling are all recorded in 21-04-SUMMARY.md): the full
# PENALTY_LADDER x MARGIN_LADDER cross product. Decimal(0) is prepended as
# the margin ladder's own control rung, mirroring PENALTY_LADDER's own
# leading Decimal(0) control entry -- MARGIN_LADDER itself (imported from
# routing.tests.test_trust_margin_rule) carries no zero rung of its own.
_MARGIN_SWEEP = (Decimal(0),) + MARGIN_LADDER


@dataclass(frozen=True)
class CorpusParams:
    """The D-16 single source of truth for the disagreement-evidence corpus.

    Deliberately a separate distribution from single_leg_routes() and
    flattened_multi_leg_routes() above (D-14). Those Hypothesis strategies
    prove optimality across many small, varied landscapes -- short routes,
    small tanks, cheap and expensive fuel all mixed together -- because
    that variety is what exercises edge cases. This corpus has a different
    job: demonstrate that the fuel-only and penalty-aware objectives
    genuinely diverge on the real product's shape, not on an arbitrary one.

    So its parameters are fixed to the UI-default loaded semi (6.5 mpg,
    1,050-mile tank) and to routes well past 1,400 miles, because
    REQUIREMENTS.md's Evidence Base records that the problem concentrates
    above ~1,400 mi -- at or under ~800 mi with a large tank, most routes
    need zero or one stop, where both objectives trivially agree and a
    corpus built there would show near-zero disagreement regardless of
    whether the penalty path works.

    stations_per_route is bounded (independent of MAX_STATIONS, which is
    the property tests' own knob above -- the two happen to share a value
    here, but neither is derived from the other) purely so the oracle's
    subset enumeration terminates quickly across a corpus of hundreds of
    routes; it is not tuned toward any particular disagreement rate.
    """

    seed: int
    mpg: Decimal
    tank_range_mi: Decimal
    starting_fuel: Decimal
    min_route_mi: Decimal
    max_route_mi: Decimal
    stations_per_route: int
    min_price: Decimal
    max_price: Decimal
    penalty: Decimal


# The D-16 single shared instance. Every field was chosen before the first
# disagreement measurement was taken, derived from REQUIREMENTS.md's
# Evidence Base and the UI-default vehicle -- never from a desired rate.
CORPUS_PARAMS = CorpusParams(
    seed=20260730,
    mpg=Decimal("6.5"),
    tank_range_mi=Decimal("1050"),
    starting_fuel=Decimal("1"),
    min_route_mi=Decimal("1400"),
    max_route_mi=Decimal("2600"),
    stations_per_route=6,
    min_price=Decimal("3.00"),
    max_price=Decimal("5.50"),
    penalty=Decimal("35"),
)


@dataclass(frozen=True)
class IdenticalPriceClusterParams:
    """D-12's pinned shape for the gap-fill import's characteristic data
    degeneracy: D-09 gives every same-region Overture row the identical
    `retail_price`, so a dense cluster of identically-priced candidates is
    the literal shape the import creates, in a corridor stretch no pinned
    corridor currently reaches -- not a synthetic edge case invented for
    this test.

    Originally considered "2 clusters of 3-5 stations each per route", but
    two clusters whose sizes each independently range 3-5 cannot both fit
    under `MAX_STATIONS=6` except at the single degenerate combination
    3+3=6 -- which would leave the drawn shape almost fixed rather than
    genuinely varied. Raising `MAX_STATIONS` to make room is out of scope
    (it bounds the oracle's own subset enumeration and is not this plan's
    knob to move), so the action's own pre-authorized fallback is adopted
    instead: exactly ONE identically-priced cluster (`cluster_size`
    stations, one shared price, one shared provenance tag -- D-09 assigns
    one provenance to a whole region's rows, never a per-station mix
    within one regional cluster) plus `singleton_count` independently-
    priced, independently-sourced stations. `cluster_size + singleton_count`
    is tied to `MAX_STATIONS` (never hand-typed as a second number that
    could silently drift from it), so every generated route still exhausts
    the oracle's own ceiling rather than under-using it, and the pairing
    self-adjusts if `MAX_STATIONS` is ever stepped down per its own
    pre-authorized 6 -> 5 -> 4 comment above.

    price_min/price_max transcribe CORPUS_PARAMS' own price band verbatim
    -- the cluster is a differently-STRUCTURED population (many stations
    sharing one price rather than each drawn independently), not a
    differently-priced one.
    """

    seed: int
    cluster_size: int
    singleton_count: int
    price_min: Decimal
    price_max: Decimal
    cluster_span_mi: int
    total_route_min_mi: int
    total_route_max_mi: int


# The D-12 single shared instance, pinned before any measurement per this
# plan's own must_haves. cluster_size=4 is inside the CONTEXT-sanctioned
# 3-5 band; singleton_count is derived from MAX_STATIONS so the pair
# always sums to exactly MAX_STATIONS (see the dataclass's own docstring).
IDENTICAL_PRICE_CLUSTER_PARAMS = IdenticalPriceClusterParams(
    seed=20260808,
    cluster_size=4,
    singleton_count=MAX_STATIONS - 4,
    price_min=CORPUS_PARAMS.min_price,
    price_max=CORPUS_PARAMS.max_price,
    cluster_span_mi=20,
    total_route_min_mi=200,
    total_route_max_mi=800,
)

# In-suite guard corpus size -- a CONTEXT-sanctioned discretionary knob,
# chosen to keep PenaltyDisagreementFloorTests inside the D-12 runtime
# ceiling. It is a prefix of any larger corpus built from CORPUS_PARAMS
# (build_corpus consumes its seeded RNG strictly sequentially), so the
# guard can never cherry-pick a favorable subset of routes.
GUARD_ROUTES = 25

# D-15: a loose floor at roughly a third of the expected ~55% disagreement
# rate -- wide enough that ordinary distribution drift never flakes this
# guard, while a silently no-op penalty path (which would score exactly
# 0%) fails it instantly.
DISAGREEMENT_FLOOR = Decimal("0.20")


@dataclass(frozen=True)
class OraclePlan:
    """The oracle's answer for one route at one penalty (and, as of Phase
    21's PROV-03 extension, one trust_margin).

    objective is the equality target for comparisons against another
    solver's result. fuel_cost is the fuel-dollars-only figure Phase 18's
    INTG-02 check needs (total_cost must report fuel dollars only, never
    the penalised total, and -- per D-06 -- never the trust margin
    either). stop_opis_ids is the station set Phase 17's prune property
    will compare against. gallons makes the D-04 nonzero-purchase rule
    inspectable -- every entry is strictly positive by construction.

    Phase 21: objective is fuel_cost + penalty*stop_count + the summed
    trust_margin_for(...) of the purchased stations (see
    optimal_fixed_charge_plan's own docstring). At trust_margin=0 this is
    byte-identical to the pre-Phase-21 objective, since
    trust_margin_for(...) returns Decimal(0) for every candidate
    regardless of price_source whenever trust_margin is itself Decimal(0).

    stop_opis_ids is in increasing distance_from_start_mi order; gallons
    is index-aligned with it. Deliberately does not mirror solver.py's
    FuelPlan/FuelStop shape -- those dataclasses are Phase 18's to change,
    and the oracle must not be coupled to them.

    is_unique_optimum (Phase 17, additive, declared last so it carries a
    default and no existing construction site breaks) reports whether
    exactly one distinct station set (one distinct stop_opis_ids tuple)
    achieves the winning objective, within COST_TOLERANCE. It is computed
    free inside the enumeration this dataclass's producing function
    already performs -- the loop visits every feasible subset regardless,
    so counting how many distinct station sets tie for the winning
    objective costs nothing extra beyond bookkeeping.

    A tied optimum is not a defect: two different station sets can
    legitimately cost the same. It exists so Phase 17's prune property can
    assert the pruned and unpruned station sets match only where "the
    plan" is well-defined -- i.e. where there is exactly one winning
    station set to compare against. Asserting the station set
    unconditionally, even when the optimum is tied, would produce exactly
    the flake the Phase 16 D-09 tolerance decision already rejected:
    pruning can legitimately flip which tied optimum the total order
    returns, without the prune being unsound.
    """

    objective: Decimal
    fuel_cost: Decimal
    stop_opis_ids: tuple[int, ...]
    gallons: tuple[Decimal, ...]
    is_unique_optimum: bool = True

    @property
    def stop_count(self) -> int:
        return len(self.stop_opis_ids)


def _subsets_by_distance(candidates):
    """Every subset of candidates, pre-sorted by
    (distance_from_start_mi, opis_id), yielded already in increasing-
    distance order. r=0 yields the empty subset -- the "reach the finish
    on starting fuel alone" case.

    itertools.combinations emits tuples in the input iterable's order, so
    sorting once up front means every yielded subset is already in
    increasing-distance order, which is also exactly the ordering D-08's
    tie-break needs.
    """
    ordered = sorted(candidates, key=lambda c: (c.distance_from_start_mi, c.opis_id))
    return chain.from_iterable(
        combinations(ordered, r) for r in range(len(ordered) + 1)
    )


def _useful_fill_levels_mi(remaining_nodes_mi, position_mi, fuel_on_arrival_mi, tank_range_mi):
    """Return the deduplicated, sorted useful purchase amounts (in miles of
    range bought) at one station in a fixed subset, given that subset's own
    remaining nodes ahead of this position.

    remaining_nodes_mi holds the distances of the fixed subset's own later
    members plus the finish -- never the whole candidate pool. Scoping to
    the whole pool would multiply the branching factor across every
    enumerated subset and blow the D-12 runtime budget; scoping to the
    subset's own remaining members plus FINISH keeps the useful-amount set
    small and finite.

    The candidate amounts are: for each remaining node, the range that
    lands exactly on that node (node_distance - position_mi -
    fuel_on_arrival_mi); plus the amount that fills the tank to capacity
    (tank_range_mi - fuel_on_arrival_mi). Both are filtered to the
    half-open interval (0, tank_range_mi - fuel_on_arrival_mi] -- strictly
    positive, because a zero purchase is never useful here: the module-
    level D-04 rule treats a station forced to buy zero as a degenerate
    duplicate of the smaller subset that omits it, and that smaller subset
    is enumerated separately by _subsets_by_distance.

    Proof this set is exhaustive over optima, for a fixed subset in which
    every member buys strictly more than zero: take any optimal
    assignment. Suppose some station's purchase amount is neither "exactly
    enough to reach the next node at which a purchase happens or the
    finish" nor "fill to capacity". Then there is slack in both
    directions, so perturb by a small positive epsilon. If this station's
    price is strictly below the price at the next purchase point, buy
    epsilon more here and epsilon less there -- possible because the tank
    is not yet full here and there is slack to give up there -- and total
    cost strictly falls, contradicting optimality. If the price here is
    strictly above the next purchase point's price, buy epsilon less here
    and epsilon more there -- possible because we are above the
    exact-reach amount here -- and total cost strictly falls, again a
    contradiction. If the two prices are equal, the perturbation is
    cost-neutral, so an optimum also exists at one of the interval's
    endpoints. Either way, an optimum exists at one of the finitely many
    listed amounts.

    Penalty-specific extension, which is why this lemma is re-derived here
    rather than imported from the pure-fuel oracle: the perturbation above
    never changes the penalty term, because the subset -- and therefore
    the stop count -- is held fixed throughout the argument, and every
    purchase in the perturbation stays strictly positive by construction.
    The only way the stop count could change is a purchase driven all the
    way to exactly zero, and that possibility is not lost: it is exactly
    the smaller subset that omits this station, which the caller
    enumerates separately and which pays one fewer penalty charge. So,
    conditional on a fixed subset, minimizing fuel dollars plus penalty
    times stop count is exactly minimizing fuel dollars, and the penalty
    enters the objective once, outside this inner enumeration.
    """
    cap_mi = tank_range_mi - fuel_on_arrival_mi
    levels = set()
    for node_mi in remaining_nodes_mi:
        exact_reach_mi = node_mi - position_mi - fuel_on_arrival_mi
        if exact_reach_mi > 0:
            levels.add(exact_reach_mi)
    if cap_mi > 0:
        levels.add(cap_mi)
    return sorted(level_mi for level_mi in levels if 0 < level_mi <= cap_mi)


def _cheapest_fuel_cost_for_subset(subset, total_route_mi, tank_range_mi, mpg, starting_fuel):
    """Return (fuel_cost, gallons) for the cheapest feasible way to fuel
    this fixed, distance-ordered subset such that every member buys
    strictly more than zero gallons, or None if no such assignment reaches
    the finish.

    Enumerated exhaustively as an iterative frontier of complete partial
    assignments, expanded one subset position at a time -- never merged,
    deduplicated, or keyed by position and fuel level. Every partial
    assignment survives independently through the whole expansion; the
    absence of any state-keyed dictionary here is exactly what keeps this
    oracle's subset loop reviewer-legible per D-01.

    Deliberately does not use the classic gas-station greedy per subset,
    even though it is provably optimal for a mandated station set --
    reimplementing that reasoning at the inner loop would reintroduce the
    shared mental model the phase boundary exists to prevent. This
    function stays dumb end to end: it tries every useful amount at every
    position and keeps every surviving partial assignment.

    START is a non-purchasable node: the origin can never be billed for
    fuel, so the initial frontier's fuel is bounded by the fuel actually
    on board (starting_fuel * tank_range_mi). At every real station in the
    subset the bound is tank_range_mi, because the tank can be topped off
    there.
    """
    # Each frontier entry: (position_mi, fuel_mi, cost, gallons_tuple).
    frontier = [(Decimal(0), starting_fuel * tank_range_mi, Decimal(0), ())]

    for i, station in enumerate(subset):
        remaining_nodes_mi = [s.distance_from_start_mi for s in subset[i + 1 :]]
        remaining_nodes_mi.append(total_route_mi)

        next_frontier = []
        for position_mi, fuel_mi, cost, gallons in frontier:
            gap_mi = station.distance_from_start_mi - position_mi
            if gap_mi > fuel_mi:
                continue  # cannot physically reach this station
            fuel_on_arrival_mi = fuel_mi - gap_mi
            for level_mi in _useful_fill_levels_mi(
                remaining_nodes_mi, station.distance_from_start_mi, fuel_on_arrival_mi, tank_range_mi
            ):
                purchase_gallons = level_mi / mpg
                next_frontier.append(
                    (
                        station.distance_from_start_mi,
                        fuel_on_arrival_mi + level_mi,
                        cost + purchase_gallons * station.price_per_gallon,
                        gallons + (purchase_gallons,),
                    )
                )
        frontier = next_frontier
        if not frontier:
            return None

    best = None
    for position_mi, fuel_mi, cost, gallons in frontier:
        gap_mi = total_route_mi - position_mi
        if gap_mi <= fuel_mi and (best is None or cost < best[0]):
            best = (cost, gallons)
    return best


def optimal_fixed_charge_plan(
    candidates,
    total_route_mi,
    *,
    penalty,
    tank_range_mi=Decimal(500),
    mpg=Decimal(10),
    starting_fuel=Decimal(1),
    trust_margin=Decimal(0),
) -> "OraclePlan | None":
    """Return the OraclePlan minimizing fuel dollars plus penalty times
    stop count plus the summed per-candidate trust margin (Phase 21,
    PROV-03), over every subset of candidates, or None if no subset
    yields a feasible plan.

    penalty is a required keyword-only plain function argument -- this
    oracle never reads a Django setting; that seam belongs to Phase 18's
    INTG-01. Per D-06, infeasibility falls purely out of the enumeration:
    there is no pre-flight gap check anywhere in this module, so this
    oracle and a DP's own gap check can never agree merely because they
    share a reachability rule.

    trust_margin is additive, keyword-only, and defaults to Decimal(0) --
    D-15's inertness guarantee -- mirroring how solve()'s own deadline=
    parameter was added in Phase 18.1. For each candidate purchased in a
    subset, trust_margin_for(candidate, trust_margin) (imported from
    routing.services.solver, the phase's single shared margin lookup --
    see this module's own docstring) is added to that subset's objective;
    it is Decimal(0) for every candidate whenever trust_margin itself is
    Decimal(0), and is Decimal(0) for a real-priced (non-estimate)
    candidate at any trust_margin. fuel_cost NEVER absorbs it (D-06):
    OraclePlan.fuel_cost stays the plain sum of gallons*price only.

    The margin is added exactly once per purchasing station, alongside
    the penalty term, OUTSIDE _cheapest_fuel_cost_for_subset's own inner
    enumeration -- never inside it. This is sound for the identical
    reason penalty already sits outside that inner enumeration
    (_useful_fill_levels_mi's own docstring proof): margin, like penalty,
    is a flat charge that does not depend on the purchased AMOUNT, only
    on whether a station is purchased at all and (unlike penalty) on that
    station's own provenance -- so, conditional on a fixed subset,
    minimizing fuel dollars is still exactly minimizing fuel dollars, and
    both the penalty and the margin enter the objective once, after the
    inner fuel-minimization is already decided.

    Ties are broken by an explicit total order (D-08): lowest objective,
    then fewest stops, then the sorted tuple of distance_from_start_mi
    (already the natural order of a subset from _subsets_by_distance),
    then the tuple of opis_id. Deterministic across Hypothesis seeds and
    repeat runs -- there is no last-writer-wins path.

    Uniqueness (Phase 17, extended Phase 21): alongside best_plan/best_key,
    one (objective, stop_opis_ids) entry is accumulated per feasible
    subset -- at MAX_STATIONS=6 that is at most 64 entries, so the memory
    cost is nil. After the loop, with the winning (margin-augmented)
    objective known, the distinct stop_opis_ids values whose objective is
    within COST_TOLERANCE of it are counted; OraclePlan.is_unique_optimum
    is set to whether that count is exactly one -- the margin introduces
    new tie shapes (e.g. two same-priced real stations either of which
    the margin makes equally attractive), and this signal is already the
    tool Phase 17 built to handle a tie by narrowing the claim rather than
    fudging a tie-break. COST_TOLERANCE -- never exact equality -- is used
    for the same reason it is used everywhere else in this module: both
    sides accumulate purchases in a different order and Decimal division
    at the default context is inexact, so exact equality would report
    spurious non-uniqueness for what is really the same tied optimum.
    """
    best_plan = None
    best_key = None
    feasible_entries = []
    for subset in _subsets_by_distance(candidates):
        result = _cheapest_fuel_cost_for_subset(subset, total_route_mi, tank_range_mi, mpg, starting_fuel)
        if result is None:
            continue
        fuel_cost, gallons = result
        stop_opis_ids = tuple(station.opis_id for station in subset)
        distances_mi = tuple(station.distance_from_start_mi for station in subset)
        margin_total = sum(
            (trust_margin_for(station, trust_margin) for station in subset), Decimal(0)
        )
        objective = fuel_cost + penalty * len(subset) + margin_total
        feasible_entries.append((objective, stop_opis_ids))
        key = (objective, len(subset), distances_mi, stop_opis_ids)
        if best_key is None or key < best_key:
            best_key = key
            best_plan = OraclePlan(
                objective=objective,
                fuel_cost=fuel_cost,
                stop_opis_ids=stop_opis_ids,
                gallons=gallons,
            )
    if best_plan is not None:
        winning_objective = best_plan.objective
        tied_station_sets = {
            stop_opis_ids
            for objective, stop_opis_ids in feasible_entries
            if abs(objective - winning_objective) <= COST_TOLERANCE
        }
        best_plan = replace(best_plan, is_unique_optimum=len(tied_station_sets) == 1)
    return best_plan


def _candidates_from_tuples(station_tuples, total_route_mi):
    """Build Candidate objects from drawn (price, distance, price_source)
    tuples, dropping any tuple whose distance exceeds total_route_mi.
    Dropping -- not raising -- keeps generated inputs inside solve()'s own
    contract: an out-of-range candidate would trigger
    InvalidRouteInputError, which is input validation on the caller's
    contract, not the optimality property under test here.

    price_source is the D-14 addition (Phase 21): every station tuple now
    carries provenance alongside price and distance, drawn by
    single_leg_routes()/flattened_multi_leg_routes() from
    (_RECORDED_PRICE_SOURCE, ESTIMATE_PRICE_SOURCE), so the property
    classes below explore mixed-provenance configurations adversarially
    rather than only the all-recorded case.
    """
    candidates = []
    for i, (price, dist, price_source) in enumerate(station_tuples):
        dist_mi = Decimal(dist)
        if dist_mi <= total_route_mi:
            candidates.append(
                Candidate(
                    name=f"S{i}",
                    opis_id=i,
                    price_per_gallon=price,
                    distance_from_start_mi=dist_mi,
                    price_source=price_source,
                )
            )
    return candidates


def build_corpus(n_routes, *, params=CORPUS_PARAMS):
    """Deterministically build n_routes long-haul (candidates, total_route_mi)
    pairs from a single random.Random(params.seed), consumed strictly
    sequentially one route at a time.

    Sequential consumption is what makes the corpus prefix-stable: the
    first N routes of build_corpus(200) are byte-identical to
    build_corpus(N) for any N <= 200, because nothing about drawing route
    N+1 can affect what was already drawn for route N. This is what makes
    GUARD_ROUTES a structural prefix of the management command's larger
    corpus rather than an independently-sampled, potentially
    cherry-picked, subset.

    Each route: total_route_mi is a whole number of miles drawn uniformly
    from [min_route_mi, max_route_mi]. stations_per_route stations are
    placed by partitioning the route into stations_per_route + 1 equal
    slots and putting station j (1-indexed) at j * slot_width, jittered by
    a uniform offset in [-0.35, +0.35] * slot_width and clamped strictly
    inside (0, total_route_mi). With slots this even and jitter bounded
    well under half a slot width, the largest possible gap between two
    consecutive nodes (including START and FINISH) is 1.7 * slot_width,
    which stays comfortably under tank_range_mi across the whole
    [min_route_mi, max_route_mi] band -- every drawn route is feasible by
    construction, not by rejection sampling. Prices are drawn uniformly
    from [min_price, max_price], quantized to whole cents.

    All arithmetic is int/Decimal throughout -- the RNG's own randint()
    calls are the only place an integer is drawn, and every mile/price
    value derived from them is built as a Decimal, never a float.
    """
    rng = random.Random(params.seed)
    slots = params.stations_per_route + 1
    min_price_cents = int(params.min_price * 100)
    max_price_cents = int(params.max_price * 100)

    routes = []
    for _ in range(n_routes):
        total_route_mi = Decimal(rng.randint(int(params.min_route_mi), int(params.max_route_mi)))
        slot_width_mi = total_route_mi / Decimal(slots)

        candidates = []
        for j in range(1, params.stations_per_route + 1):
            base_mi = Decimal(j) * slot_width_mi
            # Jitter as thousandths of a slot width, in [-0.35, +0.35].
            jitter_thousandths = Decimal(rng.randint(-350, 350))
            jitter_mi = jitter_thousandths * slot_width_mi / Decimal(1000)
            position_mi = base_mi + jitter_mi
            position_mi = max(Decimal("0.01"), min(position_mi, total_route_mi - Decimal("0.01")))

            price_cents = rng.randint(min_price_cents, max_price_cents)
            price = Decimal(price_cents) / Decimal(100)

            candidates.append(
                Candidate(
                    name=f"S{j}",
                    opis_id=j - 1,
                    price_per_gallon=price,
                    distance_from_start_mi=position_mi,
                )
            )
        routes.append((candidates, total_route_mi))
    return routes


@dataclass(frozen=True)
class DisagreementReport:
    """The result of one measure_disagreement() run: how often, and out of
    how many comparable routes, the shipped greedy's penalised objective
    was beaten by the penalty-aware oracle's true optimum.

    n_compared excludes n_infeasible routes from its denominator --
    infeasibility is not a disagreement, and counting it as one would
    understate the rate for reasons having nothing to do with the penalty
    objective. rate is Decimal(n_disagree) / Decimal(n_compared), or
    Decimal(0) if n_compared is 0.
    """

    n_routes: int
    n_compared: int
    n_disagree: int
    n_infeasible: int
    rate: Decimal
    params: CorpusParams
    penalty: Decimal


def measure_disagreement(n_routes, *, params=CORPUS_PARAMS, penalty=None):
    """Build a corpus of n_routes routes from params (via build_corpus) and
    measure how often the shipped greedy's penalised objective
    (plan.total_cost + penalty * len(plan.stops)) is strictly beaten by
    optimal_fixed_charge_plan()'s true optimum at the same penalty.

    penalty defaults to params.penalty -- exposed as a separate argument
    so both the penalty=0 sanity check and Phase 18's re-measurement
    against a real DP can vary it without touching the corpus itself.

    Both sides are solved on the SAME drawn route at the SAME vehicle
    parameters. A feasibility disagreement between the two is a bug --
    not a disagreement to be counted -- and raises AssertionError naming
    the offending route index. A negative (greedy_objective -
    oracle.objective) beyond -COST_TOLERANCE is also a bug: the oracle is
    optimal by construction over every subset the greedy could have
    chosen, so it can never be beaten.
    """
    if penalty is None:
        penalty = params.penalty

    corpus = build_corpus(n_routes, params=params)
    n_disagree = 0
    n_infeasible = 0
    n_compared = 0

    for idx, (candidates, total_route_mi) in enumerate(corpus):
        try:
            greedy_plan = frozen_greedy.solve(
                candidates,
                total_route_mi,
                tank_range_mi=params.tank_range_mi,
                mpg=params.mpg,
                starting_fuel=params.starting_fuel,
            )
            greedy_feasible = True
        except InfeasibleRouteError:
            greedy_plan = None
            greedy_feasible = False

        oracle_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=penalty,
            tank_range_mi=params.tank_range_mi,
            mpg=params.mpg,
            starting_fuel=params.starting_fuel,
        )
        oracle_feasible = oracle_plan is not None

        if greedy_feasible != oracle_feasible:
            raise AssertionError(
                f"route {idx}: feasibility verdicts disagree "
                f"(greedy={greedy_feasible}, oracle={oracle_feasible}) -- "
                f"a feasibility split is a bug in the corpus or the oracle, "
                f"not a disagreement to count; total_route_mi={total_route_mi}, "
                f"candidates={candidates!r}"
            )

        if not greedy_feasible:
            n_infeasible += 1
            continue

        n_compared += 1
        greedy_objective = greedy_plan.total_cost + penalty * len(greedy_plan.stops)
        diff = greedy_objective - oracle_plan.objective
        if diff < -COST_TOLERANCE:
            raise AssertionError(
                f"route {idx}: oracle objective ({oracle_plan.objective}) exceeds "
                f"the greedy's penalised objective ({greedy_objective}) by "
                f"{-diff} -- the oracle is optimal by construction and must never "
                f"be beaten; this means it is missing feasible plans. "
                f"total_route_mi={total_route_mi}, candidates={candidates!r}"
            )
        if diff > COST_TOLERANCE:
            n_disagree += 1

    rate = Decimal(n_disagree) / Decimal(n_compared) if n_compared else Decimal(0)
    return DisagreementReport(
        n_routes=n_routes,
        n_compared=n_compared,
        n_disagree=n_disagree,
        n_infeasible=n_infeasible,
        rate=rate,
        params=params,
        penalty=penalty,
    )


@st.composite
def single_leg_routes(draw):
    """Draw a single-leg route: a candidate list plus the four scalar
    solve()/optimal_fixed_charge_plan() arguments, as Decimals throughout.

    D-14 (Phase 21): each station tuple's third element draws a
    price_source, so the candidate list this strategy produces is
    adversarially mixed-provenance rather than uniformly
    _RECORDED_PRICE_SOURCE. unique_by stays keyed on the tuple's distance
    element only -- price_source never participates in station identity.
    """
    total_route_mi = Decimal(draw(st.integers(min_value=1, max_value=800)))
    station_tuples = draw(
        st.lists(
            st.tuples(
                st.decimals(min_value=Decimal("1.00"), max_value=Decimal("6.00"), places=2),
                st.integers(min_value=1, max_value=800),
                st.sampled_from((_RECORDED_PRICE_SOURCE, ESTIMATE_PRICE_SOURCE)),
            ),
            max_size=MAX_STATIONS,
            unique_by=lambda t: t[1],
        )
    )
    tank_range_mi = Decimal(draw(st.integers(min_value=20, max_value=800)))
    mpg = Decimal(draw(st.integers(min_value=1, max_value=50)))
    starting_fuel = draw(st.decimals(min_value=Decimal("0.00"), max_value=Decimal("1.00"), places=2))
    candidates = _candidates_from_tuples(station_tuples, total_route_mi)
    return candidates, total_route_mi, tank_range_mi, mpg, starting_fuel


@st.composite
def flattened_multi_leg_routes(draw):
    """Draw a flattened multi-leg route: a candidate list, the flattened
    total_route_mi, the leg boundary offsets, and the four scalar
    solve()/optimal_fixed_charge_plan() arguments.

    Mirrors the production flattening rule in routing/services/multi_leg.py
    (permitted read for this rule; the shipped oracle in
    test_solver_optimality.py is not, per D-05/D-18): each leg gets its
    own local (price, local_distance) station tuples, and every leg's
    local distances are offset-summed onto one continuous scale by
    adding the cumulative length of all prior legs -- never a merged
    multi-leg line, never solved leg by leg. The oracle and the shipped
    greedy both see exactly one flattened candidate list on one
    continuous distance scale.

    Uses @st.composite rather than chained flatmap calls: each leg's
    offset depends on values drawn for prior legs, and composite keeps
    the whole flattened structure as one strategy Hypothesis can shrink
    holistically -- disconnected per-leg lists summed in the test body
    would forfeit shrinking across the leg boundary.

    The flattened candidate count is bounded to MAX_STATIONS by dividing
    across legs (max(1, MAX_STATIONS // num_legs) per leg) and by
    truncating the flattened list to MAX_STATIONS entries before
    returning -- never by adding. The oracle's cost is a function of
    total candidate count, not leg count, so three legs of MAX_STATIONS
    stations each would be MAX_STATIONS=18 in disguise and blow the
    D-12 runtime budget by orders of magnitude.

    D-14 (Phase 21): each local station tuple's third element draws a
    price_source, exactly as single_leg_routes() now does, so the
    flattened candidate list is adversarially mixed-provenance too.
    """
    num_legs = draw(st.integers(min_value=2, max_value=3))
    per_leg_cap = max(1, MAX_STATIONS // num_legs)

    offset_mi = Decimal(0)
    leg_boundaries_mi = [offset_mi]
    flattened_tuples = []
    for _ in range(num_legs):
        leg_len_mi = draw(st.integers(min_value=200, max_value=800))
        local_tuples = draw(
            st.lists(
                st.tuples(
                    st.decimals(min_value=Decimal("1.00"), max_value=Decimal("6.00"), places=2),
                    # strictly inside this leg -- never on or past its far boundary
                    st.integers(min_value=1, max_value=leg_len_mi - 1),
                    st.sampled_from((_RECORDED_PRICE_SOURCE, ESTIMATE_PRICE_SOURCE)),
                ),
                max_size=per_leg_cap,
                unique_by=lambda t: t[1],
            )
        )
        for price, local_dist_mi, price_source in local_tuples:
            flattened_tuples.append((price, offset_mi + Decimal(local_dist_mi), price_source))
        offset_mi += Decimal(leg_len_mi)
        leg_boundaries_mi.append(offset_mi)

    total_route_mi = offset_mi
    # Truncate to MAX_STATIONS -- the flattened total, not the per-leg
    # caps alone, is the bound the oracle's subset enumeration must obey.
    flattened_tuples = flattened_tuples[:MAX_STATIONS]
    candidates = _candidates_from_tuples(flattened_tuples, total_route_mi)

    tank_range_mi = Decimal(draw(st.integers(min_value=20, max_value=800)))
    mpg = Decimal(draw(st.integers(min_value=1, max_value=50)))
    starting_fuel = draw(st.decimals(min_value=Decimal("0.00"), max_value=Decimal("1.00"), places=2))
    return candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, leg_boundaries_mi


@st.composite
def identical_price_cluster_routes(draw):
    """Draw a route to IDENTICAL_PRICE_CLUSTER_PARAMS' pinned shape: one
    identically-priced cluster of cluster_size stations (one shared price,
    one shared provenance tag) plus singleton_count independently-priced,
    independently-sourced stations -- exactly the dense-price-cluster
    degeneracy D-09 guarantees the gap-fill import creates. Built through
    _candidates_from_tuples, the same path single_leg_routes() and
    flattened_multi_leg_routes() use, so the resulting Candidate objects
    are indistinguishable from real corridor candidates.

    The cluster's provenance and price are each drawn ONCE per example,
    never per station -- D-09 assigns one provenance and one price to a
    whole region's worth of rows, never a per-station mix within the same
    regional cluster. The singleton stations are drawn independently of
    the cluster and of each other, mirroring single_leg_routes()' own
    per-station draw, so the module's existing mixed-provenance adversarial
    exploration is preserved for them.
    """
    params = IDENTICAL_PRICE_CLUSTER_PARAMS
    total_route_mi = Decimal(
        draw(st.integers(min_value=params.total_route_min_mi, max_value=params.total_route_max_mi))
    )

    cluster_price = draw(st.decimals(min_value=params.price_min, max_value=params.price_max, places=2))
    cluster_source = draw(st.sampled_from((_RECORDED_PRICE_SOURCE, ESTIMATE_PRICE_SOURCE)))
    cluster_start = draw(
        st.integers(min_value=1, max_value=max(1, int(total_route_mi) - params.cluster_span_mi - 1))
    )
    cluster_offsets = draw(
        st.lists(
            st.integers(min_value=0, max_value=params.cluster_span_mi),
            min_size=params.cluster_size,
            max_size=params.cluster_size,
            unique=True,
        )
    )
    cluster_positions = sorted({cluster_start + offset for offset in cluster_offsets})
    station_tuples = [(cluster_price, position, cluster_source) for position in cluster_positions]

    used_positions = set(cluster_positions)
    for _ in range(params.singleton_count):
        position = draw(
            st.integers(min_value=1, max_value=int(total_route_mi)).filter(
                lambda p: p not in used_positions
            )
        )
        used_positions.add(position)
        price = draw(st.decimals(min_value=params.price_min, max_value=params.price_max, places=2))
        source = draw(st.sampled_from((_RECORDED_PRICE_SOURCE, ESTIMATE_PRICE_SOURCE)))
        station_tuples.append((price, position, source))

    tank_range_mi = Decimal(draw(st.integers(min_value=20, max_value=800)))
    mpg = Decimal(draw(st.integers(min_value=1, max_value=50)))
    starting_fuel = draw(st.decimals(min_value=Decimal("0.00"), max_value=Decimal("1.00"), places=2))
    candidates = _candidates_from_tuples(station_tuples, total_route_mi)
    return candidates, total_route_mi, tank_range_mi, mpg, starting_fuel


class FixedChargeOracleAnchorTests(SimpleTestCase):
    """The D-09 / ROADMAP-criterion-1 anchor: at penalty=0, this oracle
    must agree with the shipped greedy on feasibility and on fuel cost,
    across the full fixed 200-example Hypothesis run.

    D-10: "the shipped greedy" means routing.tests.frozen_greedy.solve, a
    byte-verbatim, provenance-recorded copy of the pre-Phase-18
    routing.services.solver.solve -- not the live solve() import, which
    Phase 18's later plan rewrites to delegate to the DP. Retargeting this
    anchor at the frozen referee is what keeps ROADMAP criterion 1's
    "agrees with the shipped greedy on the full 200-case Hypothesis run"
    claim reproducible verbatim once solve() stops being the greedy.
    """

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=200)
    def test_penalty_zero_anchors_to_shipped_greedy(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        try:
            greedy_plan = frozen_greedy.solve(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            greedy_feasible, greedy_fuel_cost = True, greedy_plan.total_cost
        except InfeasibleRouteError:
            greedy_feasible, greedy_fuel_cost = False, None

        oracle_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=Decimal(0),
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )
        oracle_feasible = oracle_plan is not None

        context = (
            f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
            f"tank_range_mi={tank_range_mi}, mpg={mpg}, starting_fuel={starting_fuel}"
        )
        self.assertEqual(
            greedy_feasible,
            oracle_feasible,
            f"feasibility verdicts disagree: greedy={greedy_feasible}, "
            f"oracle={oracle_feasible}; {context}",
        )
        if greedy_feasible:
            self.assertEqual(
                oracle_plan.objective,
                oracle_plan.fuel_cost,
                f"oracle objective must equal fuel_cost at penalty=0; {context}",
            )
            self.assertLessEqual(
                abs(oracle_plan.fuel_cost - greedy_fuel_cost),
                COST_TOLERANCE,
                f"oracle fuel_cost={oracle_plan.fuel_cost} vs greedy "
                f"total_cost={greedy_fuel_cost} exceeds tolerance; {context}",
            )
            # D-15/PROV-03 inertness assertion (mandated by plan 21-04):
            # passing trust_margin=Decimal(0) EXPLICITLY must be
            # byte-identical to the call above, which omits trust_margin
            # entirely and relies on its Decimal(0) default. Candidates
            # here are drawn with mixed price_source (D-14) -- this is
            # what proves the extension is inert at trust_margin=0
            # regardless of provenance, not merely inert on an
            # all-recorded-price corpus.
            explicit_zero_margin_plan = optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=Decimal(0),
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                trust_margin=Decimal(0),
            )
            self.assertEqual(
                explicit_zero_margin_plan,
                oracle_plan,
                f"passing trust_margin=Decimal(0) explicitly must be "
                f"byte-identical to omitting it (D-15 inertness); {context}",
            )


class FixedChargePenaltyConsistencyTests(SimpleTestCase):
    """D-10: non-zero penalties are exercised by oracle-vs-oracle self-
    consistency properties, since the shipped greedy is penalty-blind and
    no DP exists yet. Every assertion compares the oracle's own
    recomputed values across PENALTY_LADDER on the SAME drawn route --
    never an oracle result at one penalty against a solve() result at a
    different penalty. solve() is fixed-objective (pure fuel dollars) and
    is only a valid comparison point at penalty=0; that comparison is
    FixedChargeOracleAnchorTests above.

    max_examples=50 is a discretionary formulation choice for this class
    only (16-CONTEXT.md grants the precise formulation of the D-10
    properties beyond the two named): each example already performs
    three full oracle solves (one per PENALTY_LADDER rung), so 50
    examples is 150 oracle solves per test method, roughly three
    quarters of one anchor class's cost. This is NOT a lowering of the
    anchor classes' max_examples=200, which stays fixed by ROADMAP
    criterion 1.

    Inequality assertions need no tolerance band except in the fuel-cost
    property below, because >= and <= already absorb summation-order
    noise.
    """

    def _plans_by_rung(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        return {
            penalty: optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=penalty,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            for penalty in PENALTY_LADDER
        }

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_feasibility_is_penalty_invariant(self, drawn_route):
        """Property 5: plan(p) is None iff plan(0) is None, for every
        rung. Charging for stops can never make a reachable route
        unreachable -- if this ever fails, the subset enumeration is
        dropping feasible plans. Checked first and unconditionally (no
        skip) so a silent skip in the other methods below can never hide
        a genuine feasibility disagreement.
        """
        plans = self._plans_by_rung(drawn_route)
        zero_feasible = plans[Decimal(0)] is not None
        for penalty, plan in plans.items():
            self.assertEqual(
                plan is not None,
                zero_feasible,
                f"feasibility at penalty={penalty} disagrees with the "
                f"penalty=0 verdict={zero_feasible}; drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_stop_count_non_increasing_as_penalty_rises(self, drawn_route):
        """Property 1 (D-10, named): for consecutive rungs p_lo < p_hi,
        plan(p_hi).stop_count <= plan(p_lo).stop_count. Raising the price
        of a stop can never make the optimum want more stops."""
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return  # feasibility invariant is asserted separately above
        for p_lo, p_hi in zip(PENALTY_LADDER, PENALTY_LADDER[1:]):
            self.assertLessEqual(
                plans[p_hi].stop_count,
                plans[p_lo].stop_count,
                f"stop_count rose from penalty={p_lo} "
                f"({plans[p_lo].stop_count}) to penalty={p_hi} "
                f"({plans[p_hi].stop_count}); drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_objective_at_least_penalty_zero_objective(self, drawn_route):
        """Property 2 (D-10, named): for every rung p,
        plan(p).objective >= plan(0).objective."""
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return
        zero_objective = plans[Decimal(0)].objective
        for penalty in PENALTY_LADDER:
            self.assertGreaterEqual(
                plans[penalty].objective,
                zero_objective,
                f"objective at penalty={penalty} "
                f"({plans[penalty].objective}) fell below the penalty=0 "
                f"objective ({zero_objective}); drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_objective_non_decreasing_across_ladder(self, drawn_route):
        """Property 3 (discretionary generalization of property 2): for
        consecutive rungs, plan(p_hi).objective >= plan(p_lo).objective."""
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return
        for p_lo, p_hi in zip(PENALTY_LADDER, PENALTY_LADDER[1:]):
            self.assertGreaterEqual(
                plans[p_hi].objective,
                plans[p_lo].objective,
                f"objective fell from penalty={p_lo} "
                f"({plans[p_lo].objective}) to penalty={p_hi} "
                f"({plans[p_hi].objective}); drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_fuel_cost_non_decreasing_as_penalty_rises(self, drawn_route):
        """Property 4 (discretionary): for consecutive rungs,
        plan(p_hi).fuel_cost >= plan(p_lo).fuel_cost - COST_TOLERANCE. The
        penalty=0 plan minimizes fuel alone, so trading stops away for a
        higher penalty can only ever cost more fuel. The tolerance band
        absorbs summation-order noise only; it is not slack in the claim."""
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return
        for p_lo, p_hi in zip(PENALTY_LADDER, PENALTY_LADDER[1:]):
            self.assertGreaterEqual(
                plans[p_hi].fuel_cost,
                plans[p_lo].fuel_cost - COST_TOLERANCE,
                f"fuel_cost fell from penalty={p_lo} "
                f"({plans[p_lo].fuel_cost}) to penalty={p_hi} "
                f"({plans[p_hi].fuel_cost}) beyond the summation-order "
                f"tolerance band; drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_plan_internal_consistency_across_ladder(self, drawn_route):
        """Property 6 (discretionary, direct guard on D-04 and D-07): for
        every rung with a feasible plan: objective == fuel_cost +
        penalty*stop_count exactly (no tolerance -- Decimal times int is
        exact); len(gallons) == len(stop_opis_ids); every gallons entry
        is strictly positive (D-04's nonzero-purchase rule); and
        stop_opis_ids has no duplicates and is in increasing distance
        order."""
        candidates = drawn_route[0]
        candidate_by_id = {c.opis_id: c for c in candidates}
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return
        for penalty, plan in plans.items():
            self.assertEqual(
                plan.objective,
                plan.fuel_cost + penalty * plan.stop_count,
                f"objective does not equal fuel_cost + penalty*stop_count "
                f"at penalty={penalty}; plan={plan!r}; drawn_route={drawn_route!r}",
            )
            self.assertEqual(
                len(plan.gallons),
                len(plan.stop_opis_ids),
                f"gallons/stop_opis_ids length mismatch at penalty={penalty}; "
                f"plan={plan!r}; drawn_route={drawn_route!r}",
            )
            for gallons in plan.gallons:
                self.assertGreater(
                    gallons,
                    Decimal(0),
                    f"a stop purchased zero gallons at penalty={penalty}; "
                    f"plan={plan!r}; drawn_route={drawn_route!r}",
                )
            self.assertEqual(
                len(set(plan.stop_opis_ids)),
                len(plan.stop_opis_ids),
                f"stop_opis_ids has duplicates at penalty={penalty}; "
                f"plan={plan!r}; drawn_route={drawn_route!r}",
            )
            distances_mi = [candidate_by_id[opis_id].distance_from_start_mi for opis_id in plan.stop_opis_ids]
            self.assertEqual(
                distances_mi,
                sorted(distances_mi),
                f"stop_opis_ids not in increasing distance order at "
                f"penalty={penalty}; plan={plan!r}; drawn_route={drawn_route!r}",
            )


class DpOracleDifferentialTests(SimpleTestCase):
    """D-10/D-13: the DP arm of the three-referee design. For every drawn
    route, at each rung of PENALTY_LADDER, routing.services.dp's
    solve_fixed_charge is compared against this module's own independent
    subset-enumeration oracle -- the SAME oracle FixedChargeOracleAnchorTests
    and FixedChargePenaltyConsistencyTests already answer to, never a
    second, re-derived judge.

    The comparison is structured exactly as PruneOracleDifferentialTests
    (routing/tests/test_prune_soundness.py) already established for D-13:
    the penalised objective is asserted unconditionally, and the station
    set plus total_cost are asserted only when the oracle's optimum is
    strictly unique (OraclePlan.is_unique_optimum) -- asserting the
    station set unconditionally would ride a second, unproven claim (that
    two independent implementations resolve ties identically) on top of
    the first.

    Feasibility is checked via preflight_gap_check, the DP's own
    documented precondition (D-17) -- passing it is exactly the DP's
    feasibility condition, so this is the DP-side analog of the try/except
    InfeasibleRouteError pattern used against the frozen greedy elsewhere
    in this module.

    The oracle enumeration for a drawn route is computed exactly once per
    PENALTY_LADDER rung and reused for both the unconditional objective
    comparison and the conditional station-set/total_cost comparison at
    that rung -- never recomputed.
    """

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=200)
    def test_dp_matches_oracle_across_penalty_ladder(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        for penalty in PENALTY_LADDER:
            oracle_plan = optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=penalty,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )

            try:
                preflight_gap_check(
                    candidates,
                    total_route_mi=total_route_mi,
                    tank_range_mi=tank_range_mi,
                    starting_fuel=starting_fuel,
                )
                dp_feasible = True
            except InfeasibleRouteError:
                dp_feasible = False

            context = (
                f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
                f"tank_range_mi={tank_range_mi}, mpg={mpg}, "
                f"starting_fuel={starting_fuel}, penalty={penalty}"
            )

            self.assertEqual(
                oracle_plan is not None,
                dp_feasible,
                f"feasibility verdicts disagree: oracle_feasible="
                f"{oracle_plan is not None}, dp_feasible={dp_feasible}; {context}",
            )
            if oracle_plan is None:
                continue

            dp_plan = solve_fixed_charge(
                candidates,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=penalty,
            )

            self.assertLessEqual(
                abs(dp_plan.penalised_objective - oracle_plan.objective),
                COST_TOLERANCE,
                f"dp penalised_objective ({dp_plan.penalised_objective}) "
                f"differs from oracle objective ({oracle_plan.objective}) "
                f"beyond COST_TOLERANCE; {context}",
            )

            if oracle_plan.is_unique_optimum:
                dp_stop_opis_ids = tuple(stop.opis_id for stop in dp_plan.stops)
                self.assertEqual(
                    dp_stop_opis_ids,
                    oracle_plan.stop_opis_ids,
                    f"station set differs though the oracle optimum is "
                    f"strictly unique; dp_stop_opis_ids={dp_stop_opis_ids!r}; "
                    f"{context}",
                )
                self.assertLessEqual(
                    abs(dp_plan.total_cost - oracle_plan.fuel_cost),
                    COST_TOLERANCE,
                    f"dp total_cost ({dp_plan.total_cost}) differs from "
                    f"oracle fuel_cost ({oracle_plan.fuel_cost}) beyond "
                    f"COST_TOLERANCE though the oracle optimum is strictly "
                    f"unique; {context}",
                )


class MultiLegFlattenedFixedChargeTests(SimpleTestCase):
    """D-11: a penalty-aware twin of the multi-leg flattened optimality
    class, built from offset-summed per-leg candidate lists on one
    continuous distance scale -- never a merged multi-leg line and never
    solved leg by leg. The oracle is leg-agnostic (it only ever sees one
    flattened candidate list), which is what makes this twin cheap and
    what Phase 18's PROOF-04 will build on.
    """

    @given(flattened_multi_leg_routes())
    @settings(deadline=None, max_examples=200)
    def test_penalty_zero_anchors_to_shipped_greedy_on_flattened_multi_leg(self, drawn_route):
        """The D-11 / D-09 anchor on flattened multi-leg input: same
        assertion shape as FixedChargeOracleAnchorTests above -- matching
        feasibility verdicts, and fuel cost within COST_TOLERANCE of
        solve()'s total_cost. Also asserts the drawn route really is
        multi-leg (at least two leg boundaries, flattened total equal to
        the sum of leg lengths) and that the flattened candidate count
        never exceeds MAX_STATIONS, so a strategy regression that
        silently collapses to one leg or over-generates stations fails
        loudly instead of quietly retesting the single-leg case."""
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, leg_boundaries_mi = drawn_route

        self.assertGreaterEqual(
            len(leg_boundaries_mi),
            3,
            f"expected at least two leg boundaries beyond the start "
            f"(>=3 boundary points for >=2 legs), got "
            f"{leg_boundaries_mi!r} -- a strategy regression may have "
            f"collapsed to one leg",
        )
        self.assertEqual(
            leg_boundaries_mi[-1],
            total_route_mi,
            f"flattened total_route_mi={total_route_mi} does not equal "
            f"the sum of leg lengths (final boundary "
            f"{leg_boundaries_mi[-1]}); leg_boundaries_mi={leg_boundaries_mi!r}",
        )
        self.assertLessEqual(
            len(candidates),
            MAX_STATIONS,
            f"flattened candidate count {len(candidates)} exceeds "
            f"MAX_STATIONS={MAX_STATIONS}; candidates={candidates!r}",
        )

        try:
            greedy_plan = frozen_greedy.solve(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            greedy_feasible, greedy_fuel_cost = True, greedy_plan.total_cost
        except InfeasibleRouteError:
            greedy_feasible, greedy_fuel_cost = False, None

        oracle_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=Decimal(0),
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )
        oracle_feasible = oracle_plan is not None

        context = (
            f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
            f"leg_boundaries_mi={leg_boundaries_mi!r}, "
            f"tank_range_mi={tank_range_mi}, mpg={mpg}, starting_fuel={starting_fuel}"
        )
        self.assertEqual(
            greedy_feasible,
            oracle_feasible,
            f"feasibility verdicts disagree on flattened multi-leg "
            f"input: greedy={greedy_feasible}, oracle={oracle_feasible}; {context}",
        )
        if greedy_feasible:
            self.assertLessEqual(
                abs(oracle_plan.fuel_cost - greedy_fuel_cost),
                COST_TOLERANCE,
                f"oracle fuel_cost={oracle_plan.fuel_cost} vs greedy "
                f"total_cost={greedy_fuel_cost} exceeds tolerance on "
                f"flattened multi-leg input; {context}",
            )
            # D-15/PROV-03 inertness assertion, mirroring
            # FixedChargeOracleAnchorTests' own addition above, on
            # flattened multi-leg input.
            explicit_zero_margin_plan = optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=Decimal(0),
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                trust_margin=Decimal(0),
            )
            self.assertEqual(
                explicit_zero_margin_plan,
                oracle_plan,
                f"passing trust_margin=Decimal(0) explicitly must be "
                f"byte-identical to omitting it on flattened multi-leg "
                f"input (D-15 inertness); {context}",
            )

    @given(flattened_multi_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_penalty_aware_invariant_on_flattened_multi_leg(self, drawn_route):
        """What makes this twin penalty-aware rather than a second copy
        of the pure-fuel proof: at DEFAULT_PENALTY versus Decimal(0) on
        the same flattened multi-leg route, stop count is non-increasing
        and objective is non-decreasing -- the same D-10 shape as
        FixedChargePenaltyConsistencyTests, exercised on flattened
        multi-leg input."""
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, leg_boundaries_mi = drawn_route

        zero_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=Decimal(0),
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )
        penalty_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=DEFAULT_PENALTY,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )

        context = (
            f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
            f"leg_boundaries_mi={leg_boundaries_mi!r}"
        )
        self.assertEqual(
            zero_plan is None,
            penalty_plan is None,
            f"feasibility at penalty={DEFAULT_PENALTY} disagrees with "
            f"the penalty=0 verdict on flattened multi-leg input; {context}",
        )
        if zero_plan is None:
            return
        self.assertLessEqual(
            penalty_plan.stop_count,
            zero_plan.stop_count,
            f"stop_count rose from penalty=0 ({zero_plan.stop_count}) to "
            f"penalty={DEFAULT_PENALTY} ({penalty_plan.stop_count}) on "
            f"flattened multi-leg input; {context}",
        )
        self.assertGreaterEqual(
            penalty_plan.objective,
            zero_plan.objective,
            f"objective fell from penalty=0 ({zero_plan.objective}) to "
            f"penalty={DEFAULT_PENALTY} ({penalty_plan.objective}) on "
            f"flattened multi-leg input; {context}",
        )

    @given(flattened_multi_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_dp_matches_oracle_across_penalty_ladder_on_flattened_multi_leg(self, drawn_route):
        """PROOF-04/D-38: the DP is leg-agnostic -- it only ever receives
        ONE flattened candidate list on one continuous distance scale, the
        same shape single_leg_routes() already produces; it never sees a
        leg boundary. So this property proves the flattening contract
        still holds under the new fixed-charge objective, NOT new solver
        logic -- DpOracleDifferentialTests above already proves the DP
        correct on single-leg input; this method reuses the identical
        comparison shape on flattened multi-leg input instead of
        re-deriving it.

        Arguing leg-agnosticism without a test arm was considered and
        rejected: PROOF-04 asks for coverage, and "the code cannot tell
        the difference" is not the same as a test confirming it.

        Same D-10/D-13 shape as DpOracleDifferentialTests: the penalised
        objective is asserted unconditionally at every rung of
        PENALTY_LADDER, and the station set only when the oracle's
        optimum at that rung is strictly unique.
        """
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, leg_boundaries_mi = drawn_route

        self.assertGreaterEqual(
            len(leg_boundaries_mi),
            3,
            f"expected at least two leg boundaries beyond the start "
            f"(>=3 boundary points for >=2 legs), got "
            f"{leg_boundaries_mi!r} -- a strategy regression may have "
            f"collapsed to one leg",
        )
        self.assertEqual(
            leg_boundaries_mi[-1],
            total_route_mi,
            f"flattened total_route_mi={total_route_mi} does not equal "
            f"the sum of leg lengths (final boundary "
            f"{leg_boundaries_mi[-1]}); leg_boundaries_mi={leg_boundaries_mi!r}",
        )
        self.assertLessEqual(
            len(candidates),
            MAX_STATIONS,
            f"flattened candidate count {len(candidates)} exceeds "
            f"MAX_STATIONS={MAX_STATIONS}; candidates={candidates!r}",
        )

        for penalty in PENALTY_LADDER:
            oracle_plan = optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=penalty,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )

            try:
                preflight_gap_check(
                    candidates,
                    total_route_mi=total_route_mi,
                    tank_range_mi=tank_range_mi,
                    starting_fuel=starting_fuel,
                )
                dp_feasible = True
            except InfeasibleRouteError:
                dp_feasible = False

            context = (
                f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
                f"leg_boundaries_mi={leg_boundaries_mi!r}, "
                f"tank_range_mi={tank_range_mi}, mpg={mpg}, "
                f"starting_fuel={starting_fuel}, penalty={penalty}"
            )

            self.assertEqual(
                oracle_plan is not None,
                dp_feasible,
                f"feasibility verdicts disagree on flattened multi-leg "
                f"input: oracle_feasible={oracle_plan is not None}, "
                f"dp_feasible={dp_feasible}; {context}",
            )
            if oracle_plan is None:
                continue

            dp_plan = solve_fixed_charge(
                candidates,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=penalty,
            )

            self.assertLessEqual(
                abs(dp_plan.penalised_objective - oracle_plan.objective),
                COST_TOLERANCE,
                f"dp penalised_objective ({dp_plan.penalised_objective}) "
                f"differs from oracle objective ({oracle_plan.objective}) "
                f"beyond COST_TOLERANCE on flattened multi-leg input; {context}",
            )

            if oracle_plan.is_unique_optimum:
                dp_stop_opis_ids = tuple(stop.opis_id for stop in dp_plan.stops)
                self.assertEqual(
                    dp_stop_opis_ids,
                    oracle_plan.stop_opis_ids,
                    f"station set differs though the oracle optimum is "
                    f"strictly unique on flattened multi-leg input; "
                    f"dp_stop_opis_ids={dp_stop_opis_ids!r}; {context}",
                )
                self.assertLessEqual(
                    abs(dp_plan.total_cost - oracle_plan.fuel_cost),
                    COST_TOLERANCE,
                    f"dp total_cost ({dp_plan.total_cost}) differs from "
                    f"oracle fuel_cost ({oracle_plan.fuel_cost}) beyond "
                    f"COST_TOLERANCE on flattened multi-leg input though "
                    f"the oracle optimum is strictly unique; {context}",
                )


class TrustMarginAnchorTests(SimpleTestCase):
    """PROV-03/D-13/D-14: hand-built, fixed-provenance witnesses proving
    the trust margin actually changes the oracle's answer -- the
    anti-vacuity guard for this whole plan (per the plan's own action
    text). An oracle that accepted a trust_margin argument and silently
    ignored it would satisfy every margin-zero property elsewhere in this
    module; only these two witnesses can catch that.

    Both witnesses were verified against optimal_fixed_charge_plan()
    directly before being transcribed here (never the reverse -- the
    numbers below are not hand-derived and then reconciled with a buggy
    implementation).
    """

    def test_swap_witness_cheap_estimate_beats_dearer_real_only_below_margin(self):
        """The swap witness (D-13's first named behaviour): a cheap
        estimate-priced station and a slightly dearer real-priced station,
        both reachable at the identical position (distance_from_start_mi
        =50) on a route that needs exactly one stop (tank_range_mi=60 on
        a total_route_mi=100 route). At trust_margin=0 the oracle buys the
        cheaper, estimate-priced station; at every rung of MARGIN_LADDER
        (all of which comfortably exceed the $0.40 fuel-cost gap over the
        4-gallon fill this witness purchases), the oracle SWAPS to the
        dearer, real-priced station instead -- same stop_count (1), a
        different station.
        """
        cheap_estimate = Candidate(
            name="cheap-estimate",
            opis_id=1,
            price_per_gallon=Decimal("3.00"),
            distance_from_start_mi=Decimal("50"),
            price_source=ESTIMATE_PRICE_SOURCE,
        )
        dearer_real = Candidate(
            name="dearer-real",
            opis_id=2,
            price_per_gallon=Decimal("3.10"),
            distance_from_start_mi=Decimal("50"),
            price_source=_RECORDED_PRICE_SOURCE,
        )
        candidates = [cheap_estimate, dearer_real]
        common_kwargs = dict(
            total_route_mi=Decimal(100),
            penalty=DEFAULT_PENALTY,
            tank_range_mi=Decimal(60),
            mpg=Decimal(10),
            starting_fuel=Decimal(1),
        )

        zero_margin_plan = optimal_fixed_charge_plan(candidates, trust_margin=Decimal(0), **common_kwargs)
        self.assertEqual(
            zero_margin_plan.stop_opis_ids,
            (cheap_estimate.opis_id,),
            f"at trust_margin=0 the oracle must buy the cheaper, "
            f"estimate-priced station; got {zero_margin_plan!r}",
        )
        self.assertEqual(zero_margin_plan.objective, Decimal("47.00"))
        self.assertEqual(zero_margin_plan.fuel_cost, Decimal("12.00"))

        for margin in MARGIN_LADDER:
            swapped_plan = optimal_fixed_charge_plan(candidates, trust_margin=margin, **common_kwargs)
            self.assertEqual(
                swapped_plan.stop_opis_ids,
                (dearer_real.opis_id,),
                f"at trust_margin={margin} (>= MARGIN_LADDER's smallest "
                f"rung) the oracle must swap to the dearer, real-priced "
                f"station; got {swapped_plan!r}",
            )
            self.assertEqual(swapped_plan.objective, Decimal("47.40"))
            self.assertEqual(swapped_plan.fuel_cost, Decimal("12.40"))

    def test_drop_witness_extra_estimate_stop_dropped_not_swapped_at_high_margin(self):
        """The drop witness (D-13's second named behaviour): a mandatory
        real-priced station R (distance_from_start_mi=10, the only
        reachable station from a near-empty starting tank) plus an
        optional, much cheaper estimate-priced station T further along
        (distance_from_start_mi=100) that -- at low margin -- is worth a
        second stop to reach, and -- at high margin -- is not. Unlike the
        swap witness, there is no substitute real-priced station at T's
        position: the oracle must DROP T entirely and buy more at R
        instead, proving the margin composes with the fixed per-stop
        penalty rather than replacing it (buying more at R still pays R's
        own fuel price and a single penalty charge; it never becomes
        free just because T got expensive).
        """
        station_r = Candidate(
            name="R",
            opis_id=10,
            price_per_gallon=Decimal("4.00"),
            distance_from_start_mi=Decimal("10"),
            price_source=_RECORDED_PRICE_SOURCE,
        )
        station_t = Candidate(
            name="T",
            opis_id=11,
            price_per_gallon=Decimal("1.00"),
            distance_from_start_mi=Decimal("100"),
            price_source=ESTIMATE_PRICE_SOURCE,
        )
        candidates = [station_r, station_t]
        common_kwargs = dict(
            total_route_mi=Decimal(150),
            penalty=Decimal("5"),
            tank_range_mi=Decimal(150),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.10"),
        )

        retained_expectations = {
            Decimal(0): Decimal("49.0000"),
            MARGIN_LADDER[0]: Decimal("54.4700"),
        }
        for margin, expected_objective in retained_expectations.items():
            plan = optimal_fixed_charge_plan(candidates, trust_margin=margin, **common_kwargs)
            self.assertEqual(
                plan.stop_opis_ids,
                (station_r.opis_id, station_t.opis_id),
                f"at trust_margin={margin} the extra estimate-priced stop "
                f"T must still be worth taking (2 stops); got {plan!r}",
            )
            self.assertEqual(plan.objective, expected_objective)
            self.assertEqual(plan.fuel_cost, Decimal("39.0000"))

        for margin in (MARGIN_LADDER[1], MARGIN_LADDER[2]):
            plan = optimal_fixed_charge_plan(candidates, trust_margin=margin, **common_kwargs)
            self.assertEqual(
                plan.stop_opis_ids,
                (station_r.opis_id,),
                f"at trust_margin={margin} station T must be DROPPED "
                f"entirely (1 stop, R only, buying more fuel at R rather "
                f"than swapping to any substitute) -- got {plan!r}",
            )
            self.assertEqual(plan.objective, Decimal("59.0000"))
            self.assertEqual(plan.fuel_cost, Decimal("54.0000"))


class TrustMarginPenaltyConsistencyTests(SimpleTestCase):
    """D-13's full PENALTY_LADDER x MARGIN_LADDER cross product (Task 2's
    measured verdict -- the three pre-extension wall-clock figures, the
    ~3x projection, and the post-extension figure are all recorded in
    21-04-SUMMARY.md, beside the module's own ~30s ceiling).

    Unlike FixedChargePenaltyConsistencyTests' penalty-only properties,
    stop_count and fuel_cost are deliberately NOT asserted monotonic in
    trust_margin here: margin is added only to candidates for which
    is_estimate_priced() is True, not uniformly to every stop the way
    penalty is, so a revealed-preference argument only yields monotonicity
    of the TAGGED stop count in the optimum -- never of the total stop
    count or of fuel_cost -- as margin rises.

    Proof sketch (revealed preference), for a fixed penalty and m1 < m2
    with optimal subsets S1 (at margin=m1) and S2 (at margin=m2):
        f(S1, m1) <= f(S2, m1)   [S1 optimal at m1]
        f(S2, m2) <= f(S1, m2)   [S2 optimal at m2]
    where f(S, m) = fuel(S) + penalty*|S| + m*tagged(S). Summing both
    inequalities and cancelling the margin-free fuel(S)+penalty*|S| terms
    on both sides gives (m1-m2)*tagged(S1) <= (m1-m2)*tagged(S2); since
    m1-m2 < 0, dividing flips the inequality to tagged(S1) >= tagged(S2)
    -- the tagged-station count of the optimum is non-increasing as
    margin rises. This proof does NOT establish |S1| >= |S2| or
    fuel(S1) <= fuel(S2) -- those would be the stronger, UNPROVEN claims
    this class deliberately does not assert. (The same pointwise-min-of-
    non-decreasing-functions argument used for penalty DOES generalize
    to margin for the objective itself: each subset's own f(S, .) is
    non-decreasing in margin, so their minimum is too.)

    max_examples=50 mirrors FixedChargePenaltyConsistencyTests' own
    discretionary formulation choice -- each example now performs
    len(PENALTY_LADDER) * len(_MARGIN_SWEEP) = 12 oracle solves (the
    Task-2-adopted full cross product), a real but bounded multiple of
    that class's own 3-solves-per-example cost.
    """

    def _plans_by_penalty_and_margin(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        return {
            (penalty, margin): optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=penalty,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                trust_margin=margin,
            )
            for penalty in PENALTY_LADDER
            for margin in _MARGIN_SWEEP
        }

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_feasibility_is_margin_invariant(self, drawn_route):
        """Margin can only change WHICH feasible subset wins, never
        WHETHER any subset is feasible: margin is added to a subset's
        cost only after _cheapest_fuel_cost_for_subset has already
        determined that subset is feasible, independent of margin."""
        plans = self._plans_by_penalty_and_margin(drawn_route)
        for penalty in PENALTY_LADDER:
            zero_feasible = plans[(penalty, Decimal(0))] is not None
            for margin in _MARGIN_SWEEP:
                self.assertEqual(
                    plans[(penalty, margin)] is not None,
                    zero_feasible,
                    f"feasibility at penalty={penalty}, margin={margin} "
                    f"disagrees with the margin=0 verdict={zero_feasible} "
                    f"at the same penalty; drawn_route={drawn_route!r}",
                )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_objective_at_least_margin_zero_objective(self, drawn_route):
        plans = self._plans_by_penalty_and_margin(drawn_route)
        for penalty in PENALTY_LADDER:
            zero_plan = plans[(penalty, Decimal(0))]
            if zero_plan is None:
                continue
            for margin in _MARGIN_SWEEP:
                self.assertGreaterEqual(
                    plans[(penalty, margin)].objective,
                    zero_plan.objective,
                    f"objective at penalty={penalty}, margin={margin} "
                    f"({plans[(penalty, margin)].objective}) fell below "
                    f"the margin=0 objective ({zero_plan.objective}) at "
                    f"the same penalty; drawn_route={drawn_route!r}",
                )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_objective_non_decreasing_across_margin_ladder(self, drawn_route):
        plans = self._plans_by_penalty_and_margin(drawn_route)
        for penalty in PENALTY_LADDER:
            if plans[(penalty, Decimal(0))] is None:
                continue
            for m_lo, m_hi in zip(_MARGIN_SWEEP, _MARGIN_SWEEP[1:]):
                self.assertGreaterEqual(
                    plans[(penalty, m_hi)].objective,
                    plans[(penalty, m_lo)].objective,
                    f"objective fell from margin={m_lo} "
                    f"({plans[(penalty, m_lo)].objective}) to margin="
                    f"{m_hi} ({plans[(penalty, m_hi)].objective}) at "
                    f"penalty={penalty}; drawn_route={drawn_route!r}",
                )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_tagged_count_non_increasing_as_margin_rises(self, drawn_route):
        """The class's own revealed-preference proof (see docstring
        above), the correct margin analog of
        FixedChargePenaltyConsistencyTests' stop-count property -- NOT
        total stop_count, which is unproven for margin."""
        candidates = drawn_route[0]
        candidate_by_id = {c.opis_id: c for c in candidates}
        plans = self._plans_by_penalty_and_margin(drawn_route)
        for penalty in PENALTY_LADDER:
            if plans[(penalty, Decimal(0))] is None:
                continue
            for m_lo, m_hi in zip(_MARGIN_SWEEP, _MARGIN_SWEEP[1:]):
                plan_lo = plans[(penalty, m_lo)]
                plan_hi = plans[(penalty, m_hi)]
                tagged_lo = sum(
                    1
                    for opis_id in plan_lo.stop_opis_ids
                    if is_estimate_priced(candidate_by_id[opis_id])
                )
                tagged_hi = sum(
                    1
                    for opis_id in plan_hi.stop_opis_ids
                    if is_estimate_priced(candidate_by_id[opis_id])
                )
                self.assertLessEqual(
                    tagged_hi,
                    tagged_lo,
                    f"tagged-station count rose from {tagged_lo} at "
                    f"margin={m_lo} to {tagged_hi} at margin={m_hi}, "
                    f"penalty={penalty} -- see this class's own "
                    f"revealed-preference proof; drawn_route={drawn_route!r}",
                )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_plan_internal_consistency_across_margin_ladder(self, drawn_route):
        candidates = drawn_route[0]
        candidate_by_id = {c.opis_id: c for c in candidates}
        plans = self._plans_by_penalty_and_margin(drawn_route)
        for penalty in PENALTY_LADDER:
            if plans[(penalty, Decimal(0))] is None:
                continue
            for margin in _MARGIN_SWEEP:
                plan = plans[(penalty, margin)]
                margin_total = sum(
                    (trust_margin_for(candidate_by_id[opis_id], margin) for opis_id in plan.stop_opis_ids),
                    Decimal(0),
                )
                self.assertEqual(
                    plan.objective,
                    plan.fuel_cost + penalty * plan.stop_count + margin_total,
                    f"objective does not equal fuel_cost + "
                    f"penalty*stop_count + margin_total at penalty="
                    f"{penalty}, margin={margin}; plan={plan!r}; "
                    f"drawn_route={drawn_route!r}",
                )


class TrustMarginOracleDifferentialTests(SimpleTestCase):
    """PROV-03/D-15/D-24, ROADMAP criterion 1 (twice-amended): the DP arm,
    finally wired for the trust margin (plan 21-06 Task 1) against THIS
    module's own independent subset-enumeration oracle -- mirroring
    DpOracleDifferentialTests' shape exactly, extended one dimension:
    every drawn route is checked at every `(penalty, trust_margin)` pair
    in the full `PENALTY_LADDER x _MARGIN_SWEEP` cross product, the
    IDENTICAL sweep `TrustMarginPenaltyConsistencyTests` already runs
    against the oracle alone (Task 2's adopted rung shape).

    Objective agreement is asserted unconditionally; station-set and
    `total_cost` agreement are asserted only when the oracle's optimum is
    strictly unique (`OraclePlan.is_unique_optimum`) -- plan 21-05's own
    closing note recorded why this matters here specifically: condition
    3's prune rule retains a strict superset on a mixed-provenance corpus
    even at `trust_margin=0`, which is sound (never changes the optimal
    objective) but can hand the deterministic D-12 tie-break a different
    winner among several equal-objective plans. Unconditional objective
    plus conditional station-set is Phase 17's own D-36 resolution,
    applied here rather than invented.

    **Criterion 1's re-scoping (D-24).** The criterion's literal wording
    ("`price_per_gallon`, `total_cost` and `savings` are identical to
    what the build returns with the margin set to zero") is unsatisfiable
    exactly where the margin does its job: on a tagged sweep the margin
    legitimately changes which station is bought, so `total_cost`
    legitimately differs from the `trust_margin=0` run. The universal
    anti-leak property is asserted instead, on EVERY sweep including ones
    where the margin fires: every `FuelStop.price_per_gallon` equals its
    source candidate's raw price, and `total_cost` equals
    `sum(gallons * raw price)` to within `COST_TOLERANCE`. This is
    STRICTLY STRONGER than the literal reading and true everywhere. The
    `trust_margin=0` byte-identity claim is kept too, but re-scoped to an
    ALL-`opis_indexed` (all-recorded) candidate set -- the only scope in
    which byte identity to the margin-zero build is actually a true
    claim -- in
    `test_zero_margin_byte_identity_on_all_recorded_candidates` below.
    See `.planning/ROADMAP.md` Phase 21 criterion 1's dated amendment.

    **Anti-vacuity (D-24's own instruction).** A no-leak test that only
    ever observes inert runs (margin never actually fired) proves
    nothing -- `prune(x) -> x` in a different costume. `tearDownClass`
    below fails the run if not a single example, across the whole
    Hypothesis property, had a NONZERO margin (the only variable that
    differs between it and the trust_margin=0 run over the SAME
    candidates and penalty) produce a DIFFERENT DP stop set -- the
    observable, causal definition of the margin having changed the
    decision. `_MARGIN_SWAP_WITNESS_ROUTE`, pinned via `@example` below,
    backstops this deterministically: a full swap away from every
    estimate-priced candidate produces a winning set with a zero SUMMED
    margin of its own (nothing estimate-priced survived to be charged),
    which is exactly why "the winning set's own margin is nonzero" was
    considered and rejected as this condition's definition -- it would
    silently exclude the swap witness's own case, D-24's central named
    behaviour. The observed count is recorded in 21-06-SUMMARY.md.
    """

    _margin_fired_and_differed_count = 0

    # The anti-vacuity backstop: a hand-built witness, reusing
    # TrustMarginAnchorTests' own swap-witness shape verbatim (two
    # co-located stations, one cheap and estimate-priced, one slightly
    # dearer and real-priced, on a route needing exactly one stop),
    # pinned via @example below so the anti-vacuity condition fires
    # deterministically regardless of what the random Hypothesis draws
    # happen to produce. A margin rung comfortably exceeds the $0.40
    # fuel-cost gap over the 4-gallon fill this witness purchases at
    # EVERY penalty in PENALTY_LADDER (the penalty term is identical for
    # either single-stop choice, so it cancels out of the swap decision),
    # so this witness is guaranteed to both fire the margin and swap the
    # DP's own chosen stop set relative to its trust_margin=0 run.
    _MARGIN_SWAP_WITNESS_ROUTE = (
        [
            Candidate(
                name="cheap-estimate",
                opis_id=1,
                price_per_gallon=Decimal("3.00"),
                distance_from_start_mi=Decimal("50"),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
            Candidate(
                name="dearer-real",
                opis_id=2,
                price_per_gallon=Decimal("3.10"),
                distance_from_start_mi=Decimal("50"),
                price_source=_RECORDED_PRICE_SOURCE,
            ),
        ],
        Decimal(100),
        Decimal(60),
        Decimal(10),
        Decimal(1),
    )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if cls._margin_fired_and_differed_count <= 0:
            raise AssertionError(
                "anti-vacuity failure: zero examples were observed across "
                "the whole Hypothesis run where the trust margin actually "
                "fired (a nonzero applied margin on the DP's winning stop "
                "set) AND changed the chosen stop set relative to the "
                "trust_margin=0 run at the same penalty -- the no-leak "
                "assertions in this class would be exercised only on "
                "inert runs, proving nothing (D-24's own named failure "
                "mode: prune(x) -> x in a different costume)."
            )

    @example(drawn_route=_MARGIN_SWAP_WITNESS_ROUTE)
    @given(single_leg_routes())
    @settings(deadline=None, max_examples=200)
    def test_dp_matches_oracle_across_penalty_and_margin_ladder(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        candidate_by_id = {c.opis_id: c for c in candidates}

        try:
            preflight_gap_check(
                candidates,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                starting_fuel=starting_fuel,
            )
            dp_feasible = True
        except InfeasibleRouteError:
            dp_feasible = False

        zero_margin_stop_ids_by_penalty = {}

        for penalty in PENALTY_LADDER:
            for margin in _MARGIN_SWEEP:
                oracle_plan = optimal_fixed_charge_plan(
                    candidates,
                    total_route_mi,
                    penalty=penalty,
                    tank_range_mi=tank_range_mi,
                    mpg=mpg,
                    starting_fuel=starting_fuel,
                    trust_margin=margin,
                )

                context = (
                    f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
                    f"tank_range_mi={tank_range_mi}, mpg={mpg}, "
                    f"starting_fuel={starting_fuel}, penalty={penalty}, "
                    f"margin={margin}"
                )

                self.assertEqual(
                    oracle_plan is not None,
                    dp_feasible,
                    f"feasibility verdicts disagree: oracle_feasible="
                    f"{oracle_plan is not None}, dp_feasible={dp_feasible}; "
                    f"{context}",
                )
                if oracle_plan is None:
                    continue

                dp_plan = solve_fixed_charge(
                    candidates,
                    total_route_mi=total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=mpg,
                    starting_fuel=starting_fuel,
                    penalty=penalty,
                    trust_margin=margin,
                )

                # Objective agreement: unconditional (the actual
                # differential claim -- see DpOracleDifferentialTests for
                # the identical shape, one dimension narrower).
                self.assertLessEqual(
                    abs(dp_plan.penalised_objective - oracle_plan.objective),
                    COST_TOLERANCE,
                    f"dp penalised_objective ({dp_plan.penalised_objective}) "
                    f"differs from oracle objective ({oracle_plan.objective}) "
                    f"beyond COST_TOLERANCE; {context}",
                )

                dp_stop_opis_ids = tuple(stop.opis_id for stop in dp_plan.stops)

                if oracle_plan.is_unique_optimum:
                    self.assertEqual(
                        dp_stop_opis_ids,
                        oracle_plan.stop_opis_ids,
                        f"station set differs though the oracle optimum is "
                        f"strictly unique; dp_stop_opis_ids="
                        f"{dp_stop_opis_ids!r}; {context}",
                    )
                    self.assertLessEqual(
                        abs(dp_plan.total_cost - oracle_plan.fuel_cost),
                        COST_TOLERANCE,
                        f"dp total_cost ({dp_plan.total_cost}) differs from "
                        f"oracle fuel_cost ({oracle_plan.fuel_cost}) beyond "
                        f"COST_TOLERANCE though the oracle optimum is "
                        f"strictly unique; {context}",
                    )

                # D-24 no-leak assertions: asserted directly, on EVERY
                # sweep including ones where the margin fires -- this is
                # criterion 1's actual proof, not merely its inference.
                for stop in dp_plan.stops:
                    raw_price = candidate_by_id[stop.opis_id].price_per_gallon
                    self.assertEqual(
                        stop.price_per_gallon,
                        raw_price,
                        f"FuelStop.price_per_gallon ({stop.price_per_gallon}) "
                        f"!= source candidate's raw price ({raw_price}) for "
                        f"opis_id={stop.opis_id}; {context}",
                    )
                reconstructed_total = sum(
                    (
                        stop.gallons * candidate_by_id[stop.opis_id].price_per_gallon
                        for stop in dp_plan.stops
                    ),
                    Decimal(0),
                )
                self.assertLessEqual(
                    abs(dp_plan.total_cost - reconstructed_total),
                    COST_TOLERANCE,
                    f"total_cost ({dp_plan.total_cost}) != sum(gallons * "
                    f"raw price) ({reconstructed_total}); {context}",
                )

                # Anti-vacuity bookkeeping (see tearDownClass above): the
                # margin "actually fired" on this run when a NONZERO
                # margin (the only variable that differs between this run
                # and the trust_margin=0 run at the SAME penalty and
                # candidates) produced a DIFFERENT DP stop set -- the
                # observable, causal definition of "the margin changed the
                # decision", not a proxy for it. (An earlier draft of this
                # check instead required the WINNING set's own summed
                # margin to be positive; that is a stricter, different
                # claim -- a "full swap away from every estimate-priced
                # candidate", like this class's own pinned witness below,
                # legitimately produces a stop set with a zero summed
                # margin despite the margin being exactly what caused the
                # swap -- so it is not used here.)
                if margin == Decimal(0):
                    zero_margin_stop_ids_by_penalty[penalty] = dp_stop_opis_ids
                else:
                    zero_ids = zero_margin_stop_ids_by_penalty.get(penalty)
                    if zero_ids is not None and dp_stop_opis_ids != zero_ids:
                        type(self)._margin_fired_and_differed_count += 1

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=100)
    def test_zero_margin_byte_identity_on_all_recorded_candidates(self, drawn_route):
        """D-24's inertness claim, correctly scoped: on an ALL-`opis_indexed`
        (all-recorded) candidate set -- no `eia_regional_estimate` station
        anywhere in the input -- the plan at ANY `MARGIN_LADDER` rung is
        byte-identical to the SAME input's `trust_margin=0` plan: same
        stop ids, same `total_cost`, same `solver_strategy`. This holds
        because `trust_margin_for(...)` returns `Decimal(0)` for a
        real-priced candidate at any margin value, per its own docstring.

        Run through `solver.solve()` -- not `dp.solve_fixed_charge`
        directly -- specifically so `solver_strategy` is observable at
        all; only `solve()`'s own dispatch sets that field. This is
        deliberately a STRONGER claim than criterion 1's literal wording
        ever needed: it holds at every ladder rung, not merely at the
        current `TRUST_MARGIN_USD` default (which happens to equal
        `trust_margin=0` today, making this test's content genuinely
        exceed what a same-value comparison alone would prove).
        """
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        all_recorded_candidates = [
            replace(c, price_source=_RECORDED_PRICE_SOURCE) for c in candidates
        ]

        try:
            zero_plan = solve(
                all_recorded_candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=DEFAULT_PENALTY,
                trust_margin=Decimal(0),
                deadline=None,
            )
        except InfeasibleRouteError:
            return

        for margin in MARGIN_LADDER:
            margin_plan = solve(
                all_recorded_candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=DEFAULT_PENALTY,
                trust_margin=margin,
                deadline=None,
            )
            context = (
                f"candidates={all_recorded_candidates!r}, "
                f"total_route_mi={total_route_mi}, "
                f"tank_range_mi={tank_range_mi}, mpg={mpg}, "
                f"starting_fuel={starting_fuel}, margin={margin}"
            )
            self.assertEqual(
                tuple(s.opis_id for s in margin_plan.stops),
                tuple(s.opis_id for s in zero_plan.stops),
                f"stop ids differ at margin={margin} on an all-recorded "
                f"candidate set; margin_plan={margin_plan!r}, "
                f"zero_plan={zero_plan!r}; {context}",
            )
            self.assertEqual(
                margin_plan.total_cost,
                zero_plan.total_cost,
                f"total_cost differs at margin={margin} on an all-recorded "
                f"candidate set; {context}",
            )
            self.assertEqual(
                margin_plan.strategy,
                zero_plan.strategy,
                f"solver_strategy differs at margin={margin} on an "
                f"all-recorded candidate set; {context}",
            )


class IdenticalPriceClusterOracleTests(SimpleTestCase):
    """D-12/PIPE-02 (plan 22-04): the DP and this module's own independent
    oracle must agree on the exact price-degeneracy the Overture gap-fill
    import is about to create (D-09: every same-region row shares one
    `retail_price`), across the full `PENALTY_LADDER x _MARGIN_SWEEP`
    cross product -- mirrors TrustMarginOracleDifferentialTests' shape
    exactly, drawing from identical_price_cluster_routes() instead of
    single_leg_routes().

    Objective agreement is asserted unconditionally, exactly like
    TrustMarginOracleDifferentialTests. Station-set agreement is asserted
    only when the oracle's optimum is strictly unique
    (`OraclePlan.is_unique_optimum`) -- inside a genuine price cluster,
    NON-uniqueness is the expected case (several equal-priced station sets
    tie for the winning objective), so this class tracks how many examples
    fell into each branch and fails outright, in tearDownClass, if not one
    single generated example actually contained a cluster of at least 3
    identically-priced reachable candidates -- the anti-vacuity guard this
    plan's own must_haves require, in the same spirit as
    TrustMarginOracleDifferentialTests' own margin-fired-and-differed
    counter: a strategy that degenerated into single-price routes would
    otherwise let this whole class pass while testing nothing.
    """

    _cluster_case_count = 0
    _unique_optimum_count = 0
    _non_unique_optimum_count = 0

    # The pinned D-19 tie-break witness this plan's own action text calls
    # for: a real-priced and an estimate-priced station carrying the SAME
    # raw price, alongside a genuine identically-priced cluster -- pinned
    # via @example (mirroring _MARGIN_SWAP_WITNESS_ROUTE's own pattern
    # above) so this exact shape fires deterministically regardless of
    # what the random Hypothesis draws happen to produce.
    _TIED_PRICE_MIXED_PROVENANCE_WITNESS = (
        [
            Candidate(
                name="Cluster0",
                opis_id=10,
                price_per_gallon=Decimal("3.50"),
                distance_from_start_mi=Decimal("100"),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
            Candidate(
                name="Cluster1",
                opis_id=11,
                price_per_gallon=Decimal("3.50"),
                distance_from_start_mi=Decimal("105"),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
            Candidate(
                name="Cluster2",
                opis_id=12,
                price_per_gallon=Decimal("3.50"),
                distance_from_start_mi=Decimal("110"),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
            Candidate(
                name="Cluster3",
                opis_id=13,
                price_per_gallon=Decimal("3.50"),
                distance_from_start_mi=Decimal("115"),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
            Candidate(
                name="TiedReal",
                opis_id=14,
                price_per_gallon=Decimal("3.00"),
                distance_from_start_mi=Decimal("300"),
                price_source=_RECORDED_PRICE_SOURCE,
            ),
            Candidate(
                name="TiedEstimate",
                opis_id=15,
                price_per_gallon=Decimal("3.00"),
                distance_from_start_mi=Decimal("305"),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
        ],
        Decimal(500),
        Decimal(300),
        Decimal(10),
        Decimal("1.00"),
    )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if cls._cluster_case_count <= 0:
            raise AssertionError(
                "anti-vacuity failure: zero generated examples contained a "
                "cluster of at least 3 identically-priced reachable "
                "candidates -- identical_price_cluster_routes() may have "
                "degenerated into single-price routes, which would let "
                "this whole class pass while testing nothing. "
                f"unique_optimum_count={cls._unique_optimum_count}, "
                f"non_unique_optimum_count={cls._non_unique_optimum_count}"
            )

    @example(drawn_route=_TIED_PRICE_MIXED_PROVENANCE_WITNESS)
    @given(identical_price_cluster_routes())
    @settings(deadline=None, max_examples=200)
    def test_dp_matches_oracle_on_identical_price_clusters(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        price_counts = {}
        for candidate in candidates:
            price_counts[candidate.price_per_gallon] = price_counts.get(candidate.price_per_gallon, 0) + 1
        if price_counts and max(price_counts.values()) >= 3:
            type(self)._cluster_case_count += 1

        try:
            preflight_gap_check(
                candidates,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                starting_fuel=starting_fuel,
            )
            dp_feasible = True
        except InfeasibleRouteError:
            dp_feasible = False

        for penalty in PENALTY_LADDER:
            for margin in _MARGIN_SWEEP:
                oracle_plan = optimal_fixed_charge_plan(
                    candidates,
                    total_route_mi,
                    penalty=penalty,
                    tank_range_mi=tank_range_mi,
                    mpg=mpg,
                    starting_fuel=starting_fuel,
                    trust_margin=margin,
                )

                context = (
                    f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
                    f"tank_range_mi={tank_range_mi}, mpg={mpg}, "
                    f"starting_fuel={starting_fuel}, penalty={penalty}, "
                    f"margin={margin}"
                )

                self.assertEqual(
                    oracle_plan is not None,
                    dp_feasible,
                    f"feasibility verdicts disagree: oracle_feasible="
                    f"{oracle_plan is not None}, dp_feasible={dp_feasible}; "
                    f"{context}",
                )
                if oracle_plan is None:
                    continue

                dp_plan = solve_fixed_charge(
                    candidates,
                    total_route_mi=total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=mpg,
                    starting_fuel=starting_fuel,
                    penalty=penalty,
                    trust_margin=margin,
                )

                self.assertLessEqual(
                    abs(dp_plan.penalised_objective - oracle_plan.objective),
                    COST_TOLERANCE,
                    f"dp penalised_objective ({dp_plan.penalised_objective}) "
                    f"differs from oracle objective ({oracle_plan.objective}) "
                    f"beyond COST_TOLERANCE; {context}",
                )

                if oracle_plan.is_unique_optimum:
                    type(self)._unique_optimum_count += 1
                    dp_stop_opis_ids = tuple(stop.opis_id for stop in dp_plan.stops)
                    self.assertEqual(
                        dp_stop_opis_ids,
                        oracle_plan.stop_opis_ids,
                        f"station set differs though the oracle optimum is "
                        f"strictly unique; dp_stop_opis_ids="
                        f"{dp_stop_opis_ids!r}; {context}",
                    )
                else:
                    type(self)._non_unique_optimum_count += 1


class PenaltyDisagreementFloorTests(SimpleTestCase):
    """D-13/D-15/D-16: a plain SimpleTestCase (deliberately not a
    Hypothesis property-based test) so it runs identically -- same corpus,
    same result -- on every commit, asserting only the loose > 20% floor from
    DISAGREEMENT_FLOOR. This class is the CI-enforcing half of the D-16
    pair; `measure_penalty_disagreement` (a management command, not run in
    CI) reports the precise headline figure that this floor merely
    guards.

    The exact measured rate at the sourced $35 penalty, over the
    management command's larger corpus, is recorded in the module
    docstring above and in 16-03-SUMMARY.md -- not here. This floor is
    deliberately loose: wide enough that ordinary distribution drift can
    never flake it, tight enough that a silently no-op penalty path
    (which would score exactly 0% disagreement) fails it instantly.
    """

    def test_disagreement_rate_exceeds_floor(self):
        report = measure_disagreement(GUARD_ROUTES)
        self.assertGreater(
            report.rate,
            DISAGREEMENT_FLOOR,
            f"measured disagreement rate {report.rate} over "
            f"GUARD_ROUTES={GUARD_ROUTES} routes (seed={CORPUS_PARAMS.seed}, "
            f"penalty={report.penalty}) did not exceed the "
            f"DISAGREEMENT_FLOOR={DISAGREEMENT_FLOOR}. This floor sits at "
            f"roughly a third of the expected ~55% rate -- wide enough that "
            f"distribution drift alone should never trip it. If it trips, "
            f"first check whether optimal_fixed_charge_plan's penalty "
            f"argument is silently being ignored (a no-op penalty path "
            f"scores exactly 0% here) -- do not adjust GUARD_ROUTES, the "
            f"seed, or any CORPUS_PARAMS field to make this pass (D-17).",
        )

    def test_disagreement_measurement_is_deterministic_and_prefix_stable(self):
        """Two independent calls to measure_disagreement(GUARD_ROUTES)
        must return identical n_disagree/rate (determinism), and that
        result must be unchanged even after build_corpus has since been
        called for a larger n_routes (the seeded-prefix property: a larger
        corpus's first GUARD_ROUTES routes are byte-identical to the
        guard's own GUARD_ROUTES routes, so nothing about a bigger
        corpus can retroactively change what the guard measured)."""
        first = measure_disagreement(GUARD_ROUTES)
        second = measure_disagreement(GUARD_ROUTES)
        self.assertEqual(
            first.n_disagree,
            second.n_disagree,
            "measure_disagreement(GUARD_ROUTES) returned a different "
            "n_disagree across two consecutive calls -- the corpus RNG "
            "must be freshly seeded from CORPUS_PARAMS.seed on every call.",
        )
        self.assertEqual(
            first.rate,
            second.rate,
            "measure_disagreement(GUARD_ROUTES) returned a different rate "
            "across two consecutive calls.",
        )

        # Build a corpus larger than the guard's own, then re-measure the
        # guard: the earlier result must be unaffected.
        build_corpus(GUARD_ROUTES + 50)
        third = measure_disagreement(GUARD_ROUTES)
        self.assertEqual(
            third.n_disagree,
            first.n_disagree,
            "measure_disagreement(GUARD_ROUTES)'s n_disagree changed after "
            "build_corpus was called for a larger n_routes -- the corpus "
            "RNG is freshly seeded per call, so this should be impossible "
            "unless the guard's routes are not a true prefix of the larger "
            "corpus.",
        )

        guard_corpus = build_corpus(GUARD_ROUTES)
        larger_corpus = build_corpus(GUARD_ROUTES + 50)
        for i, ((guard_candidates, guard_total), (larger_candidates, larger_total)) in enumerate(
            zip(guard_corpus, larger_corpus[:GUARD_ROUTES])
        ):
            self.assertEqual(
                guard_total,
                larger_total,
                f"route {i}: total_route_mi differs between build_corpus("
                f"{GUARD_ROUTES}) and the first {GUARD_ROUTES} routes of "
                f"build_corpus({GUARD_ROUTES + 50}) -- the corpus is not "
                f"prefix-stable.",
            )
            guard_shape = [
                (c.opis_id, c.price_per_gallon, c.distance_from_start_mi) for c in guard_candidates
            ]
            larger_shape = [
                (c.opis_id, c.price_per_gallon, c.distance_from_start_mi) for c in larger_candidates
            ]
            self.assertEqual(
                guard_shape,
                larger_shape,
                f"route {i}: candidates differ (opis_id, price_per_gallon, "
                f"distance_from_start_mi) between build_corpus({GUARD_ROUTES}) "
                f"and the first {GUARD_ROUTES} routes of "
                f"build_corpus({GUARD_ROUTES + 50}) -- the corpus is not "
                f"prefix-stable.",
            )

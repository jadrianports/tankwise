"""Independent brute-force oracle for the fuel + penalty*stops objective.

The optimum this module computes is: the plan that minimizes fuel dollars
plus penalty times the count of stations where gallons purchased is greater
than zero, for a given route.

The optimum is found by enumerating every subset of candidate stations and
costing each one fully, in a plain itertools.combinations loop -- there is
no memo table, no state-keyed dictionary, and no (node, fuel) recurrence
anywhere in this module. A reviewer checking this module's independence
from the DP it will later judge can do so by reading that loop.

The only symbols this module imports from the routing.services package are
Candidate and solve, plus InfeasibleRouteError from
routing.services.exceptions, needed only to turn an infeasible route into a
boolean feasibility verdict for the penalty=0 anchor property below. Every
other solver helper -- the plan and stop dataclasses, the purchase-reason
constants, and the solver's own private helpers -- is deliberately not
imported here.

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
"""
import random
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import chain, combinations

from django.test import SimpleTestCase
from hypothesis import given, settings
from hypothesis import strategies as st

from routing.services import Candidate, solve
from routing.services.exceptions import InfeasibleRouteError

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
    """The oracle's answer for one route at one penalty.

    objective is the equality target for comparisons against another
    solver's result. fuel_cost is the fuel-dollars-only figure Phase 18's
    INTG-02 check needs (total_cost must report fuel dollars only, never
    the penalised total). stop_opis_ids is the station set Phase 17's
    prune property will compare against. gallons makes the D-04
    nonzero-purchase rule inspectable -- every entry is strictly positive
    by construction.

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
) -> "OraclePlan | None":
    """Return the OraclePlan minimizing fuel dollars plus penalty times
    stop count, over every subset of candidates, or None if no subset
    yields a feasible plan.

    penalty is a required keyword-only plain function argument -- this
    oracle never reads a Django setting; that seam belongs to Phase 18's
    INTG-01. Per D-06, infeasibility falls purely out of the enumeration:
    there is no pre-flight gap check anywhere in this module, so this
    oracle and a DP's own gap check can never agree merely because they
    share a reachability rule.

    Ties are broken by an explicit total order (D-08): lowest objective,
    then fewest stops, then the sorted tuple of distance_from_start_mi
    (already the natural order of a subset from _subsets_by_distance),
    then the tuple of opis_id. Deterministic across Hypothesis seeds and
    repeat runs -- there is no last-writer-wins path.

    Uniqueness (Phase 17): alongside best_plan/best_key, one
    (objective, stop_opis_ids) entry is accumulated per feasible subset --
    at MAX_STATIONS=6 that is at most 64 entries, so the memory cost is
    nil. After the loop, with the winning objective known, the distinct
    stop_opis_ids values whose objective is within COST_TOLERANCE of it
    are counted; OraclePlan.is_unique_optimum is set to whether that count
    is exactly one. COST_TOLERANCE -- never exact equality -- is used for
    the same reason it is used everywhere else in this module: both sides
    accumulate purchases in a different order and Decimal division at the
    default context is inexact, so exact equality would report spurious
    non-uniqueness for what is really the same tied optimum.
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
        objective = fuel_cost + penalty * len(subset)
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
    """Build Candidate objects from drawn (price, distance) tuples,
    dropping any tuple whose distance exceeds total_route_mi. Dropping --
    not raising -- keeps generated inputs inside solve()'s own contract:
    an out-of-range candidate would trigger InvalidRouteInputError, which
    is input validation on the caller's contract, not the optimality
    property under test here.
    """
    candidates = []
    for i, (price, dist) in enumerate(station_tuples):
        dist_mi = Decimal(dist)
        if dist_mi <= total_route_mi:
            candidates.append(
                Candidate(
                    name=f"S{i}",
                    opis_id=i,
                    price_per_gallon=price,
                    distance_from_start_mi=dist_mi,
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
            greedy_plan = solve(
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
    """
    total_route_mi = Decimal(draw(st.integers(min_value=1, max_value=800)))
    station_tuples = draw(
        st.lists(
            st.tuples(
                st.decimals(min_value=Decimal("1.00"), max_value=Decimal("6.00"), places=2),
                st.integers(min_value=1, max_value=800),
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
                ),
                max_size=per_leg_cap,
                unique_by=lambda t: t[1],
            )
        )
        for price, local_dist_mi in local_tuples:
            flattened_tuples.append((price, offset_mi + Decimal(local_dist_mi)))
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


class FixedChargeOracleAnchorTests(SimpleTestCase):
    """The D-09 / ROADMAP-criterion-1 anchor: at penalty=0, this oracle
    must agree with the shipped greedy solve() on feasibility and on fuel
    cost, across the full fixed 200-example Hypothesis run.
    """

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=200)
    def test_penalty_zero_anchors_to_shipped_greedy(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        try:
            greedy_plan = solve(
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
            greedy_plan = solve(
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

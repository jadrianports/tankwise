"""Domination prune soundness claims -- what the prune keeps.

Deliberately a separate module from test_solver_fixed_charge_optimality.py
because it proves a different claim: that module proves the fixed-charge
DP matches its independent oracle, while this module proves the
domination prune's own removal rule is sound (ROADMAP criterion 2 requires
this claim be "held separately from the oracle-vs-solver test"). Later
plans in this phase extend this module with a differential property
against both referees (D-01) -- the Phase 16 fixed-charge oracle and the
shipped greedy -- and this module *imports* that oracle and its Hypothesis
strategies from test_solver_fixed_charge_optimality.py rather than
re-deriving them: D-06 constrains how a referee is *derived*, not how it
is *consumed*, and re-deriving it here would give the prune a second,
unproven judge.

Uses django.test.SimpleTestCase throughout for every class that exercises
`prune_dominated_candidates` on synthetic input: the prune under test is
pure and never touches the ORM, so Hypothesis's own Django-integrated
TestCase's per-example database transaction would buy nothing here. The
one deliberate exception is `PruneHeuristicReceivesFullCandidateListTests`
(18-09/task 3), which needs the real committed station dataset via
`corridor.candidates()`'s DB-backed STRtree query to pin a production
guarantee against a real corridor -- it uses `django.test.TestCase`, the
same DB-backed pattern `test_solver_dispatch.RealCorridorDispatchTestCase`
already establishes elsewhere in this codebase, never Hypothesis.

This first test class, PruneRetainedSetTests, is the primary evidence for
ROADMAP criterion 3, which makes a claim about what the prune *keeps*
("never discards the sole station reachable from a starting_fuel=0
origin") rather than a claim about plan equality -- so the assertions
below are on the retained set directly (D-05), not on a downstream solve()
call.
"""
import inspect
import io
import itertools
import random
from dataclasses import dataclass, replace
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, tag
from hypothesis import example, given, settings
from hypothesis import strategies as st

from routing.services import Candidate, corridor, dp, heuristic, solve
from routing.services.exceptions import InfeasibleRouteError
from routing.services.prune import prune_dominated_candidates
from routing.services.solver import ESTIMATE_PRICE_SOURCE, SolverStrategy
from routing.tests.test_corridor_fixtures import (
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_solver_fixed_charge_optimality import (
    COST_TOLERANCE,
    MAX_STATIONS,
    OraclePlan,
    optimal_fixed_charge_plan,
    single_leg_routes,
)
from routing.tests.test_trust_margin_rule import (
    ADOPTED_MARGIN_USD,
    TAG_SHARE_LADDER,
    tagged_candidates,
)


def _candidate(opis_id, price, position, name=None):
    """Build a Candidate from (opis_id, price, position) with Decimal
    coercion, so the boundary fixtures below stay readable as plain
    numbers.
    """
    return Candidate(
        name=name or f"Station {opis_id}",
        opis_id=opis_id,
        price_per_gallon=Decimal(str(price)),
        distance_from_start_mi=Decimal(str(position)),
    )


def _sort_key(candidate):
    return (
        candidate.distance_from_start_mi,
        candidate.price_per_gallon,
        candidate.opis_id,
    )


class PruneRetainedSetTests(SimpleTestCase):
    """Retained-set boundary tests for prune_dominated_candidates.

    Each test asserts what survives the prune, not what a downstream
    solve() produces -- the primary evidence shape D-05 calls for.
    """

    def test_empty_candidates_returns_empty_list_at_long_tank_range(self):
        """An empty input returns an empty list -- long tank range."""
        result = prune_dominated_candidates(
            [], tank_range_mi=Decimal(1050), total_route_mi=Decimal(2000)
        )
        self.assertEqual(result, [])

    def test_empty_candidates_returns_empty_list_at_short_tank_range(self):
        """An empty input returns an empty list -- short tank range."""
        result = prune_dominated_candidates(
            [], tank_range_mi=Decimal(50), total_route_mi=Decimal(200)
        )
        self.assertEqual(result, [])

    def test_single_candidate_always_retained(self):
        """A lone station has no dominator -- nothing ranks before it in
        the total order, so no earlier station can ever satisfy the cover
        condition against it -- and it is therefore structurally
        unprunable, checked across several tank ranges and both a route
        longer and a route shorter than the tank range.
        """
        candidate = _candidate(opis_id=1, price="3.50", position=100)

        cases = [
            # (tank_range_mi, total_route_mi)
            (Decimal(500), Decimal(1000)),  # route longer than tank range
            (Decimal(500), Decimal(200)),  # route shorter than tank range
            (Decimal(50), Decimal(1000)),
            (Decimal(1050), Decimal(2000)),
        ]
        for tank_range_mi, total_route_mi in cases:
            with self.subTest(tank_range_mi=tank_range_mi, total_route_mi=total_route_mi):
                result = prune_dominated_candidates(
                    [candidate], tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
                )
                self.assertEqual(result, [candidate])

    def test_all_identical_prices_cluster_shorter_than_tank_range_retains_the_entire_cluster(self):
        """Stations at 1-mile spacing across [0, 100], all priced
        identically, on a 2,000-mile route with tank_range_mi=1050. Under
        D-22 a station is removable only when a cheaper-or-equal
        earlier-ranked station shares its exact position, or that earlier
        station's own supply interval already reaches FINISH
        (pos_B + T >= L). No station in this cluster satisfies the latter
        -- even the farthest, at position 100, has pos + T = 1150, short of
        the 2,000-mile route -- and no two candidates share a position, so
        NOTHING is removable: the cover condition never fires and all 101
        stations survive.

        This is a deliberate, honest consequence of a provable rule, not a
        gap: it leaves the front of a long route untouched at a tank range
        that cannot reach FINISH from there, exactly as D-24 anticipates.
        Latency at realistic scale remains Phase 18's to prove; this test
        only pins the retained-set outcome the rewritten rule dictates.
        """
        candidates = [
            _candidate(opis_id=i, price="3.499", position=i) for i in range(101)
        ]

        result = prune_dominated_candidates(
            candidates, tank_range_mi=Decimal(1050), total_route_mi=Decimal(2000)
        )

        self.assertEqual(len(result), 101)
        self.assertEqual({c.opis_id for c in result}, set(range(101)))

    def test_all_identical_prices_cluster_short_tank_range_retains_the_entire_cluster(self):
        """The same cluster as above, but with tank_range_mi shrunk to 10
        mi (small relative to the cluster's 1-mile spacing). The outcome is
        identical to the long-tank-range case above and for the same
        reason: no station's own supply interval reaches FINISH on this
        2,000-mile route (the farthest station has pos + T = 110), so the
        cover condition's tail branch never fires here either, regardless
        of tank range -- what matters is distance to FINISH, not the
        cluster's internal spacing. All 101 stations survive.
        """
        candidates = [
            _candidate(opis_id=i, price="3.499", position=i) for i in range(101)
        ]

        result = prune_dominated_candidates(
            candidates, tank_range_mi=Decimal(10), total_route_mi=Decimal(2000)
        )

        self.assertEqual(len(result), 101)
        self.assertEqual({c.opis_id for c in result}, set(range(101)))

    def test_all_identical_prices_cluster_relocated_into_tail_retains_only_the_first(self):
        """The same identically-priced, 1-mile-spaced cluster as the two
        tests above, but relocated so every member's own supply interval
        reaches FINISH: total_route_mi is set equal to tank_range_mi, so
        even the earliest station (position 0) has pos + T == L, and every
        later one clears it by more. This is the arm the two tests above
        never exercise -- with every station tied on price and every one
        of them tail-eligible, the cover condition fires all the way down
        the cluster in total order, so only the lowest-position (lowest
        opis_id, on ties) station survives. Without this test the class
        would have no coverage at all of multi-station tail collapse.
        """
        candidates = [
            _candidate(opis_id=i, price="3.499", position=i) for i in range(101)
        ]

        result = prune_dominated_candidates(
            candidates, tank_range_mi=Decimal(1050), total_route_mi=Decimal(1050)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].opis_id, 0)

    def test_route_shorter_than_tank_range_retains_exactly_the_prefix_minima(self):
        """With total_route_mi < tank_range_mi, every candidate's own
        pos + T already exceeds total_route_mi (T alone already exceeds
        it), so every candidate's supply interval reaches FINISH and the
        cover condition's ``pos_B + T >= L`` branch is available to all of
        them. A candidate is removable exactly when an earlier-ranked
        station is no more expensive, so the retained set is exactly the
        strict prefix minima of the price sequence under the total order --
        the running-minimum stations. This is now a *theorem*, not merely
        an observation: it follows directly from the cover condition once
        ``L <= T`` makes it universal. Asserted as set equality against an
        independently computed expectation, not merely a length.
        """
        candidates = [
            _candidate(opis_id=1, price="5.00", position=10),
            _candidate(opis_id=2, price="4.00", position=20),
            _candidate(opis_id=3, price="4.50", position=30),
            _candidate(opis_id=4, price="3.00", position=40),
            _candidate(opis_id=5, price="3.00", position=50),  # ties the running min
            _candidate(opis_id=6, price="2.50", position=60),
        ]
        total_route_mi = Decimal(100)
        tank_range_mi = Decimal(200)
        self.assertLess(total_route_mi, tank_range_mi)

        result = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        ordered = sorted(candidates, key=_sort_key)
        expected = []
        running_min = None
        for candidate in ordered:
            if running_min is None or candidate.price_per_gallon < running_min:
                expected.append(candidate)
                running_min = candidate.price_per_gallon

        self.assertEqual(set(result), set(expected))
        self.assertEqual({c.opis_id for c in result}, {1, 2, 4, 6})

    def test_sole_reachable_origin_from_starting_fuel_zero_is_retained(self):
        """ROADMAP criterion 3's named case: a station at
        distance_from_start_mi == 0 is the only station reachable from a
        starting_fuel=0 origin, and it is made the MOST EXPENSIVE station
        in the set so a naive price-only rule would drop it.

        D-26's structural derivation: any dominator B of the origin station
        would need pos_B <= pos_A == 0 (implied by the total order), so B
        would have to sit at position 0 too -- there is no station
        strictly closer to START that could dominate it. The position-0
        station is always first in the total order (nothing sorts before
        position 0 unless it too sits there), so nothing ever ranks before
        it and it can never have a dominator under either branch of the
        cover condition. This is asserted here, not special-cased:
        prune_dominated_candidates still contains no origin-fuel parameter
        and no branch keyed on how much fuel the route starts with.
        """
        origin = _candidate(opis_id=1, price="9.99", position=0)  # most expensive
        others = [
            _candidate(opis_id=2, price="3.00", position=100),
            _candidate(opis_id=3, price="2.50", position=300),
            _candidate(opis_id=4, price="4.00", position=500),
        ]
        candidates = [origin] + others

        result = prune_dominated_candidates(
            candidates, tank_range_mi=Decimal(500), total_route_mi=Decimal(1000)
        )

        self.assertIn(origin, result)

    def test_co_located_identical_price_pair_keeps_exactly_the_lower_opis_id(self):
        """Two candidates share a position and a price but have different
        opis_id values. Under the cover condition's ``pos_B == pos_A``
        branch, without the D-11 total-order tiebreak, both would see each
        other as a valid dominator (each satisfies ``price_B <= price_A``
        and ``pos_B == pos_A`` against the other) and both would be
        removed -- this test exists to catch exactly that failure. The
        total order resolves it: only the higher-``opis_id`` station ever
        ranks after the lower one, so only it ever looks backward and finds
        a dominator. Asserts exactly one survivor and that it is the lower
        opis_id.
        """
        lower = _candidate(opis_id=2, price="3.00", position=50)
        higher = _candidate(opis_id=5, price="3.00", position=50)

        result = prune_dominated_candidates(
            [higher, lower], tank_range_mi=Decimal(500), total_route_mi=Decimal(1000)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].opis_id, 2)

    def test_co_located_different_price_pair_keeps_the_cheaper(self):
        """Same position, different prices: the cheaper station satisfies
        both cover-condition clauses against the costlier one
        (``price_B <= price_A`` and ``pos_B == pos_A``), so the cheaper
        station survives and the costlier co-located station is removed.
        """
        cheaper = _candidate(opis_id=2, price="3.00", position=50)
        costlier = _candidate(opis_id=5, price="4.00", position=50)

        result = prune_dominated_candidates(
            [costlier, cheaper], tank_range_mi=Decimal(500), total_route_mi=Decimal(1000)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].opis_id, 2)

    def test_structural_invariants_on_a_mixed_input(self):
        """On a mixed input: every returned element is one of the input
        objects (identity membership, never a reconstruction), there are
        no duplicates, and the returned list is sorted by
        (distance_from_start_mi, price_per_gallon, opis_id).
        """
        candidates = [
            _candidate(opis_id=7, price="3.80", position=40),
            _candidate(opis_id=1, price="3.20", position=10),
            _candidate(opis_id=4, price="4.10", position=250),
            _candidate(opis_id=2, price="2.90", position=180),
            _candidate(opis_id=9, price="3.20", position=10),  # co-located w/ opis 1
            _candidate(opis_id=3, price="3.55", position=90),
            _candidate(opis_id=6, price="3.55", position=90),  # co-located w/ opis 3
        ]

        result = prune_dominated_candidates(
            candidates, tank_range_mi=Decimal(150), total_route_mi=Decimal(400)
        )

        candidate_ids = {id(c) for c in candidates}
        for element in result:
            self.assertIn(id(element), candidate_ids)

        self.assertEqual(len(result), len({id(c) for c in result}))

        self.assertEqual(result, sorted(result, key=_sort_key))


# D-03: the shipping default of $35 (ATRI operating-cost sourced, see
# STATE.md / CLAUDE.md) is one anchor point, not the only value exercised --
# the differential property below sweeps a continuous penalty range and
# additionally pins both boundary values as @example anchors.
PENALTY_ANCHORS = (Decimal("0"), Decimal("35"))


# D-27/D-28: the two hand-verified witnesses that disproved the reach-sliver
# rule, recorded verbatim to match routing/services/prune.py's "Why the
# reach-sliver rule is wrong" docstring section. Each tuple is a
# ``drawn_route`` shape -- (candidates, total_route_mi, tank_range_mi, mpg,
# starting_fuel) -- built through _candidate() so every value is a Decimal.
SLIVER_WITNESS_EQUAL_PRICE = (
    [
        _candidate(opis_id=0, price="1.00", position=1, name="S0"),
        _candidate(opis_id=1, price="1.00", position=2, name="S1"),
        _candidate(opis_id=2, price="1.01", position=3, name="S2"),
    ],
    Decimal("103"),
    Decimal("100"),
    Decimal("1"),
    Decimal("0.01"),
)

SLIVER_WITNESS_STRICT_INEQUALITY = (
    [
        _candidate(opis_id=0, price="1.00", position=0, name="S0"),
        _candidate(opis_id=1, price="1.01", position=1, name="S1"),
        _candidate(opis_id=2, price="2.00", position=2, name="S2"),
    ],
    Decimal("102"),
    Decimal("100"),
    Decimal("1"),
    Decimal("0"),
)


class PruneOracleDifferentialTests(SimpleTestCase):
    """The oracle arm of D-01's two-referee design: prune-then-solve with
    the Phase 16 fixed-charge oracle must return the same objective and
    the same feasibility verdict as solving over the unpruned set, for
    every swept penalty (PROOF-03, ROADMAP criterion 2). Wherever the
    unpruned oracle's optimum is strictly unique, the station set and fuel
    cost must additionally match.

    The oracle, its Hypothesis route strategy, and its tolerance constant
    are imported from routing.tests.test_solver_fixed_charge_optimality,
    never re-derived here (D-06) -- this class consumes that referee, it
    does not build a second one.
    """

    @given(single_leg_routes(), st.decimals(min_value=Decimal("0.00"), max_value=Decimal("60.00"), places=2))
    @settings(deadline=None, max_examples=200)
    # D-03 boundary anchors, on a representative multi-station route
    # (four stations spanning an 800-mile route with a 300-mile tank, so
    # more than one stop is generally needed):
    @example(
        drawn_route=(
            [
                _candidate(opis_id=1, price="3.20", position=200),
                _candidate(opis_id=2, price="2.90", position=350),
                _candidate(opis_id=3, price="3.50", position=500),
                _candidate(opis_id=4, price="3.00", position=650),
            ],
            Decimal(800),
            Decimal(300),
            Decimal(10),
            Decimal("1.00"),
        ),
        penalty=Decimal("0"),
    )
    @example(
        drawn_route=(
            [
                _candidate(opis_id=1, price="3.20", position=200),
                _candidate(opis_id=2, price="2.90", position=350),
                _candidate(opis_id=3, price="3.50", position=500),
                _candidate(opis_id=4, price="3.00", position=650),
            ],
            Decimal(800),
            Decimal(300),
            Decimal(10),
            Decimal("1.00"),
        ),
        penalty=Decimal("35"),
    )
    # D-05 boundary shape: zero candidates.
    @example(
        drawn_route=([], Decimal(2000), Decimal(1050), Decimal(10), Decimal("1.00")),
        penalty=Decimal("35"),
    )
    # D-05 boundary shape: exactly one candidate.
    @example(
        drawn_route=(
            [_candidate(opis_id=1, price="3.50", position=100)],
            Decimal(1000),
            Decimal(500),
            Decimal(10),
            Decimal("1.00"),
        ),
        penalty=Decimal("0"),
    )
    # D-05 boundary shape: all-identical prices. Bounded to MAX_STATIONS
    # (unlike plan 17-01's 101-station retained-set version, which the
    # oracle's 2**n subset enumeration could never run) -- six stations at
    # ten-mile spacing, all priced identically.
    @example(
        drawn_route=(
            [_candidate(opis_id=i, price="3.499", position=i * 10) for i in range(6)],
            Decimal(2000),
            Decimal(1050),
            Decimal(10),
            Decimal("1.00"),
        ),
        penalty=Decimal("35"),
    )
    # D-05 boundary shape: a route shorter than the tank range -- the same
    # six-candidate mixed-price shape PruneRetainedSetTests uses for its
    # own route-shorter-than-tank-range test.
    @example(
        drawn_route=(
            [
                _candidate(opis_id=1, price="5.00", position=10),
                _candidate(opis_id=2, price="4.00", position=20),
                _candidate(opis_id=3, price="4.50", position=30),
                _candidate(opis_id=4, price="3.00", position=40),
                _candidate(opis_id=5, price="3.00", position=50),
                _candidate(opis_id=6, price="2.50", position=60),
            ],
            Decimal(100),
            Decimal(200),
            Decimal(10),
            Decimal("1.00"),
        ),
        penalty=Decimal("0"),
    )
    # D-05 boundary shape: the starting_fuel=0 sole-reachable origin -- one
    # station at distance_from_start_mi == 0, made the MOST EXPENSIVE
    # station in the set, the rest farther out.
    @example(
        drawn_route=(
            [
                _candidate(opis_id=1, price="9.99", position=0),
                _candidate(opis_id=2, price="3.00", position=100),
                _candidate(opis_id=3, price="2.50", position=300),
                _candidate(opis_id=4, price="4.00", position=500),
            ],
            Decimal(1000),
            Decimal(500),
            Decimal(10),
            Decimal("0.00"),
        ),
        penalty=Decimal("35"),
    )
    # D-27/D-28: the two reach-sliver disproof witnesses, stacked as anchors
    # so they also run the full prune-then-oracle path end to end. Under
    # the current rule both pass here vacuously (nothing is removed from
    # either witness) -- PruneSliverRuleRegressionTests below is the
    # assertion that actually bites if the reach-sliver rule is ever
    # reintroduced.
    @example(drawn_route=SLIVER_WITNESS_EQUAL_PRICE, penalty=Decimal("0"))
    @example(drawn_route=SLIVER_WITNESS_STRICT_INEQUALITY, penalty=Decimal("0"))
    def test_prune_then_oracle_matches_oracle_over_full_set(self, drawn_route, penalty):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        self.assertLessEqual(
            len(candidates),
            MAX_STATIONS,
            f"single_leg_routes() drew more than MAX_STATIONS candidates: "
            f"{candidates!r} -- the oracle's subset enumeration cannot "
            f"terminate over this input.",
        )

        retained = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        unpruned_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=penalty,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )
        pruned_plan = optimal_fixed_charge_plan(
            retained,
            total_route_mi,
            penalty=penalty,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )

        context = (
            f"candidates={candidates!r}, retained={retained!r}, "
            f"total_route_mi={total_route_mi}, tank_range_mi={tank_range_mi}, "
            f"mpg={mpg}, starting_fuel={starting_fuel}, penalty={penalty}"
        )

        # Containment: every retained candidate is one of the inputs, and
        # pruning never grows the set.
        candidate_ids = {id(c) for c in candidates}
        for element in retained:
            self.assertIn(id(element), candidate_ids, f"retained a non-input candidate; {context}")
        self.assertLessEqual(len(retained), len(candidates), f"prune grew the candidate set; {context}")

        # Feasibility, always -- the oracle returns None exactly when no
        # subset is feasible, so this is the feasibility verdict.
        self.assertEqual(
            unpruned_plan is None,
            pruned_plan is None,
            f"feasibility verdicts disagree between pruned and unpruned "
            f"solves: unpruned_feasible={unpruned_plan is not None}, "
            f"pruned_feasible={pruned_plan is not None}; {context}",
        )
        if unpruned_plan is None:
            return

        self.assertIsInstance(unpruned_plan, OraclePlan)
        self.assertIsInstance(pruned_plan, OraclePlan)

        # Objective, always -- within the same summation-order tolerance
        # band the rest of the oracle's own tests use.
        self.assertLessEqual(
            abs(unpruned_plan.objective - pruned_plan.objective),
            COST_TOLERANCE,
            f"pruned objective ({pruned_plan.objective}) differs from the "
            f"unpruned objective ({unpruned_plan.objective}) beyond "
            f"COST_TOLERANCE; {context}",
        )

        # Station set and fuel cost, conditionally -- only where the
        # unpruned optimum is strictly unique, so a legitimately tied
        # optimum flipping under pruning can never flake this property.
        if unpruned_plan.is_unique_optimum:
            self.assertEqual(
                unpruned_plan.stop_opis_ids,
                pruned_plan.stop_opis_ids,
                f"station set differs though the unpruned optimum is "
                f"strictly unique; {context}",
            )
            self.assertLessEqual(
                abs(unpruned_plan.fuel_cost - pruned_plan.fuel_cost),
                COST_TOLERANCE,
                f"fuel_cost differs beyond COST_TOLERANCE though the "
                f"unpruned optimum is strictly unique; {context}",
            )


# The D-08 knob (the only value tasks in this module may move to fit the
# ~15s runtime ceiling) and its companion, the explicit density floor
# (min_size, never left to Hypothesis's small-biased max_size-only
# default). Both are consumed directly by dense_corridor_routes() below.
#
# 18-09/PROOF-03: GREEDY_MIN_STATIONS was lowered from its original 100 to
# 60 as part of the density retune -- together with widening
# tank_range_mi's ceiling below, this is what raises the share of draws
# landing under DP_TRANSITION_BUDGET (measured 18.6% before this retune,
# ~74-88% after it across five independent 150-200-example probes; see
# EXACT_DP_REACH_FLOOR's own comment). 60 stays well above the oracle
# arm's MAX_STATIONS=6 ceiling, so this arm remains a genuine density
# referee, not a duplicate of the small-candidate oracle arm.
GREEDY_MIN_STATIONS = 60
GREEDY_STATION_CAP = 250


@st.composite
def dense_corridor_routes(draw):
    """Draw a dense single-leg route for the greedy/density differential
    arm: GREEDY_MIN_STATIONS..GREEDY_STATION_CAP candidates on a route
    drawn from roughly 1,400-2,600 mi -- the band REQUIREMENTS.md's
    Evidence Base names as where the problem concentrates -- with
    tank_range_mi drawn from roughly 200-2,600 mi, so
    tank_range_mi / total_route_mi varies across the range that governs
    how much of the route falls in the prune's tail region.

    18-09/PROOF-03 retune: tank_range_mi's ceiling was widened from 1,050
    to 2,600 (matching total_route_mi's own ceiling, so tank_range_mi can
    reach or exceed total_route_mi on a drawn example) specifically to
    raise the share of draws that land under DP_TRANSITION_BUDGET. This is
    NOT because a wider tank makes the DP itself cheaper to run -- it is
    because prune_dominated_candidates() prunes far more aggressively as
    tank_range_mi approaches or exceeds total_route_mi (see
    PruneRetainedSetTests.
    test_route_shorter_than_tank_range_retains_exactly_the_prefix_minima's
    proof: once every station's own supply interval reaches FINISH, the
    retained set collapses to the strict price prefix-minima, typically a
    single-digit count even over a few hundred stations), which shrinks
    the search_set solve() actually estimates transition counts over.
    Lowering GREEDY_MIN_STATIONS (60, see its own comment above) works the
    same direction from the other side: fewer raw candidates means a
    smaller search_set even before the tail-collapse effect. Both levers
    are the ones this plan's action text names as preferred over shrinking
    the route, and the arm stays genuinely dense at 60-250 raw stations
    per example -- far denser than the oracle arm's MAX_STATIONS=6, so
    this remains a real density referee rather than a duplicate of that
    arm.

    Prices are drawn as integer cents and divided by 100 into a Decimal --
    deliberately not Hypothesis's own decimal-drawing strategy, which
    costs roughly twice the generation time at these list sizes, and at
    200 examples that difference is what decides whether the D-08 ceiling
    holds. Hypothesis's decimal strategy stays on the oracle arm
    (single_leg_routes(), imported verbatim from
    test_solver_fixed_charge_optimality per D-06). starting_fuel below is
    drawn the same integer-cents way, for the same reason.

    min_size=GREEDY_MIN_STATIONS is set explicitly: relying on max_size
    alone yields an average of about 4.3 candidates per example (Hypothesis's
    small-biased default), which would make this arm a duplicate of the
    oracle arm rather than a density referee.

    starting_fuel is drawn as a 2-place decimal across the closed interval
    [0, 1] (as integer cents 0-100, divided by 100), so the
    starting_fuel=0 origin boundary is inside the drawn distribution
    rather than needing its own unwieldy multi-hundred-station @example
    anchor.

    Station positions are unique (unique_by on the position element) and
    strictly inside (0, total_route_mi), so solve()'s _validate never
    rejects a drawn example.
    """
    total_route_mi = Decimal(draw(st.integers(min_value=1400, max_value=2600)))
    tank_range_mi = Decimal(draw(st.integers(min_value=200, max_value=2600)))
    station_tuples = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=100, max_value=600),  # integer cents: $1.00-$6.00
                st.integers(min_value=1, max_value=int(total_route_mi) - 1),
            ),
            min_size=GREEDY_MIN_STATIONS,
            max_size=GREEDY_STATION_CAP,
            unique_by=lambda t: t[1],
        )
    )
    candidates = [
        Candidate(
            name=f"D{i}",
            opis_id=i,
            price_per_gallon=Decimal(price_cents) / Decimal(100),
            distance_from_start_mi=Decimal(position),
        )
        for i, (price_cents, position) in enumerate(station_tuples)
    ]
    mpg = Decimal(draw(st.integers(min_value=1, max_value=50)))
    starting_fuel_cents = draw(st.integers(min_value=0, max_value=100))
    starting_fuel = Decimal(starting_fuel_cents) / Decimal(100)
    return candidates, total_route_mi, tank_range_mi, mpg, starting_fuel


# 18-09/PROOF-03: the minimum fraction of non-infeasible dense_corridor_routes()
# draws that must reach `SolverStrategy.EXACT_DP` on BOTH arms for the scoped
# equality test below to be considered meaningful evidence rather than a
# vacuous pass. Pinned HERE, before task 2 touches the assertion or retunes
# the density strategy, from two inputs only: the pre-retune measured
# baseline (18.6%, see this module's own "Measured evidence this plan starts
# from" table -- a 120-example probe over the SHIPPED, un-retuned
# dense_corridor_routes() found 22/118 non-infeasible examples, 18.6%, reach
# `exact_dp` on both arms) and the retune target task 2 commits to in its own
# action text: widen tank_range_mi's ceiling and/or lower the station-count
# floor so a MATERIALLY larger share reaches the exact-DP regime. 0.50 is
# chosen deliberately far from both ends: comfortably above 18.6% (2.7x) so a
# retune that only marginally moves the needle cannot silently satisfy this
# floor and hide the fact that nothing real changed, and comfortably below
# what task 2's retune is expected to reach so ordinary Hypothesis draw
# variance across CI runs cannot flake it. If task 2's retune cannot clear
# this floor, the floor is NOT to be lowered to fit -- report it as a finding
# about DP_TRANSITION_BUDGET's calibration instead (see this plan's own
# non-negotiable honesty rules).
EXACT_DP_REACH_FLOOR = Decimal("0.50")


@tag("slow")
class PruneGreedyDifferentialTests(SimpleTestCase):
    """The greedy/density arm of D-01's two-referee design.

    Tagged `"slow"` (Phase 18-04c) so `manage.py test --exclude-tag=slow`
    can run the rest of the suite without this class's own ~50-minute
    corridor-density sweep (see `18-04b-SUMMARY.md`'s Issues Encountered
    for that measured runtime) -- a bare mechanism for the exclusion
    18-04c-SUMMARY.md's own "run the full suite twice, skipping this
    class" instruction already names explicitly. The tag changes nothing
    about what this class asserts or how it runs when selected; it is
    metadata for test selection only.

    routing/tests/test_solver_optimality.py already proves solve() is a
    true pure-fuel cost optimum -- an exhaustive memoized recursive search
    over every (node_index, fuel_miles_remaining) state, not an
    approximation. routing/services/prune.py's own soundness proof (see
    its "Soundness" docstring section, the pure-fuel paragraph) shows that
    D-22 preserves that optimum: a dominated station's supply interval is
    contained in its dominator's at a price no higher, so removing it
    never raises the achievable minimum fuel cost and never turns a
    feasible route infeasible. Both solves must therefore attain the same
    cost -- this class is not a second, weaker oracle, it is the same
    claim PruneOracleDifferentialTests already proves at up to
    MAX_STATIONS=6, exercised here at a density (100-250 stations per
    example) the exponential oracle's subset enumeration can never reach.

    Corollary the fence names: if this property ever fails, the cause is
    either an unsound prune or a shipped greedy that is not optimal at
    this density -- both are real findings for Phase 18 to investigate,
    neither is something to tune away here (scope fence clause 4).

    Station set and stop count are deliberately NOT asserted here -- only
    feasibility and total_cost. The greedy's tie-breaks may legitimately
    select a different but equal-cost station set once a co-located
    duplicate is removed by the prune; asserting the set here would
    produce flakes that say nothing about soundness.
    PruneOracleDifferentialTests above already covers station-set
    equality, and only where the unpruned optimum is strictly unique
    (D-02).

    ## Refuted claims

    18-09 scoped this class's cost-equality assertion down to the regime
    it is actually proven in (both arms reaching `exact_dp`) after finding
    the assertion was, in every session that ran it to completion,
    exercising the `penalty_aware_heuristic` fallback instead -- a path
    that was never proven, or claimed, to be prune-invariant. Two specific
    claims were checked directly against this class's own dispatch data
    and found FALSE. Neither is a claim this codebase makes or should
    retry:

    1. **The heuristic is prune-invariant.** FALSE. 18-07's witness on a
       real Hypothesis-drawn falsifying example (100 synthetic stations,
       reproduced verbatim in `deferred-items.md`'s 18-07 entry) is a
       direct counterexample: `unpruned strategy penalty_aware_heuristic
       cost 1760.26 stops 4` vs `pruned strategy penalty_aware_heuristic
       cost 1708.51 stops 3` -- a `$51.75` divergence, `517500x`
       `COST_TOLERANCE`, on the SAME input the exact-DP arm would have
       proven equal had either arm actually reached it. This is not a
       surprising defect: the heuristic is an approximation by
       construction (its own module docstring; 18-04d-SUMMARY.md measured
       it at 6.5% average / 12.5% max off the exact DP's objective on the
       seven DP-tractable corridor cells available at that time), so
       prune-invariance -- an EXACT-equality property -- was never a
       property an approximation could have held in the first place.

    2. **`pruned_cost <= unpruned_cost` on the heuristic path** (the
       weaker fallback claim considered as a substitute for exact
       equality). ALSO FALSE. This plan's own pre-retune 120-example probe
       (see "Measured evidence this plan starts from" above) found the
       direction disagrees on 25 of 118 non-infeasible draws: pruned
       cheaper on 55, identical on 38, and pruned STRICTLY WORSE on 25 --
       worst observed divergence `pruned $186.19` vs `unpruned $107.03`.
       All 25 regressions occurred on the heuristic arm, in the
       pre-pruned-input configuration production never ships (D-20: the
       live `solve()` seam always hands the heuristic the FULL, unpruned
       `candidates`, never a caller-pre-pruned list -- see task 3's
       `PruneHeuristicReceivesFullCandidateListTests` below, which pins
       that actual guarantee directly).

    Both refutations are recorded here permanently, in the same voice
    Phase 17 used for the disproved Domination Theorem in
    `routing/services/prune.py`'s own docstring -- a claim this codebase
    once implicitly relied on (via this class's unscoped assertion) but
    does not, and should not, make.
    """

    @given(dense_corridor_routes())
    @settings(deadline=None, max_examples=200)
    def test_prune_then_solve_matches_solve_over_full_set_at_corridor_density(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        retained = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        context = (
            f"candidate_count={len(candidates)}, retained_count={len(retained)}, "
            f"total_route_mi={total_route_mi}, tank_range_mi={tank_range_mi}, "
            f"mpg={mpg}, starting_fuel={starting_fuel}"
        )

        # Containment: every retained candidate is one of the inputs, and
        # pruning never grows the set. (Full candidate reprs are omitted
        # from context deliberately at this density -- 100-250 Candidate
        # reprs would make a failure message unreadable; the oracle arm's
        # small candidate counts keep full reprs useful there instead.)
        candidate_ids = {id(c) for c in candidates}
        for element in retained:
            self.assertIn(id(element), candidate_ids, f"retained a non-input candidate; {context}")
        self.assertLessEqual(len(retained), len(candidates), f"prune grew the candidate set; {context}")

        try:
            unpruned_plan = solve(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                deadline=None,  # D-05: untimed -- this property compares the DP's exact answer, never its wall clock
            )
            unpruned_feasible = True
        except InfeasibleRouteError:
            unpruned_plan = None
            unpruned_feasible = False

        try:
            pruned_plan = solve(
                retained,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                deadline=None,  # D-05: untimed -- this property compares the DP's exact answer, never its wall clock
            )
            pruned_feasible = True
        except InfeasibleRouteError:
            pruned_plan = None
            pruned_feasible = False

        # Feasibility, always (D-02).
        self.assertEqual(
            unpruned_feasible,
            pruned_feasible,
            f"feasibility verdicts disagree between pruned and unpruned "
            f"solves: unpruned_feasible={unpruned_feasible}, "
            f"pruned_feasible={pruned_feasible}; {context}",
        )
        if not unpruned_feasible:
            return

        # Cost equality, SCOPED to the regime it is actually proven in
        # (18-09/PROOF-03): both arms must report SolverStrategy.EXACT_DP.
        # When either arm dispatches to the penalty-aware heuristic
        # instead, this example is out-of-regime for the equality claim --
        # the heuristic is refuted as prune-invariant (see this class's
        # own "## Refuted claims" docstring section above) -- and is
        # recorded as such by simply not asserting equality against it,
        # rather than by weakening COST_TOLERANCE or the claim itself.
        # PruneGreedyDifferentialExactReachGuardTests below is the
        # anti-vacuity guard that keeps this scoping from silently
        # hollowing this test out.
        if unpruned_plan.strategy != SolverStrategy.EXACT_DP or pruned_plan.strategy != SolverStrategy.EXACT_DP:
            return

        self.assertLessEqual(
            abs(unpruned_plan.total_cost - pruned_plan.total_cost),
            COST_TOLERANCE,
            f"pruned total_cost ({pruned_plan.total_cost}) differs from "
            f"the unpruned total_cost ({unpruned_plan.total_cost}) beyond "
            f"COST_TOLERANCE; {context}",
        )


# 18-09/PROOF-03: a fixed, seeded sample -- deterministic random.Random
# generation, NOT a Hypothesis draw -- mirroring PruneCorpusParams/
# build_prune_corpus's precedent (D-16/D-36) rather than inventing a second
# corpus-building convention. The guard below needs the same fixed sample on
# every run so a flaky share is never mistaken for a real regression in
# DP_TRANSITION_BUDGET's calibration or the retuned density strategy.
_EXACT_DP_REACH_SAMPLE_SEED = 20260802
_EXACT_DP_REACH_SAMPLE_SIZE = 150


def _sample_dense_corridor_routes(seed, count):
    """Deterministically draw `count` dense-corridor-shaped routes from a
    fresh `random.Random(seed)`, using the SAME parameter ranges
    dense_corridor_routes() draws from (GREEDY_MIN_STATIONS..
    GREEDY_STATION_CAP stations, 1,400-2,600mi routes, 200-2,600mi tank
    ranges) -- but via the stdlib random module instead of Hypothesis, so
    two calls with the same seed are always byte-identical, exactly as
    build_prune_corpus() is elsewhere in this module.
    """
    rng = random.Random(seed)
    routes = []
    for _ in range(count):
        total_route_mi = Decimal(rng.randint(1400, 2600))
        tank_range_mi = Decimal(rng.randint(200, 2600))
        station_count = rng.randint(GREEDY_MIN_STATIONS, GREEDY_STATION_CAP)
        positions = rng.sample(range(1, int(total_route_mi)), station_count)
        candidates = [
            Candidate(
                name=f"D{i}",
                opis_id=i,
                price_per_gallon=Decimal(rng.randint(100, 600)) / Decimal(100),
                distance_from_start_mi=Decimal(position),
            )
            for i, position in enumerate(positions)
        ]
        mpg = Decimal(rng.randint(1, 50))
        starting_fuel = Decimal(rng.randint(0, 100)) / Decimal(100)
        routes.append((candidates, total_route_mi, tank_range_mi, mpg, starting_fuel))
    return routes


class PruneGreedyDifferentialExactReachGuardTests(SimpleTestCase):
    """18-09/PROOF-03's anti-vacuity guard for
    `PruneGreedyDifferentialTests.
    test_prune_then_solve_matches_solve_over_full_set_at_corridor_density`'s
    scoping above. `prune(x) -> exact_dp on nothing` (scoping the equality
    to a regime that is never actually reached) would satisfy that test's
    equality assertion vacuously -- exactly the failure mode
    PruneReductionGuardTests' own docstring names for `prune(x) -> x`. This
    class is the guard that would fail if the scoping ever hollowed the
    test out that way.

    Uses `dp.estimate_transition_count` directly against
    `SolverStrategy.EXACT_DP`'s own dispatch condition
    (`estimate <= dp.DP_TRANSITION_BUDGET`, see `solver.py`'s dispatch
    block), never a full `solve()` call -- structurally identical to what
    `solve()` itself computes for dispatch purposes, at a fraction of the
    cost of actually running the DP or the heuristic on every sampled
    route. Mirrors both arms `PruneGreedyDifferentialTests` exercises:
    `solve(candidates, ...)`'s internal search_set is
    `prune(candidates)` (the "unpruned-input" arm), and
    `solve(retained, ...)`'s internal search_set is `prune(retained)` (the
    "pruned-input" arm) -- both computed explicitly below rather than
    assumed identical, even though prune's own idempotence makes them
    equal in every sampled case measured so far.
    """

    def test_exact_dp_reach_share_meets_the_pinned_floor(self):
        routes = _sample_dense_corridor_routes(
            _EXACT_DP_REACH_SAMPLE_SEED, _EXACT_DP_REACH_SAMPLE_SIZE
        )

        both_exact = 0
        for candidates, total_route_mi, tank_range_mi, mpg, starting_fuel in routes:
            retained = prune_dominated_candidates(
                candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
            )
            unpruned_arm_search_set = prune_dominated_candidates(
                candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
            )
            pruned_arm_search_set = prune_dominated_candidates(
                retained, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
            )

            unpruned_estimate = dp.estimate_transition_count(
                unpruned_arm_search_set,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                starting_fuel=starting_fuel,
            )
            pruned_estimate = dp.estimate_transition_count(
                pruned_arm_search_set,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                starting_fuel=starting_fuel,
            )

            if (
                unpruned_estimate <= dp.DP_TRANSITION_BUDGET
                and pruned_estimate <= dp.DP_TRANSITION_BUDGET
            ):
                both_exact += 1

        share = Decimal(both_exact) / Decimal(len(routes))

        self.assertGreaterEqual(
            share,
            EXACT_DP_REACH_FLOOR,
            f"measured exact_dp reach share {share} (both_exact={both_exact}/"
            f"{len(routes)}, seed={_EXACT_DP_REACH_SAMPLE_SEED}) fell below "
            f"EXACT_DP_REACH_FLOOR={EXACT_DP_REACH_FLOOR} -- the scoped "
            f"equality assertion above would be exercising too few examples "
            f"to mean anything. Do not lower EXACT_DP_REACH_FLOOR to make "
            f"this pass; a failure here is a finding about "
            f"DP_TRANSITION_BUDGET's calibration or the density strategy, "
            f"not a bug in this guard.",
        )


class PruneHeuristicReceivesFullCandidateListTests(TestCase):
    """18-09/task 3: pins the production guarantee that actually protects
    the penalty-aware heuristic fallback, now that prune-invariance of the
    heuristic itself is refuted (see `PruneGreedyDifferentialTests`' own
    "## Refuted claims" docstring section above).

    `solver.solve()`'s dispatch block prunes only `search_set` -- the set
    the exact DP explores -- and, on the branch that dispatches to
    `heuristic.solve_penalty_aware_heuristic`, hands it the FULL,
    UNPRUNED `candidates` argument, never `search_set` (see
    `solver.py`'s own dispatch-block docstring, step 4: "over budget,
    delegate instead to `heuristic.solve_penalty_aware_heuristic` over
    the FULL, unpruned `candidates`" -- the same "operate over the full
    candidate list" discipline D-20 already established for this
    function's reporting statistics). This is the property the fallback
    path actually relies on: the heuristic is never handed a
    caller-pre-pruned list to begin with, so there is no pruned/unpruned
    divergence for it to be invariant -- or not invariant -- across on
    the live request path. This test pins that mechanism directly, by
    patching `heuristic.solve_penalty_aware_heuristic` and asserting on
    the actual argument it receives, rather than inferring the mechanism
    from the resulting plan (which the refuted claims above already show
    is unsafe to do). It is what would catch this protection being
    silently removed -- e.g. a future change that starts pruning before
    the heuristic branch too.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def test_heuristic_branch_receives_the_full_unpruned_candidate_list(self):
        factor_for = factor_lookup_for_basis("neutral")
        # toronto_oh-hillsboro_or @1050mi is a real, committed corridor
        # confirmed (test_solver_dispatch.HeavyLightDispatchTests) to
        # dispatch to the penalty-aware heuristic at these parameters --
        # dense enough (509 raw candidates, 214 retained after pruning)
        # that this assertion is genuinely load-bearing: a bug that
        # started handing the heuristic the 214-candidate search_set
        # instead of the 509-candidate full list would be caught here.
        route = load_corridor_route("toronto_oh-hillsboro_or")
        candidates = corridor.candidates(route, factor_for=factor_for)
        tank_range_mi = Decimal(1050)

        retained = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=route.total_route_mi
        )
        self.assertLess(
            len(retained),
            len(candidates),
            "toronto_oh-hillsboro_or @1050mi no longer prunes to a "
            "strictly smaller search_set -- this test's own load-bearing "
            "premise (candidates != search_set) no longer holds; pick a "
            "different real corridor/tank-range cell.",
        )

        with patch(
            "routing.services.heuristic.solve_penalty_aware_heuristic",
            wraps=heuristic.solve_penalty_aware_heuristic,
        ) as spy:
            plan = solve(
                candidates,
                route.total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=Decimal(10),
                starting_fuel=Decimal("0.5"),
                penalty=Decimal(35),
                deadline=None,  # D-05: untimed -- this property compares the DP's exact answer, never its wall clock
            )

        self.assertEqual(
            plan.strategy,
            SolverStrategy.PENALTY_AWARE_HEURISTIC,
            "toronto_oh-hillsboro_or @1050mi no longer dispatches to the "
            "penalty-aware heuristic at these parameters -- this test's "
            "own load-bearing premise no longer holds; pick a different "
            "real corridor/tank-range cell.",
        )
        spy.assert_called_once()
        received_candidates = spy.call_args.args[0]
        self.assertEqual(
            list(received_candidates),
            list(candidates),
            "heuristic.solve_penalty_aware_heuristic received a candidate "
            "list that differs from the FULL, unpruned candidates solve() "
            "was called with -- the D-20 guarantee that protects the "
            "fallback path (never handing it a pre-pruned list) is "
            "broken.",
        )
        self.assertGreater(
            len(received_candidates),
            len(retained),
            "heuristic received a list no larger than the pruned "
            "search_set (len={}) -- it should have received the full "
            "{}-candidate list, not the {}-candidate search_set.".format(
                len(retained), len(candidates), len(retained)
            ),
        )


class PrunePenaltyInvarianceTests(SimpleTestCase):
    """D-04: prune_dominated_candidates takes no penalty argument, and its
    retained set is byte-identical across penalties. Both claims are
    written so they can actually fail -- the structural test breaks the
    build the moment someone threads a penalty through the prune's
    signature, and the behavioral property re-invokes the prune separately
    per penalty rather than comparing a single result to itself, so a
    future penalty-dependent prune has somewhere real to disagree.

    [Amended 2026-08-17, Phase 25] The description above is superseded, not
    deleted, per this project's invert-a-guard-never-delete-it rule (D-07).
    `prune_dominated_candidates` now DOES take `mpg`/`penalty` keyword-only
    parameters, defaulting to `None` (D-04's 2026-08-17 amendment) -- the
    strengthened branch activates only when both are supplied. This class
    no longer proves an INVARIANCE (the retained set no longer stays fixed
    as `penalty` varies, once `mpg` is also supplied); it proves a
    TRANSITION instead: the retained set is non-increasing in `penalty`
    (D-07(a)), and at `penalty = Decimal(0)` it is byte-identical to the
    unstrengthened default path (D-07(b), the reduction anchor -- see
    below). Both halves still break the build if the strengthening is
    anything other than purely additive.
    """

    def test_signature_has_no_undeclared_knob_beyond_mpg_and_penalty(self):
        """D-04's widened parameter tuple, and the "no undeclared knob"
        property this test has always guarded -- unchanged in substance,
        only in which names are enumerated. Three claims: (1) the exact
        parameter tuple, now five names, not three; (2) `penalty=` alone
        (no `mpg`) must NOT activate the strengthened branch -- D-04's
        "only when BOTH are supplied" contract, strictly stronger than the
        old `assertRaises(TypeError)` this replaces, since it proves the
        keyword is now valid AND inert alone, not merely valid; (3) an
        unknown keyword such as `window_mi=` must still raise `TypeError`
        -- no `gap <= W` window or prefilter-width parameter exists (D-09
        below), and no other undeclared knob either.
        """
        self.assertEqual(
            tuple(inspect.signature(prune_dominated_candidates).parameters),
            ("candidates", "tank_range_mi", "total_route_mi", "mpg", "penalty"),
        )

        candidates = [
            _candidate(opis_id=1, price="3.50", position=0),
            _candidate(opis_id=2, price="3.00", position=0),
            _candidate(opis_id=3, price="2.90", position=500),
        ]
        default = prune_dominated_candidates(
            candidates, tank_range_mi=Decimal(500), total_route_mi=Decimal(1000)
        )
        penalty_only = prune_dominated_candidates(
            candidates,
            tank_range_mi=Decimal(500),
            total_route_mi=Decimal(1000),
            penalty=Decimal("35"),
        )
        self.assertEqual(
            tuple(c.opis_id for c in penalty_only),
            tuple(c.opis_id for c in default),
            "penalty= supplied alone (mpg omitted) must not change the "
            "retained set -- the strengthened branch activates only when "
            "BOTH mpg and penalty are supplied (D-04)",
        )

        with self.assertRaises(TypeError):
            prune_dominated_candidates(
                candidates,
                tank_range_mi=Decimal(500),
                total_route_mi=Decimal(1000),
                window_mi=Decimal("10"),
            )

    @given(
        single_leg_routes(),
        st.lists(
            st.decimals(min_value=Decimal("0.00"), max_value=Decimal("60.00"), places=2),
            min_size=2,
            max_size=4,
            unique=True,
        ),
    )
    # Discretionary and deliberately distinct from the differential
    # property's fixed max_examples=200 (PruneOracleDifferentialTests
    # above), following the Phase 16 precedent where the discretionary
    # consistency class ran at 50 while the anchor classes stayed at 200.
    @settings(deadline=None, max_examples=100)
    def test_penalty_domination_is_a_non_increasing_additive_transition(
        self, drawn_route, penalties
    ):
        """[Amended 2026-08-17, Phase 25] Renamed from
        `test_retained_set_is_byte_identical_across_penalties`, whose old
        name is actively misleading now that D-01 makes the retained set
        genuinely penalty-sensitive once `mpg` is also supplied. Parts (a)
        feasibility agreement and (b) objective agreement within
        COST_TOLERANCE are unchanged in substance below. The old part (c)
        (byte-identity across every drawn penalty) is replaced by D-07's
        two halves: (a) the retained set is non-increasing in `penalty` --
        a higher penalty may remove more, never less; (b) at
        `penalty = Decimal(0)` (with `mpg` supplied) the retained set is
        byte-identical to the unstrengthened default path (no `mpg`, no
        `penalty`) -- the reduction anchor, checked on every drawn example
        rather than only when Hypothesis happens to draw exactly zero.
        """
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        default_retained = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )
        default_opis_ids = tuple(c.opis_id for c in default_retained)

        # D-07(b): penalty=Decimal(0) with mpg supplied is byte-identical
        # to the default (no-mpg/no-penalty) path -- checked unconditionally
        # on every example, not only when Hypothesis happens to draw zero.
        retained_at_zero = prune_dominated_candidates(
            candidates,
            tank_range_mi=tank_range_mi,
            total_route_mi=total_route_mi,
            mpg=mpg,
            penalty=Decimal("0"),
        )
        self.assertEqual(
            tuple(c.opis_id for c in retained_at_zero),
            default_opis_ids,
            f"D-07(b): retained set at penalty=0 (mpg supplied) must be "
            f"byte-identical to the default no-mpg/no-penalty path; "
            f"candidates={candidates!r}, tank_range_mi={tank_range_mi}, "
            f"total_route_mi={total_route_mi}, mpg={mpg}",
        )

        retained_opis_sets_by_penalty = {Decimal("0"): frozenset(default_opis_ids)}
        for penalty in penalties:
            # Called fresh inside the loop, once per penalty -- never
            # computed once outside it and compared to itself, since that
            # would have nothing real to disagree with.
            retained = prune_dominated_candidates(
                candidates,
                tank_range_mi=tank_range_mi,
                total_route_mi=total_route_mi,
                mpg=mpg,
                penalty=penalty,
            )
            retained_opis_sets_by_penalty[penalty] = frozenset(
                c.opis_id for c in retained
            )

            unpruned_plan = optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=penalty,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            pruned_plan = optimal_fixed_charge_plan(
                retained,
                total_route_mi,
                penalty=penalty,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            context = (
                f"candidates={candidates!r}, retained={retained!r}, "
                f"total_route_mi={total_route_mi}, tank_range_mi={tank_range_mi}, "
                f"mpg={mpg}, starting_fuel={starting_fuel}, penalty={penalty}"
            )
            self.assertEqual(
                unpruned_plan is None,
                pruned_plan is None,
                f"feasibility verdicts disagree between pruned and "
                f"unpruned solves at penalty={penalty}; {context}",
            )
            if unpruned_plan is not None:
                # Condition 4's own bound is on REGRET, not on exact
                # preservation: prune.py's "Bound, precisely" paragraph
                # proves the strengthened path can raise the true optimum
                # by up to (but strictly less than) `penalty` itself -- a
                # weaker guarantee than conditions 1-3's exact
                # substitution, discovered by this very property finding a
                # counterexample at penalty=0.16 that cleared COST_TOLERANCE
                # (0.0001) while staying safely under the true bound. The
                # tolerance here is `penalty + COST_TOLERANCE`: at
                # `penalty=Decimal(0)` this reduces to plain COST_TOLERANCE
                # (matching D-07(b)'s exact reduction anchor); at any
                # positive penalty it checks the actual proved bound rather
                # than the much tighter exactness conditions 1-3 alone earn.
                self.assertLess(
                    abs(unpruned_plan.objective - pruned_plan.objective),
                    penalty + COST_TOLERANCE,
                    f"pruned objective ({pruned_plan.objective}) differs "
                    f"from the unpruned objective "
                    f"({unpruned_plan.objective}) by penalty or more "
                    f"at penalty={penalty}; {context}",
                )

        # D-07(a): non-increasing in penalty -- for any two penalties
        # (including the pinned zero anchor above), the retained set at
        # the higher penalty must be a subset of the retained set at the
        # lower penalty. A higher penalty may remove more, never less.
        for p1, p2 in itertools.combinations(sorted(retained_opis_sets_by_penalty), 2):
            lower_penalty, higher_penalty = p1, p2
            lower_set = retained_opis_sets_by_penalty[lower_penalty]
            higher_set = retained_opis_sets_by_penalty[higher_penalty]
            self.assertTrue(
                higher_set <= lower_set,
                f"D-07(a): retained set at penalty={higher_penalty} "
                f"({sorted(higher_set)}) is not a subset of the retained "
                f"set at penalty={lower_penalty} ({sorted(lower_set)}) -- "
                f"the retained set must be non-increasing in penalty; "
                f"candidates={candidates!r}, tank_range_mi={tank_range_mi}, "
                f"total_route_mi={total_route_mi}, mpg={mpg}",
            )


class PenaltyDominationMarginSensitivityWitnessTests(SimpleTestCase):
    """D-09(b): the non-vacuous witness that makes condition 4c's
    provenance guard falsifiable rather than merely asserted. Permanently
    pinned, hand-built, never Hypothesis-drawn -- the whole point is a
    SPECIFIC, reproducible counterexample, not a property over a
    distribution.

    Three hand-built stations on a 1000 mi route with a 450 mi tank: a
    cheap, real-priced, MANDATORY gateway D at 400 mi (within the initial
    tank range, so every plan needs it or an equivalent); a pricier,
    estimate-priced B at 600 mi; and a cheaper, real-priced A at 650 mi.
    Both B and A sit beyond the 450 mi initial range (condition 4d's
    starting-fuel guard, below) and are each individually tail-reaching
    (600/650 + 450 >= 1000), so D is unconditionally required regardless of
    which of B/A gets used for the route's remaining leg -- this is what
    makes the "extra stop is never worth it" argument genuinely apply here,
    unlike a naive two-station design where the cheap station could be
    reached directly from START (see condition 4d's own counterfactual).

    A PRICE-ONLY reading of condition 4b (ignoring condition 4c's
    provenance guard entirely) would remove A, because the raw price gap
    between B and A, scaled by a full tank, clears the penalty bar. But
    removing A is NOT actually safe at the production trust margin
    (`ADOPTED_MARGIN_USD`) -- with A gone, the fixed-charge oracle's own
    optimum is forced onto B, which is estimate-priced and so pays
    `ADOPTED_MARGIN_USD` on top of a higher raw price, raising the true
    optimum by well over COST_TOLERANCE. The shipped rule does NOT make
    this mistake: condition 4c forbids exactly this pairing (B estimate, A
    real), so `prune_dominated_candidates` retains all three stations.
    Together, these two facts are D-09's paired proof in test form: the
    "retained counts do not move with the margin's dollar value" claim
    survives for the shipped rule, but only because of condition 4c -- and
    this witness is what shows the claim would be FALSE without it.
    """

    TANK_RANGE_MI = Decimal("450")
    TOTAL_ROUTE_MI = Decimal("1000")
    MPG = Decimal("6.5")
    PENALTY = PENALTY_ANCHORS[1]  # Decimal("35")
    STARTING_FUEL = Decimal("1")

    def _stations(self):
        gateway_mandatory_real = _candidate(opis_id=3, price="3.00", position=400)
        alt_pricier_estimate = replace(
            _candidate(opis_id=1, price="3.60", position=600),
            price_source=ESTIMATE_PRICE_SOURCE,
        )
        cheap_real = _candidate(opis_id=2, price="3.50", position=650)
        return gateway_mandatory_real, alt_pricier_estimate, cheap_real

    def test_naive_price_only_reading_would_remove_the_real_priced_station(self):
        _gateway, alt_pricier_estimate, cheap_real = self._stations()

        raw_price_gap = alt_pricier_estimate.price_per_gallon - cheap_real.price_per_gallon
        raw_bound = raw_price_gap * self.TANK_RANGE_MI / self.MPG
        self.assertLess(
            raw_bound,
            self.PENALTY,
            "witness setup error: the raw price-only bound must clear the "
            "penalty bar (a naive reading must want to remove the cheap "
            "real-priced station) for this to be a meaningful counterfactual",
        )
        self.assertGreater(
            cheap_real.distance_from_start_mi,
            self.TANK_RANGE_MI,
            "witness setup error: A must sit beyond the initial tank range "
            "(condition 4d) so the counterfactual isn't blocked by the "
            "starting-fuel guard for a reason unrelated to condition 4c",
        )

    def test_removing_the_real_priced_station_raises_the_oracle_optimum_at_production_margin(
        self,
    ):
        gateway, alt_pricier_estimate, cheap_real = self._stations()

        all_three_stations = [gateway, alt_pricier_estimate, cheap_real]
        naively_pruned = [gateway, alt_pricier_estimate]  # A removed, per the naive reading

        full_plan = optimal_fixed_charge_plan(
            all_three_stations,
            self.TOTAL_ROUTE_MI,
            penalty=self.PENALTY,
            tank_range_mi=self.TANK_RANGE_MI,
            mpg=self.MPG,
            starting_fuel=self.STARTING_FUEL,
            trust_margin=ADOPTED_MARGIN_USD,
        )
        naive_plan = optimal_fixed_charge_plan(
            naively_pruned,
            self.TOTAL_ROUTE_MI,
            penalty=self.PENALTY,
            tank_range_mi=self.TANK_RANGE_MI,
            mpg=self.MPG,
            starting_fuel=self.STARTING_FUEL,
            trust_margin=ADOPTED_MARGIN_USD,
        )
        self.assertIsNotNone(full_plan)
        self.assertIsNotNone(naive_plan)
        objective_increase = naive_plan.objective - full_plan.objective
        self.assertGreater(
            objective_increase,
            COST_TOLERANCE,
            "the counterfactual claim failed: removing the real-priced "
            "station on the naive (condition-4c-blind) reading must raise "
            "the fixed-charge oracle's optimum at ADOPTED_MARGIN_USD by "
            f"more than COST_TOLERANCE; got increase={objective_increase}",
        )

    def test_shipped_rule_does_not_remove_the_real_priced_station(self):
        gateway, alt_pricier_estimate, cheap_real = self._stations()

        shipped_retained = prune_dominated_candidates(
            [gateway, alt_pricier_estimate, cheap_real],
            tank_range_mi=self.TANK_RANGE_MI,
            total_route_mi=self.TOTAL_ROUTE_MI,
            mpg=self.MPG,
            penalty=self.PENALTY,
        )
        retained_opis_ids = {c.opis_id for c in shipped_retained}
        self.assertIn(
            cheap_real.opis_id,
            retained_opis_ids,
            "condition 4c must forbid this pairing (B estimate-priced, A "
            "real-priced) -- the shipped rule removed the real-priced "
            "station anyway, which is exactly the unsound behaviour "
            "condition 4c exists to prevent",
        )


class PruneSliverRuleRegressionTests(SimpleTestCase):
    """Guards against reintroducing the disproven reach-sliver rule.

    Both witnesses below are permanently recorded in
    routing/services/prune.py's "Why the reach-sliver rule is wrong"
    docstring section. The reach-sliver rule removed the middle station in
    each witness and thereby raised the true optimum -- $102.0100 to
    $102.0200 for the equal-price witness, $103.01 to $104.00 (99x
    COST_TOLERANCE) for the strict-inequality witness -- both at
    penalty=0, a pure-fuel objective. The current rule must retain all
    three stations in each witness.

    These retention assertions are the primary evidence (D-28), not the
    @example anchors stacked onto PruneOracleDifferentialTests above:
    under the current rule neither witness is pruned at all, so a
    prune-then-solve comparison alone passes vacuously and asserts nothing
    rule-specific. Asserting the full retained opis_id tuple, rather than
    just "the middle station survives", also catches over-pruning of
    either of the other two stations.
    """

    def test_equal_price_witness_retains_all_three_stations(self):
        candidates, total_route_mi, tank_range_mi, _mpg, _starting_fuel = (
            SLIVER_WITNESS_EQUAL_PRICE
        )

        result = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        self.assertEqual(
            tuple(c.opis_id for c in result),
            (0, 1, 2),
            "equal-price sliver witness (S0 $1.00@1mi, S1 $1.00@2mi, "
            "S2 $1.01@3mi; tank_range_mi=100, total_route_mi=103): the "
            "reach-sliver rule removed S1 (opis_id=1) here and raised the "
            "true optimum from $102.0100 to $102.0200; the current rule "
            f"must retain all three stations. Got retained={result!r}",
        )

    def test_strict_inequality_witness_retains_all_three_stations(self):
        candidates, total_route_mi, tank_range_mi, _mpg, _starting_fuel = (
            SLIVER_WITNESS_STRICT_INEQUALITY
        )

        result = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        self.assertEqual(
            tuple(c.opis_id for c in result),
            (0, 1, 2),
            "strict-inequality sliver witness (S0 $1.00@0mi, "
            "S1 $1.01@1mi, S2 $2.00@2mi; tank_range_mi=100, "
            "total_route_mi=102): the reach-sliver rule removed S1 "
            "(opis_id=1) here and raised the true optimum from $103.01 "
            "to $104.00 (99x COST_TOLERANCE); the current rule must "
            f"retain all three stations. Got retained={result!r}",
        )


@dataclass(frozen=True)
class PruneCorpusParams:
    """The D-16 single source of truth for D-36's dense reduction-guard
    corpus (PruneReductionGuardTests.test_dense_corpus_reduction_rate_
    exceeds_floor below), mirroring CorpusParams' precedent in
    test_solver_fixed_charge_optimality.py: every field is fixed here,
    before the first measurement is taken, and is never revisited after
    seeing a result (D-14).

    total_route_mi is set materially longer than tank_range_mi so a
    substantial share of the uniformly-placed stations land in the
    prune's tail region (pos >= total_route_mi - tank_range_mi) and the
    tail-cover pass fires; colocated_share pins the fraction of stations
    deliberately placed at an already-used position so the co-located
    dedup pass fires too. station_count is "hundreds of stations", the
    realistic-corridor-scale D-36 calls for -- distinct from and not
    derived from GREEDY_STATION_CAP above, which is a Hypothesis
    per-example density knob, not a fixed corpus size.
    """

    seed: int
    station_count: int
    total_route_mi: Decimal
    tank_range_mi: Decimal
    min_price_cents: int
    max_price_cents: int
    colocated_share: Decimal


# The D-16 single shared instance. Every field was chosen before the
# reduction rate below was ever measured: seed is today's date (distinct
# from Phase 16's CORPUS_PARAMS.seed=20260730, so the two corpora never
# accidentally interleave draws from the same RNG state); station_count
# and the price band match the "hundreds of stations" / realistic
# corridor-price scale used elsewhere in this module; total_route_mi is
# materially longer than tank_range_mi (2200 vs 1050, matching the
# UI-default loaded-semi tank from Phase 16's CORPUS_PARAMS) so roughly
# half the route falls in the tail region by construction, not by tuning;
# colocated_share=0.10 guarantees real co-located duplicates without
# dominating the corpus.
PRUNE_CORPUS_PARAMS = PruneCorpusParams(
    seed=20260731,
    station_count=500,
    total_route_mi=Decimal(2200),
    tank_range_mi=Decimal(1050),
    min_price_cents=100,
    max_price_cents=600,
    colocated_share=Decimal("0.10"),
)

# D-36: set once, after PRUNE_CORPUS_PARAMS above was measured a single
# time -- 50.80% (246/500 retained) reduction, recorded verbatim in
# 17-05-SUMMARY.md. Set at 0.15, roughly a third of that measured rate
# (0.15 / 0.508 ~= 0.30), following DISAGREEMENT_FLOOR's precedent
# exactly: wide enough that ordinary distribution drift never flakes this
# guard, tight enough that a silently no-op prune (which scores exactly 0
# here) fails it instantly. Do not adjust PRUNE_CORPUS_PARAMS or this
# floor to make a future run pass (D-14/D-17) -- see the failure message
# on the guard below.
PRUNE_REDUCTION_FLOOR = Decimal("0.15")


def build_prune_corpus(*, params=PRUNE_CORPUS_PARAMS):
    """Deterministically build params.station_count candidates on a single
    dense corridor, from a fresh random.Random(params.seed) -- never the
    global random module, so two consecutive calls return byte-identical
    corpora (proven by
    PruneReductionGuardTests.test_build_prune_corpus_is_deterministic_
    across_two_calls below).

    A params.colocated_share fraction of the stations are placed at a
    position already drawn for an earlier station in the same corpus (a
    duplicate position, independently priced), so the prune's co-located
    dedup pass has real duplicates to resolve. The remaining
    (1 - params.colocated_share) fraction get fresh, mutually distinct
    positions drawn without replacement from across the whole route, so a
    substantial share of the corpus lands in the tail region purely from
    that uniform spread, at params.total_route_mi materially longer than
    params.tank_range_mi.
    """
    rng = random.Random(params.seed)
    n_colocated = int(params.station_count * params.colocated_share)
    n_fresh = params.station_count - n_colocated

    fresh_positions = rng.sample(range(1, int(params.total_route_mi)), n_fresh)
    colocated_positions = [rng.choice(fresh_positions) for _ in range(n_colocated)]
    positions = fresh_positions + colocated_positions

    candidates = []
    for i, position_mi in enumerate(positions):
        price_cents = rng.randint(params.min_price_cents, params.max_price_cents)
        candidates.append(
            Candidate(
                name=f"P{i}",
                opis_id=i,
                price_per_gallon=Decimal(price_cents) / Decimal(100),
                distance_from_start_mi=Decimal(position_mi),
            )
        )
    return candidates


class PruneReductionGuardTests(SimpleTestCase):
    """D-36's reduction guards -- a plain SimpleTestCase, not a Hypothesis
    property, so it runs identically -- same corpus, same result -- on
    every commit.

    `prune(x) -> x` (a no-op) satisfies every soundness property in this
    module vacuously: containment, feasibility, and cost all hold
    trivially when nothing is ever removed. D-17's floor existed to catch
    exactly that; this class replaces the one-sided floor with a
    closed-form equality (test A) derived from prune.py's own maximality
    theorem, which catches over-pruning as well as a no-op, plus a seeded
    dense-corpus floor (test B) so a regression at realistic scale still
    fails.

    The report-only twelve-corridor command is plan 17-06's deliverable
    and must never run in CI (D-17); this class is the CI-enforcing half
    of that pair.
    """

    def test_survivor_set_equals_the_independently_computed_prefix_minima(self):
        """D-36's primary guard. On a route shorter than the tank range
        with pairwise-distinct prices at distinct positions, every
        station's own supply interval reaches FINISH (pos + T >= L holds
        for all of them, since T alone already exceeds L) -- so the cover
        condition's tail branch is available to every candidate, and the
        survivor set is exactly the strict price prefix-minima in total
        order. This is a theorem of prune.py's own maximality section
        ("route shorter than tank range" collapses containment to "no
        earlier-ranked station is cheaper-or-equal"), not a number tuned
        to a measurement -- so it is asserted as set equality against an
        independently computed expectation. Equality is the point: a
        no-op prune fails it by retaining everything, and an over-eager
        prune fails it by dropping a true prefix-minimum -- exactly the
        half a one-sided floor could never catch.
        """
        total_route_mi = Decimal(1000)
        tank_range_mi = Decimal(1500)
        self.assertLess(
            total_route_mi,
            tank_range_mi,
            "this test's geometry requires total_route_mi < tank_range_mi "
            "so every station's cover condition tail branch fires",
        )
        station_count = 300

        rng = random.Random(PRUNE_CORPUS_PARAMS.seed)
        positions = rng.sample(range(1, int(total_route_mi)), station_count)
        # A price population wide enough that random.Random.sample's
        # without-replacement draw guarantees distinctness by
        # construction, not by luck.
        price_cents_population = range(100, 100 + 50 * station_count)
        price_cents = rng.sample(price_cents_population, station_count)

        candidates = [
            Candidate(
                name=f"S{i}",
                opis_id=i,
                price_per_gallon=Decimal(price_cents[i]) / Decimal(100),
                distance_from_start_mi=Decimal(positions[i]),
            )
            for i in range(station_count)
        ]

        result = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        ordered = sorted(candidates, key=_sort_key)
        expected = []
        running_min = None
        for candidate in ordered:
            if running_min is None or candidate.price_per_gallon < running_min:
                expected.append(candidate)
                running_min = candidate.price_per_gallon

        self.assertEqual(
            set(result),
            set(expected),
            f"survivor set does not equal the independently computed "
            f"strict price prefix-minima on a {station_count}-station "
            f"corpus shorter than the tank range (total_route_mi="
            f"{total_route_mi}, tank_range_mi={tank_range_mi}); got "
            f"{len(result)} survivors, expected {len(expected)}",
        )

        # A statement about the corpus, not about the prune: for a few
        # hundred randomly ordered distinct prices, the expected
        # prefix-minima count is on the order of the harmonic number
        # (single digits) -- far smaller than the station count. This
        # generous ceiling is what makes the equality assertion above
        # demonstrably load-bearing rather than vacuous.
        self.assertLess(
            len(expected),
            30,
            f"expected prefix-minima count ({len(expected)}) is not far "
            f"smaller than station_count={station_count} -- this is a "
            f"statement about the corpus's random price ordering, not "
            f"about the prune; if this fails, the corpus needs "
            f"attention, not prune_dominated_candidates",
        )

    def test_dense_corpus_reduction_rate_exceeds_floor(self):
        """D-36's seeded dense-corpus floor. A prune that has silently
        become a no-op scores exactly 0 here -- check that first if this
        ever fails. Do not adjust the seed, the station count, or any
        other PRUNE_CORPUS_PARAMS field to make this pass (D-14/D-17).
        """
        candidates = build_prune_corpus()
        retained = prune_dominated_candidates(
            candidates,
            tank_range_mi=PRUNE_CORPUS_PARAMS.tank_range_mi,
            total_route_mi=PRUNE_CORPUS_PARAMS.total_route_mi,
        )
        reduction_rate = Decimal(1) - Decimal(len(retained)) / Decimal(len(candidates))

        self.assertGreater(
            reduction_rate,
            PRUNE_REDUCTION_FLOOR,
            f"measured reduction rate {reduction_rate} over "
            f"PRUNE_CORPUS_PARAMS (seed={PRUNE_CORPUS_PARAMS.seed}, "
            f"station_count={PRUNE_CORPUS_PARAMS.station_count}) did not "
            f"exceed PRUNE_REDUCTION_FLOOR={PRUNE_REDUCTION_FLOOR}. A "
            f"prune that has silently become a no-op scores exactly 0 "
            f"here -- check that first. Do not adjust the seed, the "
            f"station count, or any other PRUNE_CORPUS_PARAMS field to "
            f"make this pass (D-14/D-17); candidate_count="
            f"{len(candidates)}, retained_count={len(retained)}",
        )

    def test_build_prune_corpus_is_deterministic_across_two_calls(self):
        """Without random.Random(seed) freshly seeded per call, a corpus
        that quietly consumed global RNG state would make both guards
        above unreproducible."""
        first = build_prune_corpus()
        second = build_prune_corpus()
        self.assertEqual(
            [(c.opis_id, c.price_per_gallon, c.distance_from_start_mi) for c in first],
            [(c.opis_id, c.price_per_gallon, c.distance_from_start_mi) for c in second],
            "build_prune_corpus() returned different corpora across two "
            "consecutive calls -- it must be freshly seeded from "
            "PRUNE_CORPUS_PARAMS.seed on every call, never consuming the "
            "global random module's state.",
        )


# ---------------------------------------------------------------------------
# D-03's sibling apparatus: a mixed-provenance corpus, its own reduction
# floor, and the mutation-checked anti-vacuity guard proving condition 3
# actually fires. Mirrors PruneCorpusParams/PRUNE_CORPUS_PARAMS/
# PRUNE_REDUCTION_FLOOR/build_prune_corpus() above precisely, so the ONLY
# structural difference between the two corpora is provenance.


@dataclass(frozen=True)
class PruneMixedCorpusParams:
    """D-03's sibling to PruneCorpusParams above -- a distinct dataclass
    (rather than an `estimate_share` field bolted onto PruneCorpusParams)
    so PRUNE_CORPUS_PARAMS itself stays byte-unchanged and this corpus's
    provenance-specific field never leaks into its all-recorded shape.
    Every field fixed here, before the mixed-corpus reduction rate below
    was ever measured, following PRUNE_CORPUS_PARAMS' own D-14 precedent
    exactly.
    """

    seed: int
    station_count: int
    total_route_mi: Decimal
    tank_range_mi: Decimal
    min_price_cents: int
    max_price_cents: int
    colocated_share: Decimal
    estimate_share: Decimal


# Every field except seed and estimate_share mirrors PRUNE_CORPUS_PARAMS
# exactly, so provenance is the ONLY variable between the two corpora.
# seed=20260807 is distinct from PRUNE_CORPUS_PARAMS.seed (20260731) and
# from TAG_SEED (test_trust_margin_rule.py's own is_tagged() selector
# seed) so this corpus's random.Random(seed) draw never interleaves with
# either -- today's date, following both precedents' own convention.
# estimate_share is pinned to TAG_SHARE_LADDER[1] (test_trust_margin_rule
# .py, D-11's middle rung), so the tagging share is one this phase already
# committed to elsewhere, never invented here.
PRUNE_MIXED_CORPUS_PARAMS = PruneMixedCorpusParams(
    seed=20260807,
    station_count=500,
    total_route_mi=Decimal(2200),
    tank_range_mi=Decimal(1050),
    min_price_cents=100,
    max_price_cents=600,
    colocated_share=Decimal("0.10"),
    estimate_share=TAG_SHARE_LADDER[1],
)

# D-03: measured ONCE against PRUNE_MIXED_CORPUS_PARAMS above -- 47.20%
# (264/500 retained, vs 259/500 for the same positions/prices with every
# provenance forced to the recorded value) reduction, recorded verbatim in
# 21-05-SUMMARY.md. Set at roughly a third of that measured rate
# (0.14 / 0.472 ~= 0.30), following PRUNE_REDUCTION_FLOOR's own precedent
# exactly. This floor is EXPECTED, and CORRECT, to sit below
# PRUNE_REDUCTION_FLOOR (0.15): condition 3 deliberately retains MORE on a
# mixed-provenance corpus than the margin-free rule retains on an
# all-recorded one, so a lower reduction rate here is the predicate
# working, not a regression. Do not adjust PRUNE_MIXED_CORPUS_PARAMS or
# this floor to make a future run pass -- see the failure message on the
# guard below.
PRUNE_MIXED_REDUCTION_FLOOR = Decimal("0.14")


def build_mixed_provenance_corpus(*, params=PRUNE_MIXED_CORPUS_PARAMS):
    """Deterministically build a mixed-provenance candidate corpus,
    mirroring build_prune_corpus() exactly for position/price generation
    (a fresh random.Random(params.seed), never the global module) and
    layering D-11's is_tagged() selector on top -- via tagged_candidates()
    from test_trust_margin_rule.py -- to assign provenance. Reproducible
    across processes: is_tagged() is hash-based (blake2b), not
    RNG-derived, so tagging never depends on random.Random's internal
    state or draw order.
    """
    rng = random.Random(params.seed)
    n_colocated = int(params.station_count * params.colocated_share)
    n_fresh = params.station_count - n_colocated

    fresh_positions = rng.sample(range(1, int(params.total_route_mi)), n_fresh)
    colocated_positions = [rng.choice(fresh_positions) for _ in range(n_colocated)]
    positions = fresh_positions + colocated_positions

    candidates = []
    for i, position_mi in enumerate(positions):
        price_cents = rng.randint(params.min_price_cents, params.max_price_cents)
        candidates.append(
            Candidate(
                name=f"M{i}",
                opis_id=i,
                price_per_gallon=Decimal(price_cents) / Decimal(100),
                distance_from_start_mi=Decimal(position_mi),
            )
        )
    return tagged_candidates(candidates, params.estimate_share)


class PruneMixedCorpusReductionGuardTests(SimpleTestCase):
    """D-03's sibling reduction-floor guard, mirroring PruneReductionGuardTests'
    shape exactly: a plain SimpleTestCase (not a Hypothesis property) so it
    runs identically -- same corpus, same result -- on every commit.
    """

    def test_mixed_corpus_reduction_rate_exceeds_floor(self):
        """A prune that has silently become a no-op, or one where
        condition 3 never actually restricts anything, both score lower
        here than the genuine measured rate. Do not adjust
        PRUNE_MIXED_CORPUS_PARAMS or PRUNE_MIXED_REDUCTION_FLOOR to make
        this pass (D-14/D-17)."""
        candidates = build_mixed_provenance_corpus()
        retained = prune_dominated_candidates(
            candidates,
            tank_range_mi=PRUNE_MIXED_CORPUS_PARAMS.tank_range_mi,
            total_route_mi=PRUNE_MIXED_CORPUS_PARAMS.total_route_mi,
        )
        reduction_rate = Decimal(1) - Decimal(len(retained)) / Decimal(len(candidates))

        self.assertGreater(
            reduction_rate,
            PRUNE_MIXED_REDUCTION_FLOOR,
            f"measured mixed-corpus reduction rate {reduction_rate} did not "
            f"exceed PRUNE_MIXED_REDUCTION_FLOOR={PRUNE_MIXED_REDUCTION_FLOOR}. "
            "Do not adjust PRUNE_MIXED_CORPUS_PARAMS or this floor to make a "
            f"future run pass; candidate_count={len(candidates)}, "
            f"retained_count={len(retained)}",
        )

    def test_build_mixed_provenance_corpus_is_deterministic_across_two_calls(self):
        """Without random.Random(seed) freshly seeded per call, a corpus
        that quietly consumed global RNG state would make the guard above
        unreproducible."""
        first = build_mixed_provenance_corpus()
        second = build_mixed_provenance_corpus()
        self.assertEqual(
            [
                (c.opis_id, c.price_per_gallon, c.distance_from_start_mi, c.price_source)
                for c in first
            ],
            [
                (c.opis_id, c.price_per_gallon, c.distance_from_start_mi, c.price_source)
                for c in second
            ],
            "build_mixed_provenance_corpus() returned different corpora "
            "across two consecutive calls -- it must be freshly seeded on "
            "every call, never consuming global RNG state.",
        )


class PruneMarginBlockedRetentionTests(SimpleTestCase):
    """D-03's mutation check and the anti-vacuity heart of this plan.

    Without this class, a bug making condition 3 always TRUE -- i.e. the
    margin never actually restricting anything -- is indistinguishable
    from correct behaviour on an all-recorded corpus: every other test in
    this module still passes, because condition 3 never fires there
    either way, correct or broken.

    Confirmed by hand (recorded verbatim in 21-05-SUMMARY.md): temporarily
    editing prune.py so the provenance condition is always satisfied made
    this class fail, specifically naming PruneMarginBlockedRetentionTests,
    and reverting restored a byte-identical prune.py (confirmed via
    `git diff --stat`, empty relative to this plan's Task 1 commit) and a
    green suite.
    """

    def test_mixed_corpus_retained_set_is_a_strict_superset_of_the_forced_real_control(self):
        """The primary evidence: prune the mixed corpus, then prune the
        SAME corpus with every provenance forced to the recorded value,
        and assert the first retained set STRICTLY contains the second.
        Equal length is a FAILURE here, not a pass -- that is exactly what
        a condition-3-never-fires bug would produce.
        """
        mixed = build_mixed_provenance_corpus()
        forced_real = [replace(c, price_source="opis_indexed") for c in mixed]

        mixed_retained = prune_dominated_candidates(
            mixed,
            tank_range_mi=PRUNE_MIXED_CORPUS_PARAMS.tank_range_mi,
            total_route_mi=PRUNE_MIXED_CORPUS_PARAMS.total_route_mi,
        )
        control_retained = prune_dominated_candidates(
            forced_real,
            tank_range_mi=PRUNE_MIXED_CORPUS_PARAMS.tank_range_mi,
            total_route_mi=PRUNE_MIXED_CORPUS_PARAMS.total_route_mi,
        )

        mixed_ids = {c.opis_id for c in mixed_retained}
        control_ids = {c.opis_id for c in control_retained}

        self.assertTrue(
            control_ids.issubset(mixed_ids),
            "the margin-aware retained set on the mixed-provenance corpus "
            "must contain every opis_id the forced-real control retains -- "
            f"missing={control_ids - mixed_ids!r}",
        )
        self.assertGreater(
            len(mixed_ids),
            len(control_ids),
            "the margin-aware retained set must be a STRICT superset of "
            "the forced-real control's retained set -- equal length is a "
            "FAILURE here, not a pass; a prune where condition 3 never "
            f"actually fires would retain the identical set. "
            f"mixed_count={len(mixed_ids)}, control_count={len(control_ids)}",
        )

    def test_earlier_cheap_estimate_and_later_dear_real_tail_station_both_survive(self):
        """Hand-built witness (pass 2, the tail branch): a cheap
        estimate-priced station sits earlier in the total order than a
        dearer real-priced station, and both reach FINISH. Under the
        pre-margin rule the real-priced station would have been removed
        -- the cheaper estimate-priced one would have qualified as its
        dominator.
        """
        candidates = [
            Candidate(
                name="Cheap Estimate",
                opis_id=1,
                price_per_gallon=Decimal("1.00"),
                distance_from_start_mi=Decimal(10),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
            Candidate(
                name="Dear Real",
                opis_id=2,
                price_per_gallon=Decimal("2.00"),
                distance_from_start_mi=Decimal(20),
            ),
        ]
        # tank_range_mi == total_route_mi so every position's own supply
        # interval trivially reaches FINISH, isolating pass 2's tail
        # branch (the two distinct positions never collide in pass 1).
        tank_range_mi = Decimal(1000)
        total_route_mi = Decimal(1000)

        result = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        self.assertEqual(
            {c.opis_id for c in result},
            {1, 2},
            "the dearer real-priced tail-reaching station (opis_id=2) must "
            "survive despite an earlier, cheaper, estimate-priced station "
            "(opis_id=1) also reaching FINISH -- under the pre-margin rule "
            f"opis_id=2 would have been removed. Got "
            f"{[c.opis_id for c in result]!r}",
        )

    def test_cheaper_co_located_estimate_and_dearer_co_located_real_both_survive_pass_one(self):
        """Hand-built witness (pass 1, the co-located branch): at a shared
        position, the cheaper station is estimate-priced and the dearer is
        real-priced. Both must survive -- the estimate-priced station
        because it is cheapest at the position, and the real-priced
        station because its only cheaper-or-equal co-located rival is
        estimate-priced, which may not dominate it.
        """
        candidates = [
            Candidate(
                name="Cheap Co-located Estimate",
                opis_id=3,
                price_per_gallon=Decimal("1.00"),
                distance_from_start_mi=Decimal(50),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
            Candidate(
                name="Dear Co-located Real",
                opis_id=4,
                price_per_gallon=Decimal("1.50"),
                distance_from_start_mi=Decimal(50),
            ),
        ]
        # A short tank range keeps neither station reaching FINISH, so
        # this witness isolates pass 1 -- pass 2 never touches either
        # candidate.
        tank_range_mi = Decimal(10)
        total_route_mi = Decimal(1000)

        result = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        self.assertEqual(
            {c.opis_id for c in result},
            {3, 4},
            "both co-located stations must survive pass 1 -- the "
            "estimate-priced one (opis_id=3) as the position's cheapest, "
            "and the real-priced one (opis_id=4) because an "
            "estimate-priced station may not dominate it. Got "
            f"{[c.opis_id for c in result]!r}",
        )

    def test_cheaper_co_located_real_still_removes_dearer_co_located_estimate(self):
        """Hand-built COUNTER-witness: at a shared position, the cheaper
        station is real-priced and the dearer is estimate-priced. The
        estimate-priced station must still be removed --
        real-dominates-anything is unchanged. This is what rules out a
        blanket dedup-within-provenance-class implementation, which would
        pass the two witnesses above while quietly abandoning legitimate
        reduction: such an implementation would wrongly retain BOTH
        stations here too.
        """
        candidates = [
            Candidate(
                name="Cheap Co-located Real",
                opis_id=5,
                price_per_gallon=Decimal("1.00"),
                distance_from_start_mi=Decimal(70),
            ),
            Candidate(
                name="Dear Co-located Estimate",
                opis_id=6,
                price_per_gallon=Decimal("1.50"),
                distance_from_start_mi=Decimal(70),
                price_source=ESTIMATE_PRICE_SOURCE,
            ),
        ]
        tank_range_mi = Decimal(10)
        total_route_mi = Decimal(1000)

        result = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        self.assertEqual(
            {c.opis_id for c in result},
            {5},
            "the dearer co-located estimate-priced station (opis_id=6) "
            "must still be removed by the cheaper co-located real-priced "
            "station (opis_id=5) -- real-dominates-anything is unchanged. "
            "A blanket dedup-within-provenance-class implementation would "
            f"wrongly retain both. Got {[c.opis_id for c in result]!r}",
        )


# ---------------------------------------------------------------------------
# D-12/PIPE-02 (plan 22-04): the identically-priced-cluster prune-soundness
# apparatus. D-09 assigns every same-region Overture row the identical
# retail_price, so a dense cluster of identically-priced candidates is the
# literal shape the gap-fill import creates, in a corridor stretch none of
# the twelve pinned corridors currently reaches -- this module is the only
# place that shape gets proven sound before the import lands.


@dataclass(frozen=True)
class PruneClusterCorpusParams:
    """The D-12 single source of truth for the identically-priced-cluster
    corpus below, mirroring PruneCorpusParams/PruneMixedCorpusParams'
    precedent exactly: every field fixed here, before the first retention
    count is ever computed, and never revisited after seeing a result
    (D-14/D-17).

    Each generated route carries exactly ONE identically-priced cluster of
    cluster_size stations, at distinct positions spaced cluster_spacing_mi
    apart -- never co-located, so pass 1's co-located dedup never touches
    them and only pass 2's tail-reach branch (or its absence) decides what
    survives. Routes alternate, by index, between two placements, each with
    its OWN total_route_mi -- a single shared route length cannot make both
    shapes simultaneously reachable from the origin AND correctly tail/
    non-tail, so the two placements are genuinely different route lengths,
    not merely different station positions on one shared route:

    * "non-tail" (even index): starts at non_tail_start_mi, well inside
      tank_range_mi of the origin (reachable directly) but positioned so
      pos_S + tank_range_mi < non_tail_total_route_mi holds for EVERY
      member -- condition 2 of the domination rule (prune.py) then fails
      for every pair inside the cluster (neither pos_B == pos_A, distinct
      positions, nor pos_B + T >= L, no member reaches FINISH), so no
      member can ever dominate another. All cluster_size members survive.
      (The route itself is infeasible past the cluster -- nothing else is
      on it -- which is fine: this shape's evidence is the retained set,
      not a feasible plan; see IdenticalPriceClusterPruneSoundnessTests'
      own soundness assertion, which handles infeasibility symmetrically.)

    * "tail" (odd index): starts at tail_start_mi, ALSO reachable directly
      from the origin, but positioned so pos_S + tank_range_mi >=
      tail_total_route_mi holds for EVERY member (even the earliest) --
      every member is then eligible as a pass-2 dominator/dominated pair;
      since every member shares one price, the total order's first
      (earliest-position) member sets the running minimum and every later
      member satisfies price_B <= price_A (equality), so it is dominated
      and removed. Exactly ONE member -- the earliest -- survives. This
      shape IS feasible end to end (a genuine single-stop plan exists),
      which is what gives the soundness assertion real cost content
      rather than a trivial infeasibility match.

    cluster_size is capped at MAX_STATIONS so a route's full candidate
    list stays inside the oracle's own subset-enumeration ceiling (see
    IdenticalPriceClusterPruneSoundnessTests' differential assertion,
    which calls the oracle over the FULL, unpruned candidate list).
    """

    seed: int
    n_routes: int
    cluster_size: int
    cluster_spacing_mi: Decimal
    tank_range_mi: Decimal
    non_tail_total_route_mi: Decimal
    non_tail_start_mi: Decimal
    tail_total_route_mi: Decimal
    tail_start_mi: Decimal
    min_price_cents: int
    max_price_cents: int


# The D-12 single shared instance. n_routes=40 is even (an exact 20/20
# non-tail/tail split, so PRUNE_CLUSTER_RETENTION_EXPECTATION below has no
# rounding ambiguity). cluster_size=5 sits comfortably under MAX_STATIONS=6
# (imported above from test_solver_fixed_charge_optimality), leaving the
# oracle's own subset enumeration well inside its budget.
#
# tank_range_mi=500, non_tail_start_mi=50: the non-tail cluster's own last
# member (50 + (5-1)*5 = 70) plus tank_range_mi is 570, comfortably short
# of non_tail_total_route_mi=2000 -- non-tail by construction, not by luck.
#
# tail_start_mi=250, tail_total_route_mi=700: the tail cluster's own
# EARLIEST member (250 + 500 = 750 >= 700) already reaches FINISH, so
# every member does -- comfortably tail by construction too. 250 is also
# <= tank_range_mi, so the whole cluster is reachable directly from the
# origin at the default starting_fuel=1 the oracle assumes when it is not
# passed explicitly -- this is what makes the tail shape genuinely
# feasible end to end, not merely reach-tagged on paper.
PRUNE_CLUSTER_CORPUS_PARAMS = PruneClusterCorpusParams(
    seed=20260808,
    n_routes=40,
    cluster_size=5,
    cluster_spacing_mi=Decimal("5"),
    tank_range_mi=Decimal("500"),
    non_tail_total_route_mi=Decimal("2000"),
    non_tail_start_mi=Decimal("50"),
    tail_total_route_mi=Decimal("700"),
    tail_start_mi=Decimal("250"),
    min_price_cents=300,
    max_price_cents=500,
)


def build_price_cluster_corpus(*, params=PRUNE_CLUSTER_CORPUS_PARAMS):
    """Deterministically build params.n_routes routes, each a single
    identically-priced cluster of params.cluster_size stations, from a
    fresh random.Random(params.seed) -- never the global random module, so
    two consecutive calls return byte-identical corpora (proven by
    test_build_price_cluster_corpus_is_deterministic_across_two_calls
    below), mirroring build_prune_corpus()/build_mixed_provenance_corpus()'s
    own determinism precedent exactly.

    Route index parity decides placement (see PruneClusterCorpusParams'
    own docstring for the full geometric argument): even index -> non-tail,
    odd index -> tail. Each route's cluster provenance is drawn once per
    route (recorded or estimate, D-09's own regional-uniformity rule -- one
    tag per region, never a per-station mix within one cluster) and its
    price once per route -- every member of that route's cluster shares
    both. Returns a list of (candidates, total_route_mi, is_tail) tuples.
    """
    rng = random.Random(params.seed)

    routes = []
    for i in range(params.n_routes):
        is_tail = i % 2 == 1
        start_mi = params.tail_start_mi if is_tail else params.non_tail_start_mi
        total_route_mi = params.tail_total_route_mi if is_tail else params.non_tail_total_route_mi

        price_cents = rng.randint(params.min_price_cents, params.max_price_cents)
        price = Decimal(price_cents) / Decimal(100)
        price_source = ESTIMATE_PRICE_SOURCE if rng.random() < 0.5 else "opis_indexed"

        candidates = [
            Candidate(
                name=f"PC{i}-{j}",
                opis_id=i * params.cluster_size + j,
                price_per_gallon=price,
                distance_from_start_mi=start_mi + Decimal(j) * params.cluster_spacing_mi,
                price_source=price_source,
            )
            for j in range(params.cluster_size)
        ]
        routes.append((candidates, total_route_mi, is_tail))
    return routes


def _expected_cluster_retention(params):
    """Pure function of PruneClusterCorpusParams, derived directly from
    the domination rule in routing/services/prune.py (condition 2) --
    never measured. Half of params.n_routes (the tail-placed routes)
    retain exactly ONE member each (the earliest position dominates every
    later, identically-priced, tail-reaching sibling); the other half (the
    non-tail-placed routes) retain every member -- condition 2 fails for
    every pair inside a non-tail cluster, so nothing there can ever be
    dominated. See PruneClusterCorpusParams' own docstring for the full
    geometric argument this reduces to arithmetic.
    """
    n_tail_routes = params.n_routes // 2
    n_non_tail_routes = params.n_routes - n_tail_routes
    return n_tail_routes * 1 + n_non_tail_routes * params.cluster_size


# Computed, not hand-typed, from PRUNE_CLUSTER_CORPUS_PARAMS above -- a
# floor expressed as "removed at least N" (PRUNE_REDUCTION_FLOOR's own
# shape) would pass a no-op prune(x) -> x just as readily as it would pass
# an over-pruning implementation that also strips a non-tail cluster down
# to one survivor; only an exact equality, derived from the rule rather
# than measured from a run, catches both directions at once.
PRUNE_CLUSTER_RETENTION_EXPECTATION = _expected_cluster_retention(PRUNE_CLUSTER_CORPUS_PARAMS)


class IdenticalPriceClusterPruneSoundnessTests(SimpleTestCase):
    """D-12/PIPE-02 (plan 22-04): the prune is sound, and provably
    non-vacuous, on the exact price-degeneracy the gap-fill import is
    about to create (D-09). Three assertions:

    1. Differential soundness against this module's own imported oracle
       (optimal_fixed_charge_plan), at both PENALTY_ANCHORS rungs and at
       the adopted TRUST_MARGIN_USD default (ADOPTED_MARGIN_USD) -- never
       argued, always checked.
    2. A closed-form retention equality (PRUNE_CLUSTER_RETENTION_EXPECTATION),
       catching both a no-op prune (which would over-retain the tail
       clusters relative to the derived expectation) and an over-pruning
       implementation (which would under-retain the non-tail clusters) --
       the two directions a one-sided floor (PRUNE_REDUCTION_FLOOR's own
       shape) cannot both catch at once.
    3. A permanent witness, reusing SLIVER_WITNESS_EQUAL_PRICE's own shape,
       proving a chain of identically-priced stations -- none of which
       individually reaches FINISH except the last -- all survive, because
       only the last is ever eligible to dominate or be dominated.
    """

    def test_prune_never_raises_the_optimum_on_identically_priced_clusters(self):
        margins = (Decimal(0), ADOPTED_MARGIN_USD)
        routes = build_price_cluster_corpus()

        for route_index, (candidates, total_route_mi, is_tail) in enumerate(routes):
            self.assertLessEqual(
                len(candidates),
                MAX_STATIONS,
                f"route {route_index}: build_price_cluster_corpus() drew more "
                f"than MAX_STATIONS candidates: {candidates!r} -- the oracle's "
                f"subset enumeration cannot terminate over this input.",
            )

            retained = prune_dominated_candidates(
                candidates,
                tank_range_mi=PRUNE_CLUSTER_CORPUS_PARAMS.tank_range_mi,
                total_route_mi=total_route_mi,
            )

            for penalty in PENALTY_ANCHORS:
                for margin in margins:
                    unpruned_plan = optimal_fixed_charge_plan(
                        candidates,
                        total_route_mi,
                        penalty=penalty,
                        tank_range_mi=PRUNE_CLUSTER_CORPUS_PARAMS.tank_range_mi,
                        trust_margin=margin,
                    )
                    pruned_plan = optimal_fixed_charge_plan(
                        retained,
                        total_route_mi,
                        penalty=penalty,
                        tank_range_mi=PRUNE_CLUSTER_CORPUS_PARAMS.tank_range_mi,
                        trust_margin=margin,
                    )

                    context = (
                        f"route_index={route_index}, is_tail={is_tail}, "
                        f"candidates={candidates!r}, retained={retained!r}, "
                        f"total_route_mi={total_route_mi}, penalty={penalty}, "
                        f"margin={margin}"
                    )

                    self.assertEqual(
                        unpruned_plan is None,
                        pruned_plan is None,
                        f"feasibility verdicts disagree between pruned and "
                        f"unpruned solves on an identically-priced cluster "
                        f"route; {context}",
                    )
                    if unpruned_plan is None:
                        continue

                    self.assertLessEqual(
                        abs(unpruned_plan.objective - pruned_plan.objective),
                        COST_TOLERANCE,
                        f"pruned objective ({pruned_plan.objective}) differs "
                        f"from the unpruned objective ({unpruned_plan.objective}) "
                        f"beyond COST_TOLERANCE on an identically-priced "
                        f"cluster route -- a removed candidate raised the "
                        f"optimum; {context}",
                    )

    def test_retained_count_equals_the_closed_form_expectation(self):
        """Anti-vacuity by closed-form equality, not by floor alone (see
        this class's own docstring). `prune(x) -> x` passes every
        soundness property in this module vacuously; only this equality --
        derived from the rule, never measured -- catches it, and catches
        over-pruning too.
        """
        routes = build_price_cluster_corpus()
        total_retained = 0
        for candidates, total_route_mi, _is_tail in routes:
            retained = prune_dominated_candidates(
                candidates,
                tank_range_mi=PRUNE_CLUSTER_CORPUS_PARAMS.tank_range_mi,
                total_route_mi=total_route_mi,
            )
            total_retained += len(retained)

        self.assertEqual(
            total_retained,
            PRUNE_CLUSTER_RETENTION_EXPECTATION,
            f"retained {total_retained} candidates across "
            f"PRUNE_CLUSTER_CORPUS_PARAMS.n_routes="
            f"{PRUNE_CLUSTER_CORPUS_PARAMS.n_routes} identically-priced-"
            f"cluster routes; expected exactly "
            f"PRUNE_CLUSTER_RETENTION_EXPECTATION="
            f"{PRUNE_CLUSTER_RETENTION_EXPECTATION}. A reduction floor "
            f"alone ('removed at least N') cannot catch this: prune(x) -> "
            f"x passes every floor and every soundness property in this "
            f"module vacuously, and an over-pruning implementation that "
            f"also strips a non-tail cluster below its full size would "
            f"pass a floor too -- only this closed-form equality catches "
            f"both directions.",
        )

    def test_build_price_cluster_corpus_is_deterministic_across_two_calls(self):
        """Without random.Random(seed) freshly seeded per call, a corpus
        that quietly consumed global RNG state would make the two guards
        above unreproducible."""
        first = build_price_cluster_corpus()
        second = build_price_cluster_corpus()
        self.assertEqual(
            [
                [
                    (c.opis_id, c.price_per_gallon, c.distance_from_start_mi, c.price_source)
                    for c in candidates
                ]
                for candidates, _total, _is_tail in first
            ],
            [
                [
                    (c.opis_id, c.price_per_gallon, c.distance_from_start_mi, c.price_source)
                    for c in candidates
                ]
                for candidates, _total, _is_tail in second
            ],
            "build_price_cluster_corpus() returned different corpora "
            "across two consecutive calls -- it must be freshly seeded on "
            "every call, never consuming global RNG state.",
        )

    def test_equal_priced_chain_survives_when_only_the_last_member_reaches_finish(self):
        """The permanent relay witness (assertion 3). Reuses
        SLIVER_WITNESS_EQUAL_PRICE's own shape -- a chain of stations at
        1-mile spacing near the start of the route -- generalized to a
        chain_size-station case. Only the LAST member's own supply
        interval reaches FINISH (pos_S + tank_range_mi >= total_route_mi);
        every earlier member's does not. So only the last member is ever
        eligible to dominate or be dominated under condition 2 -- and there
        is nothing before it that is ALSO tail-reaching to dominate it, and
        nothing after it in the chain -- so no domination fires anywhere in
        the chain and every member survives. SLIVER_WITNESS_EQUAL_PRICE
        (this module, above) is the two-station instance of exactly this
        shape that first proved a naive "reach-sliver" rule wrong; this
        witness pins the general chain_size-station case permanently.
        """
        chain_size = 5
        tank_range_mi = Decimal(100)
        total_route_mi = Decimal(chain_size) + tank_range_mi
        candidates = [
            _candidate(opis_id=i, price="1.00", position=i + 1, name=f"R{i}")
            for i in range(chain_size)
        ]

        for i in range(chain_size - 1):
            self.assertLess(
                candidates[i].distance_from_start_mi + tank_range_mi,
                total_route_mi,
                f"witness construction error: chain member {i} reaches "
                f"FINISH but only the last member should.",
            )
        self.assertGreaterEqual(
            candidates[-1].distance_from_start_mi + tank_range_mi,
            total_route_mi,
            "witness construction error: the last chain member must reach "
            "FINISH.",
        )

        result = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        self.assertEqual(
            {c.opis_id for c in result},
            {c.opis_id for c in candidates},
            f"the equal-priced relay chain must survive prune in full -- "
            f"only the last member's own supply interval reaches FINISH, "
            f"so no member is ever eligible to dominate another. Got "
            f"{sorted(c.opis_id for c in result)!r}, expected "
            f"{sorted(c.opis_id for c in candidates)!r}.",
        )


@dataclass(frozen=True)
class PrunePenaltyDominationCorpusParams:
    """The D-06 single source of truth for the penalty-domination
    (condition 4) anti-vacuity corpus below, mirroring PruneCorpusParams/
    PruneMixedCorpusParams/PruneClusterCorpusParams' precedent exactly:
    every field fixed here, before the first retention count is ever
    computed, and never revisited after seeing a result (D-14).

    Each generated route carries exactly two stations at distinct
    positions: an earlier, PRICIER station B at b_position_mi, and a
    later, CHEAPER station A at b_position_mi + a_spacing_mi -- the
    condition-4 shape (4a requires price_B >= price_A), the mirror image
    of PruneClusterCorpusParams' equal-priced-dominator shape, where the
    EARLIER station is the cheaper one. Both stations sit so that
    pos_B + tank_range_mi >= total_route_mi holds for B, and since
    pos_A > pos_B the same inequality holds a fortiori for A -- both are
    tail-reaching, satisfying condition 2's geometry the way
    PruneClusterCorpusParams' own "tail" placement does, so both are
    eligible dominator/dominated candidates under prune_dominated_
    candidates' pass 2.

    Routes alternate, by index, between two price gaps applied to the
    SAME geometry -- never a different tank_range_mi, total_route_mi or
    position, so price is the only variable that decides the split:

    * "clears the bar" (even index): the price gap (price_B - price_A) is
      price_step_above_cents, chosen so max_saving = price_gap *
      tank_range_mi / mpg sits far ABOVE penalty. Condition 4b's own
      test (`max_saving < penalty`) is false, so A is not removed by
      condition 4 -- and since A is cheaper than B, conditions 1-3 (which
      require the dominator to be cheaper-or-equal) never apply either.
      Both B and A survive: 2 retained.

    * "fails the bar" (odd index): the price gap is
      price_step_below_cents, chosen so max_saving sits far BELOW
      penalty. Condition 4b's test is true, and 4a (price_B >= price_A)
      and 4c (both stations real-priced, so margin_B == margin_A == 0)
      both hold trivially, so A is removed by condition 4. Only B
      survives: 1 retained.

    Both gaps are chosen with a wide, unambiguous margin from `penalty`
    (roughly 14.3x above and 1/7th below in the pinned instance below)
    rather than sitting near the boundary -- the boundary itself is the
    hand-built witness's job (the new test class's own Assertion 3), not
    this corpus's.
    """

    seed: int
    n_routes: int
    b_position_mi: Decimal
    a_spacing_mi: Decimal
    tank_range_mi: Decimal
    total_route_mi: Decimal
    mpg: Decimal
    penalty: Decimal
    min_price_b_cents: int
    max_price_b_cents: int
    price_step_above_cents: int
    price_step_below_cents: int


# The D-06 single shared instance. seed is today's date, distinct from
# every other corpus's seed in this module. n_routes=40 is even (an
# exact 20/20 clears/fails split, so PRUNE_PENALTY_DOMINATION_RETENTION_
# EXPECTATION below has no rounding ambiguity -- mirroring PRUNE_CLUSTER_
# CORPUS_PARAMS.n_routes=40 exactly).
#
# tank_range_mi=500, total_route_mi=700, b_position_mi=250: identical to
# PRUNE_CLUSTER_CORPUS_PARAMS' own "tail" placement (250 + 500 = 750 >=
# 700), reused rather than re-derived -- both stations are tail-reaching
# by the same already-verified arithmetic. a_spacing_mi=5 mirrors
# PRUNE_CLUSTER_CORPUS_PARAMS.cluster_spacing_mi.
#
# mpg=1, penalty=Decimal("35") == PENALTY_ANCHORS[1], chosen deliberately
# so the closed-form retention below (evaluated once, at this pinned
# penalty) is directly comparable to the new test class's own
# PENALTY_ANCHORS sweep (Task 2).
#
# price_step_above_cents=100 ($1.00): max_saving = 1.00 * 500 / 1 = $500,
# ~14.3x penalty -- clears the bar by a wide margin.
# price_step_below_cents=1 ($0.01): max_saving = 0.01 * 500 / 1 = $5,
# 1/7 of penalty -- misses the bar by a wide margin.
#
# min/max_price_b_cents=300/500 vary only the RANDOMLY-DRAWN base price
# of B per route (cosmetic variety, matching the other corpora's own
# price-band draws) -- the GAP applied on top, and therefore max_saving,
# is fixed by parity alone and never depends on the drawn base price.
PRUNE_PENALTY_DOMINATION_CORPUS_PARAMS = PrunePenaltyDominationCorpusParams(
    seed=20260817,
    n_routes=40,
    b_position_mi=Decimal("250"),
    a_spacing_mi=Decimal("5"),
    tank_range_mi=Decimal("500"),
    total_route_mi=Decimal("700"),
    mpg=Decimal("1"),
    penalty=Decimal("35"),
    min_price_b_cents=300,
    max_price_b_cents=500,
    price_step_above_cents=100,
    price_step_below_cents=1,
)


def build_penalty_domination_corpus(*, params=PRUNE_PENALTY_DOMINATION_CORPUS_PARAMS):
    """Deterministically build params.n_routes two-station routes, each a
    (B, A) pair on the SAME fixed geometry, from a fresh
    random.Random(params.seed) -- never the global random module, so two
    consecutive calls return byte-identical corpora (proven by
    test_build_penalty_domination_corpus_is_deterministic_across_two_calls
    below), mirroring build_prune_corpus()/build_price_cluster_corpus()'s
    own determinism precedent exactly.

    Route index parity decides the price gap applied (see
    PrunePenaltyDominationCorpusParams' own docstring for the full
    argument): even index -> "clears the bar" (both retained), odd index
    -> "fails the bar" (only B retained). B's own price is drawn once per
    route from the pinned band; A's price is B's price minus the
    parity-selected gap, never drawn independently, so the gap -- and
    therefore max_saving -- is fixed by parity alone, not by the draw.
    Both stations are real-priced (the default Candidate.price_source),
    so condition 4c is trivially satisfied (margin_B == margin_A == 0)
    and is never the reason either station survives or is removed here.
    Returns a list of (candidates, clears_bar) tuples.
    """
    rng = random.Random(params.seed)

    routes = []
    for i in range(params.n_routes):
        clears_bar = i % 2 == 0
        step_cents = (
            params.price_step_above_cents if clears_bar else params.price_step_below_cents
        )

        price_b_cents = rng.randint(params.min_price_b_cents, params.max_price_b_cents)
        price_a_cents = price_b_cents - step_cents

        b = Candidate(
            name=f"PB{i}",
            opis_id=i * 2,
            price_per_gallon=Decimal(price_b_cents) / Decimal(100),
            distance_from_start_mi=params.b_position_mi,
        )
        a = Candidate(
            name=f"PA{i}",
            opis_id=i * 2 + 1,
            price_per_gallon=Decimal(price_a_cents) / Decimal(100),
            distance_from_start_mi=params.b_position_mi + params.a_spacing_mi,
        )
        routes.append(([b, a], clears_bar))
    return routes


def _expected_penalty_domination_retention(params):
    """Pure function of PrunePenaltyDominationCorpusParams, derived
    directly from the domination rule in routing/services/prune.py
    (conditions 4a, 4b and 4c) -- never measured. Half of params.n_routes
    (the even-index, "clears the bar" routes) retain BOTH members: A is
    cheaper than B, so conditions 1-3 (which require the dominator to be
    cheaper-or-equal) never apply, and condition 4b's own test
    (price_gap * tank_range_mi / mpg < penalty) is false by construction
    for price_step_above_cents, so condition 4 does not remove A either.
    The other half (odd-index, "fails the bar" routes) retain only B:
    condition 4b's test is true by construction for
    price_step_below_cents, and 4a (price_B >= price_A) and 4c (both
    real-priced, margins equal) both hold trivially, so A is removed.
    See PrunePenaltyDominationCorpusParams' own docstring for the full
    argument this reduces to arithmetic.
    """
    n_clears = params.n_routes // 2
    n_fails = params.n_routes - n_clears
    return n_clears * 2 + n_fails * 1


# Computed, not hand-typed, from PRUNE_PENALTY_DOMINATION_CORPUS_PARAMS
# above -- a floor expressed as "removed at least N" (PRUNE_REDUCTION_
# FLOOR's own shape) would pass a no-op prune(x) -> x just as readily as
# it would pass an over-pruning implementation that also strips a
# clears-the-bar route down to one survivor; only an exact equality,
# derived from the rule rather than measured from a run, catches both
# directions at once -- exactly PRUNE_CLUSTER_RETENTION_EXPECTATION's own
# argument, restated here for condition 4 rather than conditions 1-3.
PRUNE_PENALTY_DOMINATION_RETENTION_EXPECTATION = _expected_penalty_domination_retention(
    PRUNE_PENALTY_DOMINATION_CORPUS_PARAMS
)


# D-06's permanent boundary witness (Assertion 3 below): a single
# hand-built three-station geometry exercising condition 4b at BOTH edges
# of `penalty` at once, rather than two separate lists. WB is the
# tail-reaching pricier alternative; WA_BELOW and WA_ABOVE are two
# cheaper, ALSO tail-reaching alternatives at DIFFERENT positions, each
# compared against the SAME witness (WB's price) -- a candidate removed
# by condition 4 never updates the running-minimum tracker (see
# prune_dominated_candidates' pass-2 loop: the running_min update lines
# sit strictly after "if removed: continue"), so WA_ABOVE is compared
# against WB's own price regardless of WA_BELOW's fate, never against
# WA_BELOW's -- verified by the standalone trace recorded in
# 25-03-SUMMARY.md before this witness was pinned. tank_range_mi=mpg=1
# so max_saving = price_gap directly, with no scaling to account for by
# hand.
#
# gap(WB, WA_BELOW) = 40.00 - 5.01 = 34.99 -- $0.01 BELOW
# PENALTY_ANCHORS[1] ($35.00). Condition 4b's test (max_saving < penalty)
# is TRUE: WA_BELOW is removed.
# gap(WB, WA_ABOVE) = 40.00 - 4.99 = 35.01 -- $0.01 ABOVE $35.00.
# Condition 4b's test is FALSE: WA_ABOVE is retained.
#
# This witness pins the bound's behaviour at the boundary and is not to
# be regenerated, mirroring SLIVER_WITNESS_EQUAL_PRICE/SLIVER_WITNESS_
# STRICT_INEQUALITY's own permanence framing above.
PENALTY_BOUNDARY_WITNESS_TANK_RANGE_MI = Decimal("1")
PENALTY_BOUNDARY_WITNESS_TOTAL_ROUTE_MI = Decimal("1")
PENALTY_BOUNDARY_WITNESS_MPG = Decimal("1")
PENALTY_BOUNDARY_WITNESS_PENALTY = PENALTY_ANCHORS[1]

PENALTY_BOUNDARY_WITNESS = [
    _candidate(opis_id=0, price="40.00", position="0", name="WB"),
    _candidate(opis_id=1, price="5.01", position="0.5", name="WA-below"),
    _candidate(opis_id=2, price="4.99", position="0.75", name="WA-above"),
]


class PenaltyDominationSoundnessTests(SimpleTestCase):
    """Phase 25 D-06: the strengthened (condition 4) domination rule is
    sound, and provably non-vacuous, on a corpus purpose-built to exercise
    it. Three assertions:

    1. Differential soundness against this module's own imported oracle
       (optimal_fixed_charge_plan), at both PENALTY_ANCHORS rungs and at
       the adopted TRUST_MARGIN_USD default (ADOPTED_MARGIN_USD) -- never
       argued, always checked. Tolerance is `penalty + COST_TOLERANCE`,
       not bare COST_TOLERANCE: condition 4's own proof in prune.py
       ("Bound, precisely") guarantees regret strictly less than
       `penalty`, not zero, on the same station that condition 4 itself
       removed -- the identical weaker bound PrunePenaltyInvarianceTests
       already checks to. Using COST_TOLERANCE here (IdenticalPriceCluster
       PruneSoundnessTests' own tolerance) would be wrong for this corpus
       specifically: that class's dominator/dominated pair share one
       price, so its own removals are cost-neutral by construction, while
       this corpus's removals cross a genuine, if small, price gap by
       design -- see 25-03-SUMMARY.md's "Tolerance" note for the standalone
       trace that first surfaced this.
    2. A closed-form retention equality
       (PRUNE_PENALTY_DOMINATION_RETENTION_EXPECTATION), catching both a
       no-op prune (which would over-retain the fails-the-bar routes
       relative to the derived expectation) and an over-pruning
       implementation (which would under-retain the clears-the-bar
       routes) -- the two directions a one-sided floor
       (PRUNE_REDUCTION_FLOOR's own shape) cannot both catch at once.
    3. A permanent hand-built witness on the SAME two-alternative
       geometry, varying only the price gap, proving condition 4 removes
       a station whose maximum saving sits a cent-scale amount below
       `penalty` and retains one whose maximum saving sits a cent-scale
       amount above it.

    Every assertion below calls prune_dominated_candidates directly, never
    solver.solve() (D-14) -- grep this class body for `solve(` to confirm.
    """

    def test_prune_regret_stays_under_penalty_on_the_penalty_domination_corpus(self):
        params = PRUNE_PENALTY_DOMINATION_CORPUS_PARAMS
        margins = (Decimal(0), ADOPTED_MARGIN_USD)
        routes = build_penalty_domination_corpus()

        for route_index, (candidates, clears_bar) in enumerate(routes):
            self.assertLessEqual(
                len(candidates),
                MAX_STATIONS,
                f"route {route_index}: build_penalty_domination_corpus() "
                f"drew more than MAX_STATIONS candidates: {candidates!r} "
                f"-- the oracle's subset enumeration cannot terminate "
                f"over this input.",
            )

            for penalty in PENALTY_ANCHORS:
                retained = prune_dominated_candidates(
                    candidates,
                    tank_range_mi=params.tank_range_mi,
                    total_route_mi=params.total_route_mi,
                    mpg=params.mpg,
                    penalty=penalty,
                )

                for margin in margins:
                    unpruned_plan = optimal_fixed_charge_plan(
                        candidates,
                        params.total_route_mi,
                        penalty=penalty,
                        tank_range_mi=params.tank_range_mi,
                        mpg=params.mpg,
                        trust_margin=margin,
                    )
                    pruned_plan = optimal_fixed_charge_plan(
                        retained,
                        params.total_route_mi,
                        penalty=penalty,
                        tank_range_mi=params.tank_range_mi,
                        mpg=params.mpg,
                        trust_margin=margin,
                    )

                    context = (
                        f"route_index={route_index}, clears_bar={clears_bar}, "
                        f"candidates={candidates!r}, retained={retained!r}, "
                        f"penalty={penalty}, margin={margin}"
                    )

                    self.assertEqual(
                        unpruned_plan is None,
                        pruned_plan is None,
                        f"feasibility verdicts disagree between pruned and "
                        f"unpruned solves on the penalty-domination corpus; "
                        f"{context}",
                    )
                    if unpruned_plan is None:
                        continue

                    self.assertLess(
                        abs(unpruned_plan.objective - pruned_plan.objective),
                        penalty + COST_TOLERANCE,
                        f"pruned objective ({pruned_plan.objective}) differs "
                        f"from the unpruned objective "
                        f"({unpruned_plan.objective}) by penalty or more; "
                        f"condition 4's own proof bounds regret strictly "
                        f"under `penalty`, never more; {context}",
                    )

    def test_retained_count_equals_the_closed_form_expectation(self):
        """Anti-vacuity by closed-form equality, not by floor alone (see
        this class's own docstring). `prune(x) -> x` passes assertion 1
        above vacuously (a no-op removes nothing, so pruned == unpruned
        trivially, and feasibility trivially agrees too); only this
        equality -- derived from the rule, never measured -- catches it,
        and catches over-pruning too.
        """
        params = PRUNE_PENALTY_DOMINATION_CORPUS_PARAMS
        routes = build_penalty_domination_corpus()
        total_retained = 0
        for candidates, _clears_bar in routes:
            retained = prune_dominated_candidates(
                candidates,
                tank_range_mi=params.tank_range_mi,
                total_route_mi=params.total_route_mi,
                mpg=params.mpg,
                penalty=params.penalty,
            )
            total_retained += len(retained)

        self.assertEqual(
            total_retained,
            PRUNE_PENALTY_DOMINATION_RETENTION_EXPECTATION,
            f"retained {total_retained} candidates across "
            f"PRUNE_PENALTY_DOMINATION_CORPUS_PARAMS.n_routes="
            f"{params.n_routes} penalty-domination routes at "
            f"penalty={params.penalty}; expected exactly "
            f"PRUNE_PENALTY_DOMINATION_RETENTION_EXPECTATION="
            f"{PRUNE_PENALTY_DOMINATION_RETENTION_EXPECTATION}. A smaller "
            f"measured total means the rule over-pruned a clears-the-bar "
            f"route; a larger one means it under-pruned (or is a no-op) "
            f"on a fails-the-bar route -- either way the rule and an "
            f"independent reading of its own stated conditions (4a, 4b, "
            f"4c) disagree.",
        )

    def test_build_penalty_domination_corpus_is_deterministic_across_two_calls(self):
        """Without random.Random(seed) freshly seeded per call, a corpus
        that quietly consumed global RNG state would make the two guards
        above unreproducible."""
        first = build_penalty_domination_corpus()
        second = build_penalty_domination_corpus()
        self.assertEqual(
            [
                [
                    (c.opis_id, c.price_per_gallon, c.distance_from_start_mi, c.price_source)
                    for c in candidates
                ]
                for candidates, _clears_bar in first
            ],
            [
                [
                    (c.opis_id, c.price_per_gallon, c.distance_from_start_mi, c.price_source)
                    for c in candidates
                ]
                for candidates, _clears_bar in second
            ],
            "build_penalty_domination_corpus() returned different corpora "
            "across two consecutive calls -- it must be freshly seeded on "
            "every call, never consuming global RNG state.",
        )

    def test_penalty_bound_boundary_witness(self):
        """Assertion 3: the permanent hand-built witness. A single
        three-station list exercising condition 4b at both edges of
        `penalty` at once -- see PENALTY_BOUNDARY_WITNESS' own comment
        for the full geometry and the running-minimum argument for why
        the two edges do not interfere with each other.
        """
        result = prune_dominated_candidates(
            PENALTY_BOUNDARY_WITNESS,
            tank_range_mi=PENALTY_BOUNDARY_WITNESS_TANK_RANGE_MI,
            total_route_mi=PENALTY_BOUNDARY_WITNESS_TOTAL_ROUTE_MI,
            mpg=PENALTY_BOUNDARY_WITNESS_MPG,
            penalty=PENALTY_BOUNDARY_WITNESS_PENALTY,
        )
        self.assertEqual(
            tuple(c.opis_id for c in result),
            (0, 2),
            f"penalty boundary witness (WB $40.00@0mi; WA-below "
            f"$5.01@0.5mi, gap $34.99, $0.01 below penalty; WA-above "
            f"$4.99@0.75mi, gap $35.01, $0.01 above penalty; "
            f"tank_range_mi=mpg=1, penalty=$35.00): WA-below (opis_id=1) "
            f"must be removed and WA-above (opis_id=2) must be retained, "
            f"alongside WB (opis_id=0). Got retained={result!r}",
        )

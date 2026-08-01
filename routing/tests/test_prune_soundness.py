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

Uses django.test.SimpleTestCase throughout, never Hypothesis's own
Django-integrated TestCase: the prune under test is pure and never
touches the ORM, so the Django/Hypothesis integration's per-example
database transaction would buy nothing here.

This first test class, PruneRetainedSetTests, is the primary evidence for
ROADMAP criterion 3, which makes a claim about what the prune *keeps*
("never discards the sole station reachable from a starting_fuel=0
origin") rather than a claim about plan equality -- so the assertions
below are on the retained set directly (D-05), not on a downstream solve()
call.
"""
import inspect
import random
from dataclasses import dataclass
from decimal import Decimal

from django.test import SimpleTestCase, tag
from hypothesis import example, given, settings
from hypothesis import strategies as st

from routing.services import Candidate, solve
from routing.services.exceptions import InfeasibleRouteError
from routing.services.prune import prune_dominated_candidates
from routing.tests.test_solver_fixed_charge_optimality import (
    COST_TOLERANCE,
    MAX_STATIONS,
    OraclePlan,
    optimal_fixed_charge_plan,
    single_leg_routes,
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
GREEDY_MIN_STATIONS = 100
GREEDY_STATION_CAP = 250


@st.composite
def dense_corridor_routes(draw):
    """Draw a dense single-leg route for the greedy/density differential
    arm: GREEDY_MIN_STATIONS..GREEDY_STATION_CAP candidates on a route
    drawn from roughly 1,400-2,600 mi -- the band REQUIREMENTS.md's
    Evidence Base names as where the problem concentrates -- with
    tank_range_mi drawn from roughly 200-1,050 mi, so
    tank_range_mi / total_route_mi varies across the range that governs
    how much of the route falls in the prune's tail region.

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
    tank_range_mi = Decimal(draw(st.integers(min_value=200, max_value=1050)))
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

        # Cost, always (D-02) -- within the same tolerance band the oracle
        # arm and the shipped optimality suite both use.
        self.assertLessEqual(
            abs(unpruned_plan.total_cost - pruned_plan.total_cost),
            COST_TOLERANCE,
            f"pruned total_cost ({pruned_plan.total_cost}) differs from "
            f"the unpruned total_cost ({unpruned_plan.total_cost}) beyond "
            f"COST_TOLERANCE; {context}",
        )


class PrunePenaltyInvarianceTests(SimpleTestCase):
    """D-04: prune_dominated_candidates takes no penalty argument, and its
    retained set is byte-identical across penalties. Both claims are
    written so they can actually fail -- the structural test breaks the
    build the moment someone threads a penalty through the prune's
    signature, and the behavioral property re-invokes the prune separately
    per penalty rather than comparing a single result to itself, so a
    future penalty-dependent prune has somewhere real to disagree.
    """

    def test_signature_has_no_penalty_and_no_undeclared_knobs(self):
        """This test exists to break the build the moment someone threads
        a penalty parameter through prune_dominated_candidates. It pins
        three claims at once: no `penalty` parameter (D-04); no
        `gap <= W` window or prefilter-width parameter (D-09 -- the exact
        rule is the whole rule, and shipping a tunable constant alongside
        the theorem would leave exactly one unproven number in the
        deliverable); and no other undeclared knob, since the parameter
        tuple must equal exactly three names, not merely contain them.
        """
        self.assertEqual(
            tuple(inspect.signature(prune_dominated_candidates).parameters),
            ("candidates", "tank_range_mi", "total_route_mi"),
        )
        with self.assertRaises(TypeError):
            prune_dominated_candidates(
                [],
                tank_range_mi=Decimal(500),
                total_route_mi=Decimal(1000),
                penalty=Decimal("35"),
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
    def test_retained_set_is_byte_identical_across_penalties(self, drawn_route, penalties):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        retained_opis_ids_by_penalty = {}
        for penalty in penalties:
            # Called fresh inside the loop, once per penalty -- never
            # computed once outside it and compared to itself, since that
            # would have nothing real to disagree with.
            retained = prune_dominated_candidates(
                candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
            )
            retained_opis_ids_by_penalty[penalty] = tuple(c.opis_id for c in retained)

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
                self.assertLessEqual(
                    abs(unpruned_plan.objective - pruned_plan.objective),
                    COST_TOLERANCE,
                    f"pruned objective ({pruned_plan.objective}) differs "
                    f"from the unpruned objective "
                    f"({unpruned_plan.objective}) beyond COST_TOLERANCE "
                    f"at penalty={penalty}; {context}",
                )

        distinct_retained_sets = set(retained_opis_ids_by_penalty.values())
        self.assertEqual(
            len(distinct_retained_sets),
            1,
            f"retained opis_id tuple was not byte-identical across "
            f"penalties (D-04): {retained_opis_ids_by_penalty!r}; "
            f"candidates={candidates!r}, tank_range_mi={tank_range_mi}, "
            f"total_route_mi={total_route_mi}",
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

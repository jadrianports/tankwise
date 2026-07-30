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

Uses django.test.SimpleTestCase throughout, never
hypothesis.extra.django.TestCase: the prune under test is pure and never
touches the ORM, so the Django/Hypothesis integration's per-example
database transaction would buy nothing here.

This first test class, PruneRetainedSetTests, is the primary evidence for
ROADMAP criterion 3, which makes a claim about what the prune *keeps*
("never discards the sole station reachable from a starting_fuel=0
origin") rather than a claim about plan equality -- so the assertions
below are on the retained set directly (D-05), not on a downstream solve()
call.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from routing.services import Candidate, solve
from routing.services.exceptions import InfeasibleRouteError
from routing.services.prune import prune_dominated_candidates


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
        """A lone station has no dominator (the stack is empty when it is
        examined), so it is structurally unprunable -- checked across
        several tank ranges and both a route longer and a route shorter
        than the tank range.
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

    def test_all_identical_prices_cluster_shorter_than_tank_range_leaves_one_survivor(self):
        """Stations at 1-mile spacing across [0, 100], all priced
        identically, on a 2,000-mile route with tank_range_mi=1050. Every
        station's dominator is its immediate predecessor (equal price
        never pops the monotonic stack), and every resulting sliver lands
        past the cluster (>= 1050 mi) and short of FINISH (2,000 mi), so
        exactly one station survives -- the lowest-position,
        lowest-opis_id one.
        """
        candidates = [
            _candidate(opis_id=i, price="3.499", position=i) for i in range(101)
        ]

        result = prune_dominated_candidates(
            candidates, tank_range_mi=Decimal(1050), total_route_mi=Decimal(2000)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].opis_id, 0)

    def test_all_identical_prices_short_tank_range_retains_more_than_one(self):
        """The same cluster as above, but with tank_range_mi small
        relative to the 1-mile spacing (10 mi), so that
        sliver(A, B) = (pos_B + T, pos_A + T] lands inside the populated
        [0, 100] span for most candidates and finds an occupying station.
        Asserts len(retained) > 1 -- the arm that stops the long-tank-range
        test above from being satisfiable by a prune that simply always
        returns one element.
        """
        candidates = [
            _candidate(opis_id=i, price="3.499", position=i) for i in range(101)
        ]

        result = prune_dominated_candidates(
            candidates, tank_range_mi=Decimal(10), total_route_mi=Decimal(2000)
        )

        self.assertGreater(len(result), 1)

    def test_route_shorter_than_tank_range_retains_exactly_the_prefix_minima(self):
        """With total_route_mi < tank_range_mi, every sliver's lower bound
        (pos_B + T) already exceeds every candidate's position and exceeds
        total_route_mi (since T alone already exceeds total_route_mi), so
        no sliver can hold a candidate or FINISH. A candidate is removable
        exactly when it has a dominator, so the retained set is exactly
        the strict prefix minima of the price sequence under the total
        order -- the running-minimum stations. Asserted as set equality
        against that independently computed expectation, not merely a
        length.
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

        D-05's derivation: any dominator B of the origin station would
        need pos_B <= pos_A == 0, so B would have to sit at position 0
        too -- there is no station strictly closer to START that could
        dominate it. Structurally, the position-0 station is always index
        0 in the total order (nothing sorts before position 0) and the
        monotonic stack is empty when index 0 is examined, so it can
        never have a dominator and can never be pruned. This is asserted
        here, not special-cased: prune_dominated_candidates contains no
        starting_fuel parameter and no branch keyed on origin fuel at all.
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
        opis_id values. Under a non-strict comparison without the D-11
        total order, both would see each other as a valid dominator (a
        same-position sliver has zero width and is trivially empty) and
        both would be removed -- this test exists to catch exactly that
        failure. Asserts exactly one survivor and that it is the lower
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
        """Same position, different prices: the cheaper station survives,
        the costlier co-located station is removed.
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

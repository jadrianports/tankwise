"""Phase 25 D-08: the reconciliation gate inside `classify_removals()`
(`measure_prune_reduction.py`) has, until this module, only ever fired
when a human ran the command by hand -- there was no test module for this
command at all. That is precisely the "guard nobody has watched fail"
pattern this milestone exists to end, so this module pins it: every
class is proven reachable, the widened four-tuple reconciliation is
exercised in CI even though `measure_prune_reduction` itself must not
run in CI, and both hard `CommandError` gates are proven capable of
firing, not merely assumed to be capable.

All tests below construct `ordered`/`retained` `Candidate` lists by hand
and call `classify_removals()` directly -- never through
`prune_dominated_candidates` or the command's own `handle()` -- which is
the point: `classify_removals()` is an INDEPENDENT reading of
`prune.py`'s admission conditions, and this module's job is to prove that
independence is real, not merely claimed in a docstring.
"""
from decimal import Decimal
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from routing.management.commands.measure_prune_reduction import (
    _is_penalty_dominated,
    classify_removals,
)
from routing.services import Candidate
from routing.services.solver import ESTIMATE_PRICE_SOURCE

# Shared geometry for most cases below: tank_range_mi == total_route_mi
# means every station's own supply interval trivially reaches FINISH
# (pos + 1000 >= 1000 for any pos >= 0), so the "tail" branch of
# condition 2 is always available and each test corpus can isolate the
# ONE distinguishing feature (price direction, provenance, position) it
# means to exercise without also having to engineer tank-range geometry.
TANK_RANGE_MI = Decimal("1000")
TOTAL_ROUTE_MI = Decimal("1000")
MPG = Decimal("10")
PENALTY = Decimal("35")


def _candidate(opis_id, price, position, *, estimate=False):
    """Build a Candidate from (opis_id, price, position) with Decimal
    coercion, optionally estimate-priced -- mirrors
    test_prune_soundness.py's own `_candidate` helper shape."""
    return Candidate(
        name=f"Station {opis_id}",
        opis_id=opis_id,
        price_per_gallon=Decimal(str(price)),
        distance_from_start_mi=Decimal(str(position)),
        price_source=ESTIMATE_PRICE_SOURCE if estimate else "opis_indexed",
    )


class ClassifyRemovalsFourClassReconciliationTests(SimpleTestCase):
    """Phase 25 D-08. Exercises `classify_removals()`'s widened four-class
    reconciliation -- `co_located`, `tail`, `margin_blocked`,
    `penalty_dominated` -- on hand-built inputs small enough to verify by
    hand. This class exists so the reconciliation gate, including both
    hard `CommandError` paths, runs in CI on every push, even though
    `measure_prune_reduction` (the command that calls `classify_removals`
    against real committed data) must not run in CI itself.
    """

    def test_three_class_parity_with_mpg_and_penalty_omitted(self):
        """With mpg=None (the default), the four-tuple's first three
        members equal what the three-class world produced on the same
        input, and penalty_dominated is 0. Corpus: B (cheap, pos 0) is a
        co-located dominator for A (pos 0), and D (cheap, pos 950) is a
        tail dominator for C (pos 980) -- two independent removals, one
        of each pre-existing class, in a single call. Uses a tank range
        SHORTER than the route (unlike this module's shared TANK_RANGE_MI/
        TOTAL_ROUTE_MI, which are equal and would make every station
        trivially tail-reaching) so the co-located pair at position 0
        cannot cross-contaminate the tail pair at the far end of the
        route -- the two removals stay genuinely independent, which is
        the point of pinning parity on a corpus exercising BOTH classes
        in the same call.
        """
        tank_range_mi = Decimal("100")
        total_route_mi = Decimal("1000")

        cheap_colocated = _candidate(opis_id=1, price="2.00", position=0)
        expensive_colocated = _candidate(opis_id=2, price="3.00", position=0)
        cheap_tail = _candidate(opis_id=3, price="2.50", position=950)
        expensive_tail = _candidate(opis_id=4, price="3.50", position=980)

        ordered = sorted(
            [cheap_colocated, expensive_colocated, cheap_tail, expensive_tail],
            key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
        )
        retained = [cheap_colocated, cheap_tail]

        result = classify_removals(
            ordered, retained, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        self.assertEqual(
            result,
            (1, 1, 0, 0),
            "three-class parity broken: (co_located=1, tail=1, margin_blocked=0, "
            "penalty_dominated=0) expected with mpg=None",
        )
        self.assertEqual(result[:3], (1, 1, 0), "first three tallies must match the pre-amendment shape")
        self.assertEqual(result[3], 0, "penalty_dominated must be 0 whenever mpg/penalty are omitted")

    def test_co_located_class_reachable_in_isolation(self):
        """A removal explainable ONLY by a co-located, cheaper-or-equal
        dominator lands in co_located and nowhere else."""
        cheap = _candidate(opis_id=1, price="3.00", position=0)
        expensive = _candidate(opis_id=2, price="4.00", position=0)
        ordered = [cheap, expensive]
        retained = [cheap]

        result = classify_removals(
            ordered, retained, tank_range_mi=TANK_RANGE_MI, total_route_mi=TOTAL_ROUTE_MI
        )

        self.assertEqual(result, (1, 0, 0, 0))

    def test_tail_class_reachable_in_isolation(self):
        """A removal explainable ONLY by an earlier, cheaper-or-equal,
        tail-reaching dominator at a DIFFERENT position lands in tail and
        nowhere else."""
        cheap = _candidate(opis_id=1, price="2.00", position=100)
        expensive = _candidate(opis_id=2, price="3.00", position=200)
        ordered = [cheap, expensive]
        retained = [cheap]

        result = classify_removals(
            ordered, retained, tank_range_mi=TANK_RANGE_MI, total_route_mi=TOTAL_ROUTE_MI
        )

        self.assertEqual(result, (0, 1, 0, 0))

    def test_margin_blocked_class_reachable_in_isolation(self):
        """A real-priced station that a price-only reading would have
        removed, but which survives because its only qualifying
        dominator is estimate-priced (condition 3), is margin_blocked --
        and stays RETAINED, contributing zero to co_located/tail/
        penalty_dominated."""
        cheap_estimate = _candidate(opis_id=1, price="2.00", position=100, estimate=True)
        real = _candidate(opis_id=2, price="3.00", position=200)
        ordered = [cheap_estimate, real]
        retained = [cheap_estimate, real]  # real survives -- margin-blocked

        result = classify_removals(
            ordered, retained, tank_range_mi=TANK_RANGE_MI, total_route_mi=TOTAL_ROUTE_MI
        )

        self.assertEqual(result, (0, 0, 1, 0))

    def test_penalty_dominated_class_reachable_in_isolation(self):
        """A removal explainable ONLY by a retained, PRICIER-or-equal,
        tail-reaching alternative whose maximum fuel saving over the
        removed station is provably below `penalty` (condition 4) lands
        in penalty_dominated and nowhere else. B is pricier than A --
        the opposite price direction from every other class above -- so
        A has no cheaper dominator at all and the co_located/tail checks
        both fail before the penalty check is ever reached.
        """
        pricier_but_good_enough = _candidate(opis_id=1, price="3.30", position=0)
        cheap_not_worth_the_stop = _candidate(opis_id=2, price="3.00", position=100)
        ordered = [pricier_but_good_enough, cheap_not_worth_the_stop]
        retained = [pricier_but_good_enough]

        # Witness setup check: 4b's bound must actually clear, or this
        # corpus proves nothing about the penalty_dominated class.
        price_gap = pricier_but_good_enough.price_per_gallon - cheap_not_worth_the_stop.price_per_gallon
        max_saving = price_gap * TANK_RANGE_MI / MPG
        self.assertLess(max_saving, PENALTY, "witness setup error: 4b's bound must clear")

        result = classify_removals(
            ordered,
            retained,
            tank_range_mi=TANK_RANGE_MI,
            total_route_mi=TOTAL_ROUTE_MI,
            mpg=MPG,
            penalty=PENALTY,
        )

        self.assertEqual(result, (0, 0, 0, 1))

    def test_precedence_a_doubly_explainable_removal_lands_in_the_geometric_class(self):
        """A removal that satisfies BOTH a geometric condition (co-located,
        cheaper-or-equal) AND, independently, the penalty condition
        (price_B >= price_A, since B's price equals A's exactly) must
        land in co_located, never penalty_dominated -- proving the
        precedence rule is load-bearing, not prose. Without it, a removal
        attributable to two classes could be double-counted or attributed
        inconsistently depending on evaluation order.
        """
        dominator = _candidate(opis_id=1, price="3.00", position=0)
        removed = _candidate(opis_id=2, price="3.00", position=0)
        ordered = [dominator, removed]
        retained = [dominator]

        # Prove the double-qualification is real, not assumed: the same
        # earlier station independently satisfies the penalty-domination
        # predicate too (price_B == price_A clears BOTH price_B <= price_A
        # and price_B >= price_A).
        self.assertTrue(
            _is_penalty_dominated(
                removed,
                [dominator],
                {id(dominator)},
                tank_range_mi=TANK_RANGE_MI,
                total_route_mi=TOTAL_ROUTE_MI,
                mpg=MPG,
                penalty=PENALTY,
            ),
            "witness setup error: this corpus must be doubly-explainable for the "
            "precedence test to prove anything",
        )

        result = classify_removals(
            ordered,
            retained,
            tank_range_mi=TANK_RANGE_MI,
            total_route_mi=TOTAL_ROUTE_MI,
            mpg=MPG,
            penalty=PENALTY,
        )

        self.assertEqual(
            result,
            (1, 0, 0, 0),
            "a doubly-explainable removal must land in co_located (checked first), "
            "not penalty_dominated -- and the tallies must still sum to the total",
        )
        self.assertEqual(sum(result[:2]) + result[3], 1, "co_located + tail + penalty_dominated == total_removed")

    def test_zero_unexplained_gate_fires_on_an_unattributable_removal(self):
        """Constructs a `retained` list that omits a station no stated
        condition -- geometric or (with mpg/penalty active) penalty --
        can explain, by hand-trimming `retained` rather than using the
        rule's own output. `far_cheap` sits far downstream, is cheaper,
        but its own supply interval does not reach the removed station's
        position AND does not reach FINISH, so it cannot dominate under
        any condition. `unattributable` must therefore be reported, not
        silently misclassified.
        """
        short_tank = Decimal("10")
        long_route = Decimal("1000")

        near_expensive = _candidate(opis_id=1, price="5.00", position=0)
        far_cheap_noncovering = _candidate(opis_id=2, price="1.00", position=500)
        ordered = [near_expensive, far_cheap_noncovering]
        # Hand-trimmed: omits far_cheap_noncovering even though nothing
        # explains its absence -- this is what the gate exists to catch.
        retained = [near_expensive]

        with self.assertRaises(CommandError) as ctx:
            classify_removals(
                ordered, retained, tank_range_mi=short_tank, total_route_mi=long_route
            )

        self.assertIn("could not attribute", str(ctx.exception))
        self.assertIn(str(far_cheap_noncovering.opis_id), str(ctx.exception))

    def test_sum_gate_fires_independent_of_the_unexplained_gate(self):
        """The tally-sum gate (`co_located + tail + penalty_dominated !=
        total_removed`) can, in principle, never fire once the
        unexplained gate has already passed with zero unexplained
        removals: every removed candidate is attributed to exactly one of
        the three buckets by construction of the classification loop's
        if/elif/elif chain, so co_located + tail + penalty_dominated
        equals total_removed by simple counting whenever unexplained is
        empty. This test instead exploits `total_removed`'s definition
        (`len(ordered) - len(retained)`, using the raw list length, not a
        deduplicated identity-set size) by passing `retained` with a
        DUPLICATE reference to the same object -- a malformed but
        independently-constructible caller error that undercounts
        `total_removed` relative to the correctly-attributed tallies,
        firing the sum gate on its own, with the unexplained gate never
        triggered at all.
        """
        cheap = _candidate(opis_id=1, price="2.00", position=0)
        expensive = _candidate(opis_id=2, price="3.00", position=0)
        ordered = [cheap, expensive]
        retained = [cheap, cheap]  # duplicate reference -- len(retained) == 2

        with self.assertRaises(CommandError) as ctx:
            classify_removals(
                ordered, retained, tank_range_mi=TANK_RANGE_MI, total_route_mi=TOTAL_ROUTE_MI
            )

        self.assertIn("do not sum to the total number removed", str(ctx.exception))

    def test_classify_removals_never_calls_prune_dominated_candidates(self):
        """Mechanical proof of the independence claim the module docstring
        makes: patch `routing.services.prune.prune_dominated_candidates`
        to raise on any call, then run the same corpus every other test in
        this module uses, and assert BOTH that the four-tuple is still
        correct AND that the patched function was never invoked. If
        `classify_removals` ever started delegating to the implementation
        it exists to check, this test fails the instant that delegation
        is added -- it does not depend on the mock actually raising."""
        cheap = _candidate(opis_id=1, price="3.00", position=0)
        expensive = _candidate(opis_id=2, price="4.00", position=0)
        ordered = [cheap, expensive]
        retained = [cheap]

        with patch(
            "routing.services.prune.prune_dominated_candidates",
            side_effect=AssertionError(
                "classify_removals must never call prune_dominated_candidates"
            ),
        ) as mock_prune:
            result = classify_removals(
                ordered, retained, tank_range_mi=TANK_RANGE_MI, total_route_mi=TOTAL_ROUTE_MI
            )

        mock_prune.assert_not_called()
        self.assertEqual(result, (1, 0, 0, 0))

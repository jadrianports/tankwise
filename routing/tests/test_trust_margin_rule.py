"""Trust-margin derivation formula, tagging selector, and adoption rule --
pinned before any dispersion figure, ladder rung, or sweep result exists.

D-09's ordering is the single decision in Phase 21 that cannot be
recovered from if it goes wrong: once a distribution or a sweep result has
been seen, no later claim that the ladder was chosen independently is
credible. Every constant and every pure function below was committed
before the dispersion measurement (plan 21-03), the twelve-corridor sweep
(plan 21-08), or the adoption decision that sweep drives ever ran. This is
the same discipline `CorpusParams`/`CORPUS_PARAMS`
(`test_solver_fixed_charge_optimality.py`, Phase 16) and
`PruneCorpusParams`/`PRUNE_CORPUS_PARAMS` (`test_prune_soundness.py`,
Phase 17) both exist to enforce, and 18.1-01 pinned
`DP_TRANSITION_BUDGET_LADDER` and its adoption rule the same way before any
live measurement. Adjusting any constant or any rule below after seeing a
result is the specific failure this file exists to prevent -- if that ever
happens, the honest record is a NEW dated constant or rule, never a silent
edit to one of these.

Like `test_corridor_fixtures.py`, this is a `test_*` module that ALSO
exports pinned constants and pure functions for production management
commands to import -- `measure_price_dispersion.py` (plan 21-03) and
`measure_trust_margin.py` (plan 21-08) both import from here, mirroring how
`measure_prune_reduction.py` already imports `CORRIDORS`/`PRICE_BASES`/
`factor_lookup_for_basis()` from `test_corridor_fixtures.py`.

`MARGIN_LADDER` and `PADD_AVERAGE_PRICE` are declared below as explicitly
unset (`None`), with a passing test asserting exactly that. Plan 21-03 is
the only plan permitted to instantiate them, by inverting that test rather
than deleting it (the project's standing "invert the guard, don't delete
it" rule) -- so the git history itself proves the rule predates the
numbers, rather than merely asserting it in a SUMMARY.
"""
import hashlib
import subprocess
import sys
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal

from django.test import SimpleTestCase

from routing.services.solver import Candidate

try:
    from routing.services.solver import ESTIMATE_PRICE_SOURCE
except ImportError:
    # Plan 21-05 is expected to land the shared, canonical
    # `routing.services.solver.ESTIMATE_PRICE_SOURCE` literal. Until it
    # does, this module defines its own copy so `tagged_candidates()` below
    # has a value to write. The literal is `Station.PriceSource
    # .EIA_REGIONAL_ESTIMATE`'s value (see `routing/models.py`), transcribed
    # here rather than imported across the pure/Django boundary. Once
    # 21-05 lands the shared constant, the `try` branch above picks it up
    # automatically and this fallback becomes dead code -- no edit needed.
    ESTIMATE_PRICE_SOURCE = "eia_regional_estimate"


# ---------------------------------------------------------------------------
# D-07 -- the derivation formula's two pinned choices. Both are Claude's
# Discretion per 21-CONTEXT.md, but per D-09 the choice is pinned HERE,
# before the measurement plan 21-03 runs, never after.

# The statistic is the MEDIAN, not the mean, of each routable station's
# absolute deviation from its own PADD's mean retail price, pooled across
# all PADDs. Median because the OPIS price distribution has a long tail
# (see `verify_stations.py`'s own duplicate-group spread figures --
# $0.090 median vs $0.900 max), and the mean would let a handful of
# outlier stations set a planning constant that then charges every
# estimate-priced purchase in the country.
DISPERSION_STATISTIC_NAME = "median_absolute_deviation_from_padd_mean"

# The typical fill is the MEDIAN of `FuelStop.gallons` over the shipped
# plans for the twelve `CORRIDORS` (`test_corridor_fixtures.py`) at the
# pinned UI-default vehicle (6.5 mpg / 1,050 mi tank) -- not an invented
# industry figure. Derived from data already produced by this repo's own
# solver, which is D-07's whole point: a real derivation chain from data
# already in the repo, fully offline, no new source.
TYPICAL_FILL_RULE_NAME = "median_purchased_gallons_across_the_twelve_pinned_corridors"

# Three rungs, mirroring `PENALTY_LADDER`'s three-value shape
# (`test_solver_fixed_charge_optimality.py`). The middle rung (1x) is the
# unmultiplied sourced value and is therefore D-08's fallback when nothing
# in the sweep moves.
MARGIN_LADDER_MULTIPLIERS = (Decimal("0.5"), Decimal("1"), Decimal("2"))

# D-08's N: the adoption rule fires on the smallest rung that changes the
# stop set on at least this many of the twelve pinned corridors. Following
# 18.1's `RECOVERY_MIN_CELLS=1` precedent (`test_dispatch_recovery.py`) --
# the smallest value that demonstrably matters is the most conservative
# choice of margin, since a higher bar would let a rung that visibly
# changes real corridors go unadopted.
ADOPTION_MIN_MOVED_CORRIDORS = 1

# The selector's seed. Distinct from `CORPUS_PARAMS.seed` (20260730,
# `test_solver_fixed_charge_optimality.py`) and `PRUNE_CORPUS_PARAMS.seed`
# (20260731, `test_prune_soundness.py`) so the three pinned corpora never
# interleave draws from the same state -- today's date, following both
# precedents' own "seed is today's date" convention.
TAG_SEED = 20260807

# D-11's share ladder, fixed before any measurement is taken against it.
TAG_SHARE_LADDER = (Decimal("0.10"), Decimal("0.25"), Decimal("0.50"))

# D-09: explicitly unset. Plan 21-03 is the ONLY plan permitted to give
# this a value -- by inverting `PinnedConstantsAreUnmeasuredTests` below,
# never by deleting it. Its being `None` here, committed before any
# dispersion figure existed, is what makes "the ladder predates the
# number" a checkable property of the git history rather than an
# assertion in a SUMMARY.
MARGIN_LADDER = None

# D-09/D-07: also explicitly unset for the same reason as `MARGIN_LADDER`
# above. Plan 21-03 instantiates this alongside the ladder, since both
# come out of the same dispersion measurement pass (per-PADD averages are
# a byproduct of computing the per-PADD deviations `MARGIN_LADDER`'s base
# is derived from).
PADD_AVERAGE_PRICE = None


def derive_margin_rungs(dispersion_usd_per_gallon, typical_fill_gallons):
    """Apply D-07's derivation formula to a measured dispersion figure.

    `base = dispersion_usd_per_gallon * typical_fill_gallons` -- dollars of
    price uncertainty per typical purchase. Returns the three
    `MARGIN_LADDER_MULTIPLIERS` applied to `base`, each independently
    quantized to cents with `ROUND_HALF_UP` (never the ambient Decimal
    context's default `ROUND_HALF_EVEN`, which would silently round some
    rungs down instead of up on an exact .xx5 boundary). The result is
    strictly increasing whenever `base` is positive, because the
    multipliers themselves are strictly increasing.

    At `dispersion_usd_per_gallon == 0` this returns `(0, 0, 0)` --
    degenerate but well-defined, since a corpus with zero measured price
    dispersion has no basis for any margin at all.

    Both parameters MUST be `Decimal`, never `float` -- this value
    eventually enters the DP's exact-rational fixed-charge tick machinery
    (`dp.py`'s `Fraction(...)/_money_scale` -> `P_NUM/P_DEN` shape), where
    float imprecision is a documented, previously-hit landmine
    (`_exact_sub`'s existence).

    Raises `ValueError` if either argument is negative -- neither a price
    dispersion nor a fuel fill can be meaningfully negative.
    """
    if dispersion_usd_per_gallon < 0:
        raise ValueError(
            f"dispersion_usd_per_gallon must be >= 0, got {dispersion_usd_per_gallon!r}"
        )
    if typical_fill_gallons < 0:
        raise ValueError(f"typical_fill_gallons must be >= 0, got {typical_fill_gallons!r}")

    base = dispersion_usd_per_gallon * typical_fill_gallons
    cent = Decimal("0.01")
    return tuple(
        (base * multiplier).quantize(cent, rounding=ROUND_HALF_UP)
        for multiplier in MARGIN_LADDER_MULTIPLIERS
    )


_TAG_MODULUS = 10_000


def is_tagged(opis_id, share):
    """D-11's deterministic tagging selector.

    Uses `hashlib.blake2b` over `f"{TAG_SEED}:{opis_id}"`, `digest_size=8`,
    read as a big-endian integer modulo `_TAG_MODULUS` and compared against
    `share * _TAG_MODULUS`. Deliberately NOT Python's built-in randomized
    string hash routine -- that routine is `PYTHONHASHSEED`-seeded fresh
    per process by design, so a selector built on it would silently tag a
    different corpus on a different process (a different CI run, a
    different developer's machine) and destroy the reproducibility D-11's
    whole citability argument rests on. `hashlib.blake2b` carries no such
    per-process seed; the same `(TAG_SEED, opis_id, share)` triple always
    produces the same tag decision everywhere, forever, proven by
    `IsTaggedTests.test_stable_across_a_fresh_python_interpreter` below.

    `share=0` never tags anything (`0 * _TAG_MODULUS == 0`, and the digest
    value is never negative); `share=1` always tags everything
    (`1 * _TAG_MODULUS == _TAG_MODULUS`, strictly above every possible
    digest value, which ranges `0` to `_TAG_MODULUS - 1`).
    """
    digest = hashlib.blake2b(f"{TAG_SEED}:{opis_id}".encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, byteorder="big") % _TAG_MODULUS
    return Decimal(value) < share * _TAG_MODULUS


def tagged_candidates(candidates, share):
    """Apply `is_tagged()` to every candidate in `candidates` at `share`.

    Returns a NEW list. Each tagged candidate is a `dataclasses.replace(...)`
    copy carrying `price_source=ESTIMATE_PRICE_SOURCE`, with every other
    field byte-identical to the original. Every untagged candidate is
    returned completely unchanged, by object identity (not merely equal
    field values) -- the caller can rely on `candidate is result[i]` for
    every untagged position.
    """
    return [
        replace(candidate, price_source=ESTIMATE_PRICE_SOURCE)
        if is_tagged(candidate.opis_id, share)
        else candidate
        for candidate in candidates
    ]


# ---------------------------------------------------------------------------
# Task 1 tests -- derive_margin_rungs / is_tagged / tagged_candidates, plus
# the MARGIN_LADDER/PADD_AVERAGE_PRICE "still unmeasured" guard.


class DeriveMarginRungsTests(SimpleTestCase):
    def test_zero_dispersion_returns_all_zero_but_well_defined(self):
        result = derive_margin_rungs(Decimal("0"), Decimal("100"))
        self.assertEqual(result, (Decimal("0.00"), Decimal("0.00"), Decimal("0.00")))

    def test_positive_dispersion_and_fill_returns_strictly_increasing_positive_rungs(self):
        result = derive_margin_rungs(Decimal("0.05"), Decimal("500"))
        self.assertEqual(result, (Decimal("12.50"), Decimal("25.00"), Decimal("50.00")))
        self.assertTrue(result[0] < result[1] < result[2])
        self.assertTrue(all(r > 0 for r in result))

    def test_middle_rung_equals_base_quantized_to_cents(self):
        dispersion = Decimal("0.05")
        fill = Decimal("500")
        result = derive_margin_rungs(dispersion, fill)
        expected_middle = (dispersion * fill).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.assertEqual(result[1], expected_middle)

    def test_quantizes_with_round_half_up_not_the_ambient_rounding_mode(self):
        # base = 0.25 * 1 = 0.25; the 0.5x rung is 0.125, an exact .xx5
        # boundary. ROUND_HALF_UP gives 0.13; the ambient Decimal context's
        # default ROUND_HALF_EVEN would give 0.12 (12 is even). This test
        # fails if ROUND_HALF_UP is ever dropped in favor of the ambient
        # default.
        result = derive_margin_rungs(Decimal("0.25"), Decimal("1"))
        self.assertEqual(result, (Decimal("0.13"), Decimal("0.25"), Decimal("0.50")))

    def test_negative_dispersion_raises_value_error(self):
        with self.assertRaises(ValueError):
            derive_margin_rungs(Decimal("-0.01"), Decimal("100"))

    def test_negative_fill_raises_value_error(self):
        with self.assertRaises(ValueError):
            derive_margin_rungs(Decimal("0.05"), Decimal("-1"))


class IsTaggedTests(SimpleTestCase):
    def test_stable_across_repeated_calls_in_the_same_process(self):
        for opis_id in (1, 42, 6290, 999999):
            first = is_tagged(opis_id, Decimal("0.25"))
            second = is_tagged(opis_id, Decimal("0.25"))
            self.assertEqual(first, second)

    def test_zero_share_never_tags(self):
        for opis_id in range(200):
            self.assertFalse(is_tagged(opis_id, Decimal(0)))

    def test_full_share_always_tags(self):
        for opis_id in range(200):
            self.assertTrue(is_tagged(opis_id, Decimal(1)))

    def test_realised_share_within_two_sided_tolerance_of_requested_share(self):
        # The anti-vacuity guard for the selector: a selector that tags
        # everything or nothing passes every OTHER property in this class
        # trivially (`test_zero_share_never_tags` and
        # `test_full_share_always_tags` alone cannot catch that -- a
        # constant-False-except-at-share>=1 selector would still pass
        # both). Only a two-sided bound on the realised share at an
        # intermediate share value catches it, because a selector pinned
        # at all-True or all-False fails the lower or upper bound
        # respectively.
        sample_size = 5000
        tolerance = Decimal("0.03")
        for share in TAG_SHARE_LADDER:
            tagged_count = sum(1 for opis_id in range(sample_size) if is_tagged(opis_id, share))
            realised_share = Decimal(tagged_count) / Decimal(sample_size)
            self.assertGreaterEqual(
                realised_share,
                share - tolerance,
                f"share={share!r} realised={realised_share!r} below the lower bound",
            )
            self.assertLessEqual(
                realised_share,
                share + tolerance,
                f"share={share!r} realised={realised_share!r} above the upper bound",
            )

    def test_stable_across_a_fresh_python_interpreter(self):
        # No PYTHONHASHSEED dependence: a brand-new subprocess (which gets
        # its own, independently-randomized default PYTHONHASHSEED unless
        # this test forces one) reimplements is_tagged()'s exact formula
        # from raw hashlib primitives with no import of this module at
        # all, and must still agree with this process's own real result --
        # proving the tag decision does not silently ride on the builtin
        # randomized string hash routine's per-process seed.
        opis_id = 424242
        share = Decimal("0.25")
        here = is_tagged(opis_id, share)

        script = (
            "import hashlib\n"
            f"digest = hashlib.blake2b(f'{TAG_SEED}:{opis_id}'.encode('utf-8'), digest_size=8).digest()\n"
            "value = int.from_bytes(digest, byteorder='big') % 10_000\n"
            f"print(value < {share} * 10_000)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        fresh_process_result = result.stdout.strip() == "True"
        self.assertEqual(fresh_process_result, here)


class TaggedCandidatesTests(SimpleTestCase):
    def _candidate(self, opis_id, name="Station"):
        return Candidate(
            name=name,
            opis_id=opis_id,
            price_per_gallon=Decimal("3.50"),
            distance_from_start_mi=Decimal("100"),
        )

    def test_tagged_candidates_get_estimate_provenance_others_untouched(self):
        candidates = [self._candidate(opis_id) for opis_id in range(50)]
        share = Decimal("0.25")
        result = tagged_candidates(candidates, share)

        self.assertEqual(len(result), len(candidates))
        for original, replaced in zip(candidates, result):
            if is_tagged(original.opis_id, share):
                self.assertEqual(replaced.price_source, ESTIMATE_PRICE_SOURCE)
                self.assertEqual(replaced.opis_id, original.opis_id)
                self.assertEqual(replaced.price_per_gallon, original.price_per_gallon)
                self.assertEqual(
                    replaced.distance_from_start_mi, original.distance_from_start_mi
                )
                self.assertIsNot(replaced, original)
            else:
                self.assertIs(replaced, original)
                self.assertEqual(replaced.price_source, "opis_indexed")

    def test_at_least_one_tagged_and_one_untagged_in_the_fixture(self):
        # A weak non-vacuity check on the fixture itself: if share=0.25
        # over 50 ids tagged either everyone or no one, the test above
        # would pass vacuously without ever exercising both branches.
        candidates = [self._candidate(opis_id) for opis_id in range(50)]
        share = Decimal("0.25")
        tagged_flags = [is_tagged(c.opis_id, share) for c in candidates]
        self.assertTrue(any(tagged_flags))
        self.assertFalse(all(tagged_flags))


class PinnedConstantsAreUnmeasuredTests(SimpleTestCase):
    def test_margin_ladder_and_padd_average_price_are_none(self):
        # Plan 21-03 inverts this test (rather than deleting it) once the
        # dispersion measurement runs. Until then, this passing assertion
        # is the checkable proof that no numeric margin value or PADD
        # average existed when the rule above was pinned.
        self.assertIsNone(MARGIN_LADDER)
        self.assertIsNone(PADD_AVERAGE_PRICE)

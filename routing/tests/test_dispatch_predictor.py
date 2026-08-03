"""Pinned verdict rule, closed predictor family and anti-vacuity retention
floor for the one question this module and the plans built on top of it
exist to answer: **is `dp.estimate_transition_count` salvageable as a
runtime predictor for `solver.solve()`'s dispatch decision, or does it need
replacing?**

Every constant below is fixed in this module's own commit, before any
measurement command or in-suite guard exists in this file and before the
sweep has ever been run -- the same discipline
`routing/tests/test_eia_penalty_sweep.py` (D-25) and
`routing/tests/test_solver_latency.py` (D-19/D-21) already established.
Neither `measure_dispatch_predictor` (a later commit) nor
`DispatchPredictorGuardTests` (also a later commit, same file) defines a
copy of its own -- both import every one of the seven names below, this
module's own single shared source of truth.

## The incumbent's inversion

Two witnesses, both already on the record before this module's own
measurement ever ran:

1. **Committed workstation evidence** (`dp.py`'s own `DP_TRANSITION_BUDGET`
   calibration comment, read in its own stated order): `dallas_tx-seattle_wa`
   at a 500 mi tank estimates **61,944** and took the worst-of-3 raw DP
   **2.706s**; the SAME corridor at a 1,050 mi tank estimates **117,895** --
   90% larger -- and took the worst-of-3 raw DP **2.181s**, 19% FASTER. A
   90% larger estimate ran faster, not slower.

2. **Live production evidence**, pre-hotfix (commit `8946567`): the same
   corridor at 500 mi (estimate 61,912) returned HTTP 500, reproduced 5/5 at
   30.5-35.7s, right-censored at the deployed `GUNICORN_TIMEOUT=30`; at
   1,050 mi (estimate 117,852) it returned HTTP 200 in ~12s. The same
   inversion, larger.

**The structural consequence.** `solver.solve()` dispatches by comparing
`dp.estimate_transition_count(...)` against `dp.DP_TRANSITION_BUDGET` --
a single scalar THRESHOLD. Because the two witness cells above invert (the
smaller estimate is the slower one), no threshold value can simultaneously
retain the fast cell (117,895) and demote the slow one (61,912/61,944):
either both sit on the same side of any single boundary, or the boundary
puts the SLOW cell on the "exact_dp" side and the FAST one on the
"heuristic" side -- backwards. That is a property of the estimator itself,
never a property of the specific number 50,000 or 134,000 the hotfix or
its predecessor chose.

## What this module does NOT do

It does not change `solver.py`'s dispatch, does not move
`dp.DP_TRANSITION_BUDGET` or any other threshold, and does not touch
`routing/services/` at all (`git diff --stat routing/services/` must stay
empty across every commit this module's own plan produces). Selecting and
wiring whichever predictor (if any) qualifies is plan 18-12's job, and
18-12 calibrates against plan 18-11's DEPLOYED-hardware measurement, never
against this module's workstation figures alone.
"""
import bisect
import collections
import io
import statistics
import time
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from routing.services import corridor, dp
from routing.services.prune import prune_dominated_candidates
from routing.tests.test_corridor_fixtures import (
    PRICE_BASIS_NEUTRAL,
    TANK_RANGES_MI,
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_solver_dispatch import CALIBRATION_CELLS, MPG, PENALTY, STARTING_FUEL

# --- PREDICTOR_CELLS -------------------------------------------------------
#
# The exact twelve (corridor slug, tank range) cells `dp.DP_TRANSITION_BUDGET`'s
# own calibration comment and `test_solver_dispatch.CALIBRATION_CELLS` already
# measure -- imported, not redeclared, per this module's own single-shared-
# source-of-truth discipline (see module docstring). Reusing this exact set,
# rather than inventing a new one, ties this plan's measurement to the SAME
# already-committed evidence the inversion witnesses above cite, so no new
# corridor selection can be second-guessed as chosen to produce a particular
# verdict.
#
# This set spans BOTH entries of `TANK_RANGES_MI` for THREE corridors, not
# merely the one this module's docstring calls out: `dallas_tx-seattle_wa`
# (the named inversion witness), `toronto_oh-hillsboro_or`, and
# `el_paso_tx-portland_me`. The inversion is a tank-range effect -- a
# corridor measured at only one tank range could never expose it, because
# there is nothing to invert AGAINST -- so a matrix collapsed to a single
# tank range would make this whole measurement unable to observe the very
# thing it exists to test. `DispatchPredictorGuardTests` (added by a later
# commit in this same file) pins this non-collapse property permanently.
PREDICTOR_CELLS = CALIBRATION_CELLS

# --- PREDICTOR_CELL_BUDGET_SECONDS ------------------------------------------
#
# Per-cell wall-clock budget after which a cell is recorded as right-censored
# rather than measured to completion. Censored cells are RECORDED, never
# dropped -- see `CENSORED_RANK_TREATMENT` below for how a censored cell
# enters the rank statistic.
#
# Chosen from two already-known figures, not from a fresh measurement of its
# own: the deployed `GUNICORN_TIMEOUT=30` (`render.yaml` / `entrypoint.sh`),
# plus the already-measured `toronto_oh-hillsboro_or` raw-DP figures
# (`18-04c-SUMMARY.md`'s own calibration table): 31.190s @500mi and 43.298s
# @1050mi. 35 sits 5 seconds above the deployed timeout (headroom for this
# sweep's own process-spawn overhead, never claimed as a production budget)
# and lands cleanly BETWEEN the two known Toronto figures -- @500mi
# (31.190s) would complete under it, @1050mi (43.298s) would not -- so this
# budget is derived from real, already-recorded evidence, not picked to
# produce a particular censored/uncensored split.
PREDICTOR_CELL_BUDGET_SECONDS = 35

# --- CENSORED_RANK_TREATMENT -------------------------------------------------
#
# How a censored cell enters the rank statistic: it is recorded at exactly
# `PREDICTOR_CELL_BUDGET_SECONDS` and therefore takes the MAXIMUM runtime
# rank among all measured cells (ties broken by the standard averaged-rank
# convention `statistics.correlation(..., method="ranked")` already applies
# to any tie, censored or not).
#
# This treatment deliberately FAVOURS the incumbent estimator: every cell
# this sweep is likely to censor (see `PREDICTOR_CELLS`'s own comment on
# Toronto/El Paso) is ALSO one of the highest-`estimate_transition_count`
# cells in this pinned matrix, so scoring it as "the slowest" makes it
# CONCORDANT with the incumbent's own high estimate, not discordant. A
# treatment chosen to help the incumbent's case, under which the incumbent
# STILL fails the pinned rule, cannot be dismissed later as an artifact of
# how censoring was scored -- that is the whole reason this constant, and
# its favourable direction, is pinned here, before the sweep runs, rather
# than chosen after seeing which cells actually censor.
CENSORED_RANK_TREATMENT = "max_rank"

# --- CANDIDATE_PREDICTORS ----------------------------------------------------
#
# The CLOSED, named family of predictor functions to be scored, each a pure
# function of the same four arguments `dp.estimate_transition_count` already
# takes (`candidates, *, total_route_mi, tank_range_mi, starting_fuel`) --
# so any member is a drop-in dispatch input, exactly like the incumbent.
#
# This family is declared closed here, in this commit, before any cell has
# been measured against it: adding a member after the numbers land is
# FORBIDDEN. A member that fails the pinned rule is recorded as failing,
# never quietly removed -- an empty qualifying shortlist is an explicitly
# permitted, real outcome (see `measure_dispatch_predictor`'s own module
# docstring).
#
# The incumbent is an ORDINARY member of this family, scored by the
# identical rule as every challenger -- never given a separate, looser
# standard.
#
# The remaining six members are systematically built from the three
# structural quantities `solve_fixed_charge`'s own recurrence exposes
# (candidate count, the route-length-to-tank-range ratio, and the summed
# count of distinct useful fill levels -- a LINEAR analogue of the
# incumbent's own quadratic reach term, `reach + 1` summed rather than
# `reach * (reach + 1)`), plus each of those three multiplied by the
# incumbent's own reach term itself -- the "products/combinations ... with
# the incumbent's own reach term" category.


def _sorted_positions(candidates):
    return sorted(c.distance_from_start_mi for c in candidates)


def _candidate_count(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Predictor family member: raw post-prune candidate count. The
    simplest possible structural proxy -- no positional information at
    all, no reach computation."""
    return len(candidates)


def _route_tank_ratio(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Predictor family member: route-length-to-tank-range ratio alone,
    with NO dependence on candidate count whatsoever -- tests whether
    route geometry by itself (independent of how densely it is
    populated) explains DP runtime."""
    return float(total_route_mi / tank_range_mi)


def _fill_level_reach_sum(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Predictor family member: summed count of distinct useful fill
    levels across START plus every candidate -- the LINEAR analogue of
    the incumbent's own `reach * (reach + 1)` quadratic sum (`reach + 1`
    summed instead), computed independently via the same
    sorted-positions-plus-`bisect_right` technique
    `estimate_transition_count` uses internally, never by calling into
    that function's own private closures. Deliberately plain `Decimal`
    arithmetic, not the exact-integer-tick machinery
    `solve_fixed_charge`/`estimate_transition_count` use for their
    money/position-domain correctness guarantees -- adequate for a
    structural correlation predictor, which carries none of those
    functions' exactness obligations.
    """
    positions = _sorted_positions(candidates)
    node_count = len(positions)
    full = positions + [total_route_mi]
    start_fuel = starting_fuel * tank_range_mi

    total = bisect.bisect_right(full, start_fuel)
    for i in range(node_count):
        reach = bisect.bisect_right(full, positions[i] + tank_range_mi, i + 1) - (i + 1)
        total += reach + 1
    return total


def _candidate_count_x_incumbent(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Predictor family member: candidate count multiplied by the
    incumbent's own estimate -- a combination member, per this family's
    closing category."""
    incumbent = dp.estimate_transition_count(
        candidates,
        total_route_mi=total_route_mi,
        tank_range_mi=tank_range_mi,
        starting_fuel=starting_fuel,
    )
    return len(candidates) * incumbent


def _route_tank_ratio_x_incumbent(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Predictor family member: the route/tank ratio multiplied by the
    incumbent's own estimate -- a combination member, per this family's
    closing category."""
    incumbent = dp.estimate_transition_count(
        candidates,
        total_route_mi=total_route_mi,
        tank_range_mi=tank_range_mi,
        starting_fuel=starting_fuel,
    )
    ratio = _route_tank_ratio(
        candidates,
        total_route_mi=total_route_mi,
        tank_range_mi=tank_range_mi,
        starting_fuel=starting_fuel,
    )
    return ratio * incumbent


def _fill_level_reach_sum_x_incumbent(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Predictor family member: the linear fill-level-reach sum
    multiplied by the incumbent's own estimate -- a combination member,
    per this family's closing category."""
    incumbent = dp.estimate_transition_count(
        candidates,
        total_route_mi=total_route_mi,
        tank_range_mi=tank_range_mi,
        starting_fuel=starting_fuel,
    )
    linear = _fill_level_reach_sum(
        candidates,
        total_route_mi=total_route_mi,
        tank_range_mi=tank_range_mi,
        starting_fuel=starting_fuel,
    )
    return linear * incumbent


CANDIDATE_PREDICTORS = (
    ("estimate_transition_count", dp.estimate_transition_count),
    ("candidate_count", _candidate_count),
    ("route_tank_ratio", _route_tank_ratio),
    ("fill_level_reach_sum", _fill_level_reach_sum),
    ("candidate_count_x_incumbent", _candidate_count_x_incumbent),
    ("route_tank_ratio_x_incumbent", _route_tank_ratio_x_incumbent),
    ("fill_level_reach_sum_x_incumbent", _fill_level_reach_sum_x_incumbent),
)

# --- PREDICTOR_RANK_FLOOR -----------------------------------------------------
#
# Minimum Spearman rank correlation (`statistics.correlation(...,
# method="ranked")`) between a predictor's value and measured DP solve time
# for that predictor to be called predictive.
#
# Pinned from what a dispatch THRESHOLD actually needs -- near-monotone
# ordering across the whole measured range, because a threshold is a single
# cut point and any pair of cells on the wrong side of it is a live dispatch
# mistake -- not from a conventional statistical cutoff (0.5-0.7 "moderate
# correlation" is a survey-research convention with no bearing on whether a
# SINGLE THRESHOLD can safely separate two populations). 0.90 is chosen
# because it demands the predictor get the ordering right on all but a
# small residual of this pinned twelve-cell matrix; a predictor clearing a
# looser bar could still leave exactly the kind of inverted pair this
# module's docstring documents unresolved.
PREDICTOR_RANK_FLOOR = 0.90

# --- PREDICTOR_INVERSION_BUDGET -----------------------------------------------
#
# Maximum number of discordant cell PAIRS tolerated -- the sharper
# condition a rank-correlation floor alone cannot enforce, since a high
# correlation can still coexist with the one specific inverted pair that
# actually matters (exactly what this module's docstring's two witnesses
# show: the incumbent's overall estimate ordering across the twelve pinned
# cells is otherwise sane, but the two Dallas-Seattle cells invert).
#
# Pinned at ZERO: because `solver.solve()` dispatches on a single scalar
# threshold, ANY discordant pair in this measured matrix reproduces the
# exact structural failure mode this module's docstring already proves --
# some (retain, demote) outcome becomes unreachable by every threshold
# value. There is no principled non-zero tolerance for a fixed-charge
# dispatch guard whose entire job is getting that one boundary right; zero
# encodes the structural fact directly, rather than picking an arbitrary
# small integer.
PREDICTOR_INVERSION_BUDGET = 0

# --- DISPATCH_RETENTION_FLOOR -------------------------------------------------
#
# The ANTI-VACUITY guard. A predictor whose induced threshold demotes every
# cell trivially satisfies "no cell breaches its own induced threshold" --
# exactly as `prune(x) -> x` trivially satisfies every soundness property
# `routing/services/prune.py`'s own disproven reach-sliver rule was
# eventually caught by (see that module's "Why the reach-sliver rule is
# wrong" section). A predictor that clears `PREDICTOR_RANK_FLOOR` and
# `PREDICTOR_INVERSION_BUDGET` above must ALSO retain the exact DP on at
# least this many of the cells measured comfortably inside
# `PREDICTOR_CELL_BUDGET_SECONDS`, or it does not qualify -- a demote-
# everything policy is disqualified by construction, not merely by
# convention.
#
# Pinned at 5: this module's own docstring already lists SEVEN
# already-committed workstation figures, all comfortably under budget
# (houston 0.001s, phoenix 0.053s, dallas@500mi 2.706s,
# san_diego 2.542s, dallas@1050mi 2.181s, atlanta 8.546s, miami 13.008s --
# every one well under half of `PREDICTOR_CELL_BUDGET_SECONDS`). Five of
# seven is a majority of that already-known comfortable set, chosen before
# this sweep runs and before it is known which predictor, if any, would
# actually clear it.
DISPATCH_RETENTION_FLOOR = 5

# --- Permanent regression guard (Task 3) -------------------------------------
#
# The smallest cell pair, from `measure_dispatch_predictor`'s own measured
# table, that reproduces the incumbent's inversion: `dallas_tx-seattle_wa`
# at its two pinned tank ranges -- the exact pair this module's docstring
# names throughout, and the cheapest of the three discordant pairs that
# measured table's own `estimate_transition_count` row reports (both cells
# already run in close to a second each, unlike the two Toronto/El Paso
# cells that dominate this file's own `PREDICTOR_CELL_BUDGET_SECONDS`
# budget). Fresh measurement (this plan, same workstation) reproduced it
# again: @500mi estimates 61,944 and measured 1.3935s; @1050mi estimates
# 117,895 (90% larger) and measured 1.0904s (22% FASTER) -- the same
# inversion, not merely the historical one.
_INVERSION_WITNESS_SLUG = "dallas_tx-seattle_wa"
_INVERSION_WITNESS_SMALL_TANK_MI = Decimal("500")
_INVERSION_WITNESS_LARGE_TANK_MI = Decimal("1050")

# Repeats for this guard's own median-of-N timing (mirrors
# `SolverLatencyCeilingTests`' `_RATIO_GUARD_REPEATS` convention) -- both
# witness cells run in close to a second each, so 3 repeats costs a few
# seconds total, well within the ordinary suite's budget.
_GUARD_REPEATS = 3

# The minimum in-process wall-clock RATIO (smaller-estimate cell's median
# time / larger-estimate cell's median time) this guard requires to call
# the inversion reproduced. Freshly measured ratio (this plan):
# 1.3935s / 1.0904s ~= 1.278x; historically recorded ratio (dp.py's own
# calibration comment): 2.706s / 2.181s ~= 1.241x. 1.10 is pinned
# comfortably below both of those independently-measured figures, so
# ordinary machine noise cannot flake a genuine ~24-28% effect, while
# still requiring a real, non-trivial inversion rather than a coin-flip
# tie -- never an absolute wall-clock bound (the flake source D-19's own
# module docstring already retired one of), only a same-process RATIO
# between the two cells, mirroring the machine-independence argument
# `LATENCY_RATIO_CEILING` (`test_solver_latency.py`) already establishes.
_INVERSION_RATIO_FLOOR = Decimal("1.10")


class DispatchPredictorGuardTests(TestCase):
    """Permanent CI-enforcing guard for the finding this module's
    docstring records: `estimate_transition_count` does not predict
    `solve_fixed_charge`'s real runtime, so a future change to `dp.py`
    (a further optimization pass, a state-space restructuring, or simply
    routine maintenance) cannot silently make this finding disappear
    without a test noticing. This is the reason
    `DP_TRANSITION_BUDGET`'s own two successive calibrations (250,000,
    then 134,000) both put the dispatch boundary somewhere that could not
    actually work: the estimator they were calibrated against does not
    order cells the way real wall-clock time does. A finding recorded
    only in a planning artifact is invisible to a maintainer reading the
    code; this class is the same finding recorded where the code lives.

    Two assertions, both non-vacuous by construction (mirroring
    `PruneSliverRuleRegressionTests`' own two-witness shape in
    `test_prune_soundness.py`):

    1. The inversion itself is still reproducible, on the smallest cell
       pair known to show it.
    2. `PREDICTOR_CELLS` cannot quietly collapse to a single tank range --
       doing so would make assertion 1 unobservable in the first place,
       so this must fail BEFORE assertion 1 ever could.

    Reuses `RealCorridorDispatchTestCase`'s own `setUpTestData`
    CSV-seeding pattern (`test_solver_dispatch.py`) rather than inventing
    a second seeding path.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def test_predictor_cells_spans_both_tank_ranges_with_a_dual_range_corridor(self):
        """Anti-vacuity guard on the measurement matrix itself: a future
        edit that narrows `PREDICTOR_CELLS` to a single tank range would
        make the inversion below unobservable (there would be nothing
        left to invert AGAINST), so this must fail first, before that
        silent narrowing could ever hide assertion 1's own regression."""
        tank_ranges_seen = {tank for _slug, tank in PREDICTOR_CELLS}
        self.assertEqual(
            tank_ranges_seen,
            set(TANK_RANGES_MI),
            f"PREDICTOR_CELLS must span every entry of TANK_RANGES_MI "
            f"({set(TANK_RANGES_MI)!r}); saw only {tank_ranges_seen!r}. A "
            "matrix collapsed to one tank range cannot observe the "
            "tank-range-dependent inversion this module exists to guard.",
        )

        slug_counts = collections.Counter(slug for slug, _tank in PREDICTOR_CELLS)
        self.assertTrue(
            any(count >= 2 for count in slug_counts.values()),
            "PREDICTOR_CELLS must contain at least one corridor measured "
            f"at BOTH tank ranges; saw per-corridor counts {dict(slug_counts)!r}.",
        )

    def test_incumbent_inversion_is_still_reproducible_on_the_smallest_pair(self):
        """The incumbent's own estimate orders the two witness cells one
        way (500mi's smaller estimate before 1050mi's larger one); real,
        in-process measured DP work orders them the OTHER way (500mi
        takes longer). Uses an in-process wall-clock RATIO between the
        two cells, never an absolute second count -- see
        `_INVERSION_RATIO_FLOOR`'s own comment for why."""
        factor_for = factor_lookup_for_basis(PRICE_BASIS_NEUTRAL)
        route = load_corridor_route(_INVERSION_WITNESS_SLUG)
        raw_candidates = corridor.candidates(route, factor_for=factor_for)

        search_small = prune_dominated_candidates(
            raw_candidates,
            tank_range_mi=_INVERSION_WITNESS_SMALL_TANK_MI,
            total_route_mi=route.total_route_mi,
        )
        search_large = prune_dominated_candidates(
            raw_candidates,
            tank_range_mi=_INVERSION_WITNESS_LARGE_TANK_MI,
            total_route_mi=route.total_route_mi,
        )

        estimate_small = dp.estimate_transition_count(
            search_small,
            total_route_mi=route.total_route_mi,
            tank_range_mi=_INVERSION_WITNESS_SMALL_TANK_MI,
            starting_fuel=STARTING_FUEL,
        )
        estimate_large = dp.estimate_transition_count(
            search_large,
            total_route_mi=route.total_route_mi,
            tank_range_mi=_INVERSION_WITNESS_LARGE_TANK_MI,
            starting_fuel=STARTING_FUEL,
        )
        self.assertLess(
            estimate_small,
            estimate_large,
            "witness precondition: the small-tank cell's own incumbent "
            "estimate must be the SMALLER of the two for this pair to be "
            f"an inversion witness at all (got small={estimate_small}, "
            f"large={estimate_large})",
        )

        def _median_seconds(search_set, tank_range_mi):
            times = []
            for _ in range(_GUARD_REPEATS):
                started = time.perf_counter()
                dp.solve_fixed_charge(
                    search_set,
                    total_route_mi=route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    starting_fuel=STARTING_FUEL,
                    penalty=PENALTY,
                )
                times.append(time.perf_counter() - started)
            return statistics.median(times)

        time_small = _median_seconds(search_small, _INVERSION_WITNESS_SMALL_TANK_MI)
        time_large = _median_seconds(search_large, _INVERSION_WITNESS_LARGE_TANK_MI)
        ratio = (
            Decimal(str(time_small)) / Decimal(str(time_large))
            if time_large > 0
            else Decimal("Infinity")
        )

        self.assertGreaterEqual(
            ratio,
            _INVERSION_RATIO_FLOOR,
            f"expected the SMALLER-estimate cell (estimate={estimate_small}, "
            f"tank={_INVERSION_WITNESS_SMALL_TANK_MI}mi, median={time_small}s) "
            f"to take at least {_INVERSION_RATIO_FLOOR}x as long as the "
            f"LARGER-estimate cell (estimate={estimate_large}, "
            f"tank={_INVERSION_WITNESS_LARGE_TANK_MI}mi, median={time_large}s) "
            f"-- measured ratio={ratio}. If this no longer holds, the "
            "incumbent's inversion may have been fixed (e.g. by a DP "
            "optimization that changed its relative cost across tank "
            "ranges) and this guard should be reviewed, not loosened "
            "blindly.",
        )

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

from routing.services import dp
from routing.tests.test_solver_dispatch import CALIBRATION_CELLS

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

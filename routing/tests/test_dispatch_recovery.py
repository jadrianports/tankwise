"""Pinned rules for phase 18.1's dispatch-quality-recovery measurements --
the single shared source of truth every later plan in this phase measures
against.

Every constant and function below was committed in THIS module's own
commit, strictly BEFORE any measurement this phase takes against them:
before `measure_dispatch_grid` (plan 07) replays the widened grid, before
the time-boxed DP exists to breach or complete against a deadline (plan
04), and before a single live probe (plan 09) is issued against the
deployed instance. `measure_dispatch_grid` and the live probe IMPORT every
rule from this module and never redeclare one -- the same single-source-
of-truth discipline `routing/tests/test_dispatch_predictor.py` already
established for plan 18-10's predictor verdict.

This module defines no corridor, cell, vehicle or budget of its own beyond
the ones named here. It settles four numbers CONTEXT.md (18.1-CONTEXT.md)
explicitly delegates to the planner:

  - D-03: `DP_TRANSITION_BUDGET_LADDER` + `adopt_budget_rung()`
  - D-06: `DEADLINE_CHECK_STRIDE_LADDER` + `DEADLINE_OVERSHOOT_BUDGET_SECONDS`
    + `adopt_stride_rung()`
  - D-09/D-10: `RESPONSE_BAR_SECONDS`, `HEURISTIC_FALLBACK_ALLOWANCE_SECONDS`,
    `ROUTE_ALTERNATIVES_FANOUT`, `GUNICORN_TIMEOUT_SECONDS`,
    `GUNICORN_TIMEOUT_MARGIN_SECONDS`, `derive_dp_deadline_seconds()`
  - D-17: `LIVE_PROBE_MAX_CELLS` + `select_live_probe_cells()`
  - D-18: `RECOVERY_MIN_CELLS` + `recovery_verdict()` +
    `measurement_floor_violations()`

This is the project's standing convention -- `PENALTY_LADDER`,
`EIA_MULTIPLIER_LADDER`, `DISPATCH_RETENTION_FLOOR`, `EXACT_DP_REACH_FLOOR`,
`PRUNE_REDUCTION_FLOOR`, `PREDICTOR_INVERSION_BUDGET` are all pinned before
their measurements -- and it is what makes ROADMAP criterion 3's "no
corridor may be tuned to make `dallas_tx-seattle_wa` pass" checkable
rather than merely asserted. `PinnedRecoveryRuleGuardTests` below is this
module's own permanent guard that none of these rules is degenerate.
"""
import unittest
from decimal import ROUND_DOWN, Decimal

from django.test import SimpleTestCase

from routing.services import dp
from routing.tests import test_live_latency_probe
from routing.tests.test_live_latency_probe import (
    LIVE_PROBE_CACHE_BUST_LADDER,
    LIVE_PROBE_MAX_REQUESTS,
    LIVE_PROBE_REPEATS,
)

# ---------------------------------------------------------------------------
# D-03 -- DP_TRANSITION_BUDGET_LADDER + adopt_budget_rung()
#
# Pinned 2026-08-04 (plan 18.1-01), BEFORE the time-boxed DP exists to
# generate a single `worst_timed_response_seconds` figure against any rung
# above 50,000. The trailing `None` rung means "no estimate gate at all" --
# every candidate reaches the DP, backstopped only by the wall-clock
# deadline.
#
# What each rung newly admits relative to the current 50,000 boundary,
# from RESEARCH.md's measured 24-cell grid (mpg=10, starting_fuel=0.5,
# price_basis=neutral -- the exact vehicle non-scalar-dispatch-rule.md's
# 41.7% figure used):
#   70,000    admits dallas_tx-seattle_wa@500mi (61,944, the known
#             live-breaching cell) and san_diego_ca-jacksonville_fl@500mi
#             (66,571).
#   130,000   additionally admits dallas_tx-seattle_wa@1050mi (117,895 --
#             ROADMAP criterion 1's own worked example, live-fine
#             pre-hotfix at ~12s).
#   200,000   additionally admits atlanta_ga-denver_co@500mi (150,905) and
#             miami_fl-boston_ma@500mi (182,506).
#   400,000   additionally admits jacksonville_fl-bangor_me@500mi
#             (356,085 -- already measured at 19.327s workstation, well
#             past any plausible deadline; admitted cells at this rung
#             routinely breach and fall back).
#   None      additionally admits el_paso_tx-portland_me and
#             toronto_oh-hillsboro_or at both tank ranges (workstation
#             projections in the multi-minute range).
DP_TRANSITION_BUDGET_LADDER = (50_000, 70_000, 130_000, 200_000, 400_000, None)


def adopt_budget_rung(rows):
    """Adopt the largest `DP_TRANSITION_BUDGET_LADDER` rung whose
    newly-admitted cells hold up under measurement.

    `rows`: iterable of dicts, each carrying at least:
      - "slug": corridor slug (str)
      - "tank_range_mi": Decimal
      - "estimate": int, `dp.estimate_transition_count`'s output
      - "worst_timed_response_seconds": Decimal, the worst-of-repeats
        total end-to-end-equivalent response time for that cell under the
        time-boxed dispatch (attempt-or-breach-and-fallback, whichever the
        cell actually did)
      - "breached": bool, True if this cell's DP attempt raised
        `dp.DeadlineExceededError` (fell back to the heuristic) rather
        than completing

    Rule: walking `DP_TRANSITION_BUDGET_LADDER` in ascending order
    (skipping the baseline rung, which is never "newly admitted" -- it is
    already the current policy), a rung QUALIFIES only when every cell
    newly admitted at that rung (estimate strictly above the previous
    rung, at or below this one) has `worst_timed_response_seconds <=
    RESPONSE_BAR_SECONDS`, AND at least one newly-admitted cell did not
    breach. The walk stops at the first rung that fails to qualify -- a
    later rung can never be adopted if an earlier one did not qualify.
    Returns `DP_TRANSITION_BUDGET_LADDER[0]` (the current 50,000 baseline)
    when no higher rung qualifies.

    Two outcome branches, both legitimate:
      - RAISED: a rung above 50,000 qualifies and is adopted.
      - NOT RAISED: none does, and 50,000 stands -- not a failure, a
        result this rule is required to be capable of producing.
    """
    rows = list(rows)
    adopted = DP_TRANSITION_BUDGET_LADDER[0]
    lower_bound = DP_TRANSITION_BUDGET_LADDER[0]
    for rung in DP_TRANSITION_BUDGET_LADDER[1:]:
        newly_admitted = [
            row
            for row in rows
            if row["estimate"] > lower_bound
            and (rung is None or row["estimate"] <= rung)
        ]
        all_within_bar = all(
            row["worst_timed_response_seconds"] <= RESPONSE_BAR_SECONDS
            for row in newly_admitted
        )
        at_least_one_completed = any(not row["breached"] for row in newly_admitted)
        if not (all_within_bar and at_least_one_completed):
            break
        adopted = rung
        lower_bound = rung if rung is not None else lower_bound
    return adopted


# ---------------------------------------------------------------------------
# D-06 -- DEADLINE_CHECK_STRIDE_LADDER, DEADLINE_OVERSHOOT_BUDGET_SECONDS,
# adopt_stride_rung()
#
# Pinned 2026-08-04 (plan 18.1-01), BEFORE the stride check exists in
# `dp.solve_fixed_charge` to measure any overshoot against it. Units are
# `(state, level)` pairs -- the `_reachable_ticks`-call granularity
# RESEARCH.md's Assumption A1 names as the natural check point (one check
# per level considered, sitting just above the innermost per-target loop,
# not inside it and not per outer node).
#
# RESEARCH.md's own measured per-call costs at this granularity: 77.7
# microseconds/call on atlanta_ga-denver_co@500mi, 99.2
# microseconds/call on jacksonville_fl-bangor_me@500mi. Both are
# MODERATE-density cells, not the densest a raised budget rung might
# admit -- Pitfall 3 (RESEARCH.md) requires re-measuring on the densest
# ADMITTED cell before a rung is adopted; a stride safe on these two
# moderate cells is not assumed safe on a denser one without that
# re-measurement.
DEADLINE_CHECK_STRIDE_LADDER = (500, 1_000, 2_000, 5_000)

DEADLINE_OVERSHOOT_BUDGET_SECONDS = Decimal("0.5")


def adopt_stride_rung(rows):
    """Adopt the largest `DEADLINE_CHECK_STRIDE_LADDER` rung whose worst
    measured overshoot stays inside `DEADLINE_OVERSHOOT_BUDGET_SECONDS`.

    `rows`: iterable of dicts, each carrying at least:
      - "stride": int, a member of `DEADLINE_CHECK_STRIDE_LADDER`
      - "worst_overshoot_seconds": Decimal, the worst observed
        `actual_elapsed - deadline` on the densest ADMITTED cell measured
        at that stride

    Rule: walking `DEADLINE_CHECK_STRIDE_LADDER` in ascending order,
    adopt the largest rung whose `worst_overshoot_seconds <=
    DEADLINE_OVERSHOOT_BUDGET_SECONDS`, stopping the walk at the first
    rung that overshoots the budget (or has no measurement row at all --
    an unmeasured rung cannot be adopted, by the same "cannot skip over a
    rung" discipline `adopt_budget_rung` uses). Returns
    `DEADLINE_CHECK_STRIDE_LADDER[0]` when even the finest-grained rung
    does not qualify -- the finest stride in this ladder is the floor;
    there is nothing finer to fall back to.
    """
    overshoot_by_stride = {row["stride"]: row["worst_overshoot_seconds"] for row in rows}
    adopted = DEADLINE_CHECK_STRIDE_LADDER[0]
    for stride in DEADLINE_CHECK_STRIDE_LADDER:
        if stride not in overshoot_by_stride:
            break
        if overshoot_by_stride[stride] > DEADLINE_OVERSHOOT_BUDGET_SECONDS:
            break
        adopted = stride
    return adopted


# ---------------------------------------------------------------------------
# D-09/D-10 -- the deadline derivation
#
# Pinned 2026-08-04 (plan 18.1-01), BEFORE any fresh live upstream
# measurement is taken during this phase's own execution.
#
# RESPONSE_BAR_SECONDS -- the total cold cache-miss response-time bar
# (D-10, locked). Recorded here explicitly, because it matters for
# criterion 3's integrity: the only cell with a known live `exact_dp`
# stress timing is `dallas_tx-seattle_wa`@1050mi at roughly 12 seconds
# pre-hotfix. A bar near 20 seconds would recover that cell; this 15-second
# bar will not. 15 was chosen on UX grounds with that fact already known,
# and is a deliberate refusal to tune toward the one corridor ROADMAP
# criterion 3 forbids tuning toward. Downstream work MUST NOT revisit this
# number in order to capture that cell.
RESPONSE_BAR_SECONDS = Decimal(15)

# 18-11's own live measurement of the penalty-aware heuristic solver stage
# (88-92ms, Dallas -> Seattle, deployed hardware) -- this allowance is
# roughly five times that, covering the fallback solve triggered by a
# breach without needing to re-measure the heuristic's own cost here.
HEURISTIC_FALLBACK_ALLOWANCE_SECONDS = Decimal("0.5")

# The divisor. `routing/services/mapbox.py::get_routes` requests
# `alternatives=true`; `routing/views.py::_solve_all_alternatives` calls
# `solver.solve()` once PER returned alternative inside a single
# `self._timing.stage("solver")` block; and `routing.timing.ServerTiming`
# ACCUMULATES a repeated stage name -- so a per-solve deadline is paid up
# to `ROUTE_ALTERNATIVES_FANOUT` times within one request, and the
# per-solve constant must be derived against the per-request bar divided
# by that fan-out. Mapbox Directions returns at most three routes for
# `alternatives=true`, so 3 is the conservative pin.
#
# Alternative considered and NOT taken: one cumulative per-request
# deadline shared across alternatives. Rejected because D-02 pins a fixed
# per-solve constant, and a shared budget would make a later
# alternative's outcome depend on an earlier alternative's runtime.
ROUTE_ALTERNATIVES_FANOUT = 3

# D-09's secondary check, sourced from `render.yaml`'s own
# `GUNICORN_TIMEOUT`. Decimal, like every other time threshold here, since
# it is compared directly against a Decimal-valued worst-case total.
GUNICORN_TIMEOUT_SECONDS = Decimal(30)
GUNICORN_TIMEOUT_MARGIN_SECONDS = Decimal(5)


def derive_dp_deadline_seconds(measured_upstream_seconds):
    """Derive the per-solve DP deadline (D-09), a pure function of the
    single measured upstream figure -- never a hand-picked number.

    `measured_upstream_seconds`: Decimal (or a value `Decimal(str(...))`
    can parse), the measured `corridor + route + eia + cache + index`
    stage total for one live request.

    Subtracts `measured_upstream_seconds`,
    `DEADLINE_OVERSHOOT_BUDGET_SECONDS` and
    `HEURISTIC_FALLBACK_ALLOWANCE_SECONDS` from `RESPONSE_BAR_SECONDS`,
    divides the remainder by `ROUTE_ALTERNATIVES_FANOUT`, and quantizes
    DOWN to one decimal place. Raises `ValueError` if the result is not
    strictly positive -- there is no room left for a deadline at all.

    Secondary check (D-09), NOT enforced inside this function -- the
    caller must confirm it separately (see
    `PinnedRecoveryRuleGuardTests.test_derive_dp_deadline_seconds_is_
    strictly_decreasing_positive_and_bounded` for the executable form):

        measured_upstream_seconds
        + ROUTE_ALTERNATIVES_FANOUT * (result + DEADLINE_OVERSHOOT_BUDGET_SECONDS)
        + HEURISTIC_FALLBACK_ALLOWANCE_SECONDS

    must stay at or below `GUNICORN_TIMEOUT_SECONDS -
    GUNICORN_TIMEOUT_MARGIN_SECONDS`, so a value that clears the product
    bar but not the worker timeout cannot ship unnoticed.
    """
    upstream = (
        measured_upstream_seconds
        if isinstance(measured_upstream_seconds, Decimal)
        else Decimal(str(measured_upstream_seconds))
    )
    remainder = (
        RESPONSE_BAR_SECONDS
        - upstream
        - DEADLINE_OVERSHOOT_BUDGET_SECONDS
        - HEURISTIC_FALLBACK_ALLOWANCE_SECONDS
    )
    per_solve = remainder / ROUTE_ALTERNATIVES_FANOUT
    quantized = per_solve.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    if quantized <= 0:
        raise ValueError(
            f"no room for a positive per-solve DP deadline: measured upstream "
            f"{upstream}s leaves a remainder of {remainder}s after subtracting "
            f"DEADLINE_OVERSHOOT_BUDGET_SECONDS ({DEADLINE_OVERSHOOT_BUDGET_SECONDS}s) "
            f"and HEURISTIC_FALLBACK_ALLOWANCE_SECONDS "
            f"({HEURISTIC_FALLBACK_ALLOWANCE_SECONDS}s) from RESPONSE_BAR_SECONDS "
            f"({RESPONSE_BAR_SECONDS}s); quantized result was {quantized}s"
        )
    return quantized


# ---------------------------------------------------------------------------
# D-17 -- LIVE_PROBE_MAX_CELLS + select_live_probe_cells()
#
# Pinned 2026-08-04 (plan 18.1-01), BEFORE the offline replay table
# (`measure_dispatch_grid`, plan 07) this selection rule reads from
# exists.
#
# LIVE_PROBE_MAX_CELLS derived against
# `test_live_latency_probe.LIVE_PROBE_MAX_REQUESTS` (20) using that
# module's own request-count formula: cells * repeats * ladder rungs + 1.
# Three cells yields 3 * 2 * 3 + 1 = 19, inside the existing 20-request
# budget -- no probe-budget constant needs to move.
LIVE_PROBE_MAX_CELLS = 3

# The two `dp.DISPATCH_RETENTION_FLOOR` cells, named in this exact order
# (matching `dp.py`'s own docstring and
# `test_solver_dispatch.DispatchRetentionFloorGuardTests._RETENTION_SET`)
# -- both mandatory members of every live-probe selection, never
# hand-picked.
_LIVE_PROBE_RETENTION_FLOOR_CELLS = (
    ("sacramento_ca-salt_lake_city_ut", Decimal(500)),
    ("dallas_tx-seattle_wa", Decimal(1050)),
)


def _newly_admitted_at_rung(rows, rung):
    """Cells whose estimate falls in the band `adopt_budget_rung` would
    have newly admitted at `rung` -- empty for the baseline rung itself,
    since the baseline is the existing policy, never "newly" anything."""
    if rung == DP_TRANSITION_BUDGET_LADDER[0]:
        return []
    rung_index = DP_TRANSITION_BUDGET_LADDER.index(rung)
    lower_bound = DP_TRANSITION_BUDGET_LADDER[rung_index - 1]
    return [
        row
        for row in rows
        if row["estimate"] > lower_bound and (rung is None or row["estimate"] <= rung)
    ]


def select_live_probe_cells(rows, adopted_rung):
    """Select the live-probe cell set (D-17) by rule, never by hand.

    `rows`: iterable of dicts, each carrying at least:
      - "slug": corridor slug (str)
      - "tank_range_mi": Decimal
      - "estimate": int
      - "offline_untimed_solve_seconds": Decimal, the worst-of-N raw
        `dp.solve_fixed_charge(..., deadline=None)` wall-clock time
      - "is_demo_cell": bool, True for the two demo-chip cells (D-13),
        False for the 24 corridor-grid cells
    `adopted_rung`: the value `adopt_budget_rung` returned.

    Returns an ordered tuple of at most `LIVE_PROBE_MAX_CELLS`
    `(slug, tank_range_mi)` pairs:
      1. Both `_LIVE_PROBE_RETENTION_FLOOR_CELLS`, in the order given.
      2. The single cell newly admitted at `adopted_rung` with the
         largest `offline_untimed_solve_seconds`; ties broken by slug
         then tank range, ascending. If the rule-selected cell is already
         in the set, the next-largest is taken instead. If no cell is
         newly admitted at `adopted_rung` (e.g. the NOT RAISED outcome),
         the demo cell (`is_demo_cell=True`) with the largest
         `offline_untimed_solve_seconds` is substituted instead.

    The set is whatever this function returns. No cell is ever added or
    removed by hand after seeing a result.
    """
    rows = list(rows)
    selected = list(_LIVE_PROBE_RETENTION_FLOOR_CELLS)
    seen = set(selected)

    pool = [
        row
        for row in _newly_admitted_at_rung(rows, adopted_rung)
        if (row["slug"], row["tank_range_mi"]) not in seen
    ]
    if not pool:
        pool = [
            row
            for row in rows
            if row.get("is_demo_cell") and (row["slug"], row["tank_range_mi"]) not in seen
        ]

    ordered_pool = sorted(
        pool,
        key=lambda row: (-row["offline_untimed_solve_seconds"], row["slug"], row["tank_range_mi"]),
    )
    for row in ordered_pool:
        key = (row["slug"], row["tank_range_mi"])
        if key in seen:
            continue
        selected.append(key)
        break

    return tuple(selected[:LIVE_PROBE_MAX_CELLS])


# ---------------------------------------------------------------------------
# D-18 -- RECOVERY_MIN_CELLS + recovery_verdict() + measurement_floor_violations()
#
# Pinned 2026-08-04 (plan 18.1-01), BEFORE the live spot-check (plan 09)
# this verdict rule and measurement floor are applied against exists.
RECOVERY_MIN_CELLS = 1


def recovery_verdict(live_rows):
    """Apply D-18's verdict rule to the live spot-check results.

    `live_rows`: iterable of dicts, each carrying at least:
      - "slug": corridor slug (str)
      - "tank_range_mi": Decimal
      - "shipped_policy_strategy": str, the strategy this cell dispatched
        to under the PRE-PHASE shipped policy ("penalty_aware_heuristic"
        or "exact_dp")
      - "worst_of_repeats_solver_strategy": str, the worst-of-repeats live
        `solver_strategy`
      - "worst_of_repeats_total_response_seconds": Decimal, the
        worst-of-repeats total live response time

    Returns the string "RECOVERED" when at least `RECOVERY_MIN_CELLS`
    rows satisfy all three: the cell dispatched to
    `penalty_aware_heuristic` under the pre-phase shipped policy; its
    worst-of-repeats live `solver_strategy` is `exact_dp`; and its
    worst-of-repeats total response time is at or below
    `RESPONSE_BAR_SECONDS`. Returns "NOT RECOVERED" otherwise, including
    on empty input.
    """
    recovered_count = sum(
        1
        for row in live_rows
        if row["shipped_policy_strategy"] == "penalty_aware_heuristic"
        and row["worst_of_repeats_solver_strategy"] == "exact_dp"
        and row["worst_of_repeats_total_response_seconds"] <= RESPONSE_BAR_SECONDS
    )
    return "RECOVERED" if recovered_count >= RECOVERY_MIN_CELLS else "NOT RECOVERED"


def measurement_floor_violations(live_rows):
    """Apply D-18's measurement floor to the live spot-check results.

    `live_rows`: same shape `recovery_verdict` takes, plus:
      - "confirmed_warm": bool, True if taken against a confirmed-warm
        instance (18-11's wake-then-measure protocol)
      - "repeats": int, the number of repeats this row's figures are
        drawn from
      - "all_repeats_genuine_cache_miss": bool, True only if every repeat
        was a genuine cache miss
      - "reported_figure_kind": str, one of "worst", "best", "mean" --
        which repeat statistic the reported figures use

    Returns a list of human-readable violation strings, empty when the
    floor is clear. The floor: every row must be taken against a
    confirmed-warm instance; every row must carry at least
    `LIVE_PROBE_REPEATS` repeats; every repeat must be a genuine cache
    miss; and every reported figure must be the worst of the repeats,
    never a best or a mean.

    A NOT RECOVERED verdict may not be declared while this function
    returns any violation -- free-tier cold-boot noise is the single most
    likely source of a wrong negative, and this is what blocks it.
    """
    violations = []
    for row in live_rows:
        label = f"{row.get('slug', '<unknown>')}@{row.get('tank_range_mi', '<unknown>')}mi"
        if not row.get("confirmed_warm"):
            violations.append(f"{label}: not measured against a confirmed-warm instance")
        repeats = row.get("repeats", 0)
        if repeats < LIVE_PROBE_REPEATS:
            violations.append(
                f"{label}: only {repeats} repeat(s), floor requires at least "
                f"{LIVE_PROBE_REPEATS}"
            )
        if not row.get("all_repeats_genuine_cache_miss", False):
            violations.append(f"{label}: at least one repeat was not a genuine cache miss")
        if row.get("reported_figure_kind") != "worst":
            violations.append(
                f"{label}: reported figure is {row.get('reported_figure_kind')!r}, "
                "floor requires worst-of-repeats"
            )
    return violations


# ---------------------------------------------------------------------------
# Phase 25 D-10 -- DEMOTED_CELL_COUNT, PLATEAU_TRIGGER_MIN_RECOVERED_CELLS,
# plateau_verdict()
#
# Pinned 2026-08-17 (plan 25-01), BEFORE any Phase 25 measurement exists --
# before the strengthened domination rule in routing/services/prune.py is
# even written, and before a single cell's post-prune estimate is computed.
# This is a DIFFERENT decision family from the bare `D-NN` labels above,
# which all belong to phase 18.1 -- every citation in this section and the
# three that follow it is qualified `Phase 25 D-NN` so the two label
# families never merge (25-RESEARCH.md Correction #6).
#
# DEMOTED_CELL_COUNT -- the count of ADMISSION_MANIFEST
# (test_solver_dispatch.py:568) cells currently on penalty_aware_heuristic
# (False in the manifest). 14, counted directly off the manifest as
# committed 2026-08-08 (plan 22-14). The pre-Phase-22 figure of 11 is NOT
# the denominator here -- Phase 22's Overture gap-fill import flipped three
# additional cells onto the heuristic side
# (san_diego_ca-jacksonville_fl@1050mi, sacramento_ca-salt_lake_city_ut@500mi,
# demo_la_ca-denver_co-chicago_il@1050mi -- 25-RESEARCH.md SS C1).
DEMOTED_CELL_COUNT = 14

# PLATEAU_TRIGGER_MIN_RECOVERED_CELLS -- Phase 25 D-10. The plateau is
# declared when FEWER than this many of the 14 currently-demoted cells fall
# at or under dp.DP_TRANSITION_BUDGET after the strengthened prune. Grounded
# on 25-CONTEXT.md's own pre-measurement estimate-cut arithmetic (the
# <specifics> table -- estimate_transition_count scales roughly cubically
# with candidate density, so halving candidates divides the estimate by
# about 8):
#   dallas_tx-seattle_wa@500        needs a 1.24x estimate cut  (~8% of candidates)
#   dallas_tx-seattle_wa@1050       needs a 2.4x estimate cut   (~26%)
#   atlanta_ga-denver_co@500        needs a 3.0x estimate cut   (~31%)
#   miami_fl-boston_ma@500          needs a 3.7x estimate cut   (~35%)
# A rule achieving a plausible 20-40% candidate cut therefore lands somewhere
# in the 2-to-5 recovered-cell range; 3 sits inside that range rather than at
# either end.
#
# This is a DIFFERENT question from 18.1-01's RECOVERY_MIN_CELLS = 1 (this
# module, above): that floor asks whether time-boxed dispatch recovered ANY
# cell at all against a single live spot-check.
# PLATEAU_TRIGGER_MIN_RECOVERED_CELLS asks whether the strengthened prune
# went far enough, across the WHOLE 14-cell demoted set measured offline, to
# make the deadline-based band fallback (Phase 25 D-11) unnecessary.
PLATEAU_TRIGGER_MIN_RECOVERED_CELLS = 3


def plateau_verdict(after_rows):
    """Apply Phase 25 D-10's plateau verdict to the strengthened prune's
    26-cell sweep.

    `after_rows`: iterable of dicts, each carrying at least:
      - "slug": corridor slug (str)
      - "tank_range_mi": Decimal
      - "was_demoted": bool -- this cell's PRE-Phase-25 admission state
        (True if it was on penalty_aware_heuristic before the strengthened
        rule ran)
      - "estimate": int -- the strengthened rule's post-prune estimate

    Counts rows where `was_demoted` is True and
    `estimate <= dp.DP_TRANSITION_BUDGET`. Returns the string "PLATEAU" when
    that count is strictly less than PLATEAU_TRIGGER_MIN_RECOVERED_CELLS,
    otherwise "NO_PLATEAU". Raises ValueError when the number of
    `was_demoted` rows is not exactly DEMOTED_CELL_COUNT -- a partial sweep
    must never be able to manufacture a plateau verdict.

    "PLATEAU" means the deadline-based band fallback (Phase 25 D-11) is the
    path forward. This is a pre-declared verdict, pinned before a single
    post-prune estimate exists -- not a judgement call at the moment of
    truth.
    """
    after_rows = list(after_rows)
    demoted_rows = [row for row in after_rows if row["was_demoted"]]
    if len(demoted_rows) != DEMOTED_CELL_COUNT:
        raise ValueError(
            f"plateau_verdict requires exactly {DEMOTED_CELL_COUNT} "
            f"was_demoted=True rows (the full pre-Phase-25 demoted set), "
            f"got {len(demoted_rows)} -- a partial sweep must never be able "
            "to manufacture a plateau verdict"
        )
    recovered_count = sum(
        1 for row in demoted_rows if row["estimate"] <= dp.DP_TRANSITION_BUDGET
    )
    return (
        "PLATEAU"
        if recovered_count < PLATEAU_TRIGGER_MIN_RECOVERED_CELLS
        else "NO_PLATEAU"
    )


# ---------------------------------------------------------------------------
# Phase 25 D-11 -- BAND_EXACT_DP_DIRECT, BAND_EXACT_DP_ATTEMPT,
# BAND_HEURISTIC_NO_ATTEMPT, band_dispatch_decision()
#
# Pinned 2026-08-17 (plan 25-01), BEFORE any Phase 25 measurement exists.
# D-11 widens the dispatch gate from a single threshold to a band: cells
# with estimate in `(dp.DP_TRANSITION_BUDGET, BAND_CEILING]` are ATTEMPTED
# under the DP_DEADLINE_SECONDS deadline rather than dispatched straight to
# the heuristic -- the estimate proxy stops deciding the answer and starts
# deciding only whether to try.
BAND_EXACT_DP_DIRECT = "exact_dp_direct"
BAND_EXACT_DP_ATTEMPT = "exact_dp_banded_attempt"
BAND_HEURISTIC_NO_ATTEMPT = "heuristic_no_attempt"


def band_dispatch_decision(estimate, *, band_ceiling):
    """Apply Phase 25 D-11's three-valued band policy to one cell's
    estimate.

    A hard ceiling survives alongside the wall-clock deadline because the
    deadline caps TIME, not MEMORY: nothing bounds what a multi-million-
    transition DP allocates inside the deadline on a 512 MB Render
    instance, and ROUTE_ALTERNATIVES_FANOUT = 3 (this module, above) means
    up to three attempts can be paid for within a single request.

    Returns BAND_EXACT_DP_DIRECT when `estimate <= dp.DP_TRANSITION_BUDGET`;
    BAND_EXACT_DP_ATTEMPT when
    `dp.DP_TRANSITION_BUDGET < estimate <= band_ceiling`; otherwise
    BAND_HEURISTIC_NO_ATTEMPT. Raises ValueError when
    `band_ceiling < dp.DP_TRANSITION_BUDGET` -- a ceiling below the budget
    is not a band at all.
    """
    if band_ceiling < dp.DP_TRANSITION_BUDGET:
        raise ValueError(
            f"band_ceiling ({band_ceiling}) must be at least "
            f"dp.DP_TRANSITION_BUDGET ({dp.DP_TRANSITION_BUDGET}) -- a "
            "ceiling below the budget is not a band at all"
        )
    if estimate <= dp.DP_TRANSITION_BUDGET:
        return BAND_EXACT_DP_DIRECT
    if estimate <= band_ceiling:
        return BAND_EXACT_DP_ATTEMPT
    return BAND_HEURISTIC_NO_ATTEMPT


# ---------------------------------------------------------------------------
# Phase 25 D-12 -- FREE_TIER_MEMORY_MB, RENDER_WEB_CONCURRENCY,
# BAND_CEILING_RSS_FRACTION, derive_band_ceiling()
#
# Pinned 2026-08-17 (plan 25-01), BEFORE a single peak-RSS figure exists.
# Phase 25 pins the derivation rule and the fraction; Phase 26 takes the
# measurement.
#
# FREE_TIER_MEMORY_MB / RENDER_WEB_CONCURRENCY -- render.yaml:107-110
# (WEB_CONCURRENCY = 2 gunicorn workers on Render free's 512 MB instance),
# with that file's own comment stating both are measurement-backed against
# Render free's 512 MB limit.
FREE_TIER_MEMORY_MB = 512
RENDER_WEB_CONCURRENCY = 2

# BAND_CEILING_RSS_FRACTION -- derived, not asserted: RENDER_WEB_CONCURRENCY
# = 2 gunicorn workers share the 512 MB instance, so one worker's entire
# share is exactly half the ceiling, and a band whose worst-case memory a
# SINGLE worker cannot fit inside its own share is not shippable regardless
# of what the other worker is doing at the time. This fraction is a
# pre-declared bound, pinned before a single peak-RSS figure existed.
BAND_CEILING_RSS_FRACTION = Decimal("0.50")


def derive_band_ceiling(rss_rows):
    """Derive BAND_CEILING (Phase 25 D-12) from a peak-RSS ladder measured
    on deployed hardware.

    Phase 25 pins this rule; Phase 26 takes the measurement. The RSS figure
    is expected out-of-band via resource.getrusage, surfaced through
    ServerTiming.record() -- the same shape dp_deadline_breach already
    uses -- never by wrapping a .stage() block, since RSS is not a
    duration.

    `rss_rows`: iterable of dicts, each carrying at least:
      - "rung": int, a member of DP_TRANSITION_BUDGET_LADDER (may be None,
        the ladder's no-gate sentinel)
      - "peak_rss_mb": Decimal, the measured peak resident-set-size delta
        during one genuine DP attempt at that rung

    Returns the HIGHEST rung whose
    `peak_rss_mb * ROUTE_ALTERNATIVES_FANOUT <= FREE_TIER_MEMORY_MB *
    BAND_CEILING_RSS_FRACTION`, falling back to dp.DP_TRANSITION_BUDGET when
    no rung qualifies. Raises ValueError on a `None` rung -- the ladder's
    no-gate sentinel cannot itself be a memory-bounded ceiling -- and on a
    `rung` that is not otherwise a member of DP_TRANSITION_BUDGET_LADDER.
    """
    budget_limit = FREE_TIER_MEMORY_MB * BAND_CEILING_RSS_FRACTION
    qualifying_rungs = []
    for row in rss_rows:
        rung = row["rung"]
        if rung is None:
            raise ValueError(
                "rung=None (DP_TRANSITION_BUDGET_LADDER's no-gate sentinel) "
                "cannot be a memory-bounded ceiling"
            )
        if rung not in DP_TRANSITION_BUDGET_LADDER:
            raise ValueError(
                f"rung {rung!r} is not a member of DP_TRANSITION_BUDGET_LADDER"
            )
        if row["peak_rss_mb"] * ROUTE_ALTERNATIVES_FANOUT <= budget_limit:
            qualifying_rungs.append(rung)
    if not qualifying_rungs:
        return dp.DP_TRANSITION_BUDGET
    return max(qualifying_rungs)


# ---------------------------------------------------------------------------
# Phase 25 D-16 / D-17 -- DEMOTION_GUARD_PROBE_REPEATS,
# demotion_guard_outcome_bound(), demotion_guard_rewrite_qualifies()
#
# Pinned 2026-08-17 (plan 25-01), BEFORE any live probe against
# tankwise.onrender.com is issued for this phase.
#
# DEMOTION_GUARD_PROBE_REPEATS -- Phase 25 D-16. The guard this bar could
# license rewriting (test_solver_dispatch.DispatchDemotionGuardTests) rests
# on dp.py:814-819's record of dallas_tx-seattle_wa@500mi (API-default
# vehicle) reproducing HTTP 500 5/5 at 30.5-35.7s live, pre-hotfix. The
# rewrite bar is therefore exactly as many clean live repeats as there were
# failures -- evidence of EQUAL weight, not lighter. Offline evidence does
# not qualify at any repeat count: this project has measured the
# workstation-to-live factor at 4.31x-105.98x (18-11's own capture,
# test_live_latency_probe.py) and explicitly recorded that the factor is
# NOT a constant.
DEMOTION_GUARD_PROBE_REPEATS = 5


def demotion_guard_outcome_bound(row):
    """Apply Phase 25 D-17's arm-agnostic outcome bound to one live-probe
    row.

    This asserts the property the demotion guard actually exists to
    protect -- the live HTTP 500 -- rather than the strategy-label proxy it
    currently asserts. It reads no strategy/arm/solver_strategy key at
    all, so a row carrying one is accepted and ignored; this is the point,
    not an oversight -- it cannot flake on deadline straddle, which is
    exactly what broke the 130,000 rung's own measurement (breached 2/3,
    completed 1/3 on the same cell, same hardware).

    `row`: dict carrying at least "status_code" (int) and
    "solver_stage_seconds" (Decimal).

    Returns True only when `row["status_code"] == 200` and
    `row["solver_stage_seconds"] <= RESPONSE_BAR_SECONDS`.
    """
    return row["status_code"] == 200 and row["solver_stage_seconds"] <= RESPONSE_BAR_SECONDS


def demotion_guard_rewrite_qualifies(live_rows):
    """Apply Phase 25 D-16's full evidence bar for rewriting
    DispatchDemotionGuardTests.

    This is the ENTIRE evidence bar -- pinned in Phase 25, before any probe
    ran. Per Phase 25 D-15, Phase 25 itself changes no guard whatever the
    26-cell sweep finds; the reconciliation belongs to Phase 26, on
    deployed evidence.

    `live_rows`: iterable of dicts, each carrying at least "source" (str),
    "host" (str), "cache_miss" (bool), "status_code" (int) and
    "solver_stage_seconds" (Decimal).

    Returns True only when ALL of the following hold:
      - there are exactly DEMOTION_GUARD_PROBE_REPEATS rows
      - every row has row["source"] == "live"
      - every row has row["host"] == "tankwise.onrender.com"
      - every row has row["cache_miss"] is True
      - demotion_guard_outcome_bound(row) is True for every row
    Returns False otherwise.
    """
    live_rows = list(live_rows)
    if len(live_rows) != DEMOTION_GUARD_PROBE_REPEATS:
        return False
    return all(
        row["source"] == "live"
        and row["host"] == "tankwise.onrender.com"
        and row["cache_miss"] is True
        and demotion_guard_outcome_bound(row)
        for row in live_rows
    )


# ---------------------------------------------------------------------------
# Permanent anti-vacuity guard (Task 2)
# ---------------------------------------------------------------------------


class PinnedRecoveryRuleGuardTests(SimpleTestCase):
    """Proves none of the six rules pinned above is degenerate.

    A rule that is CONSTANT is indistinguishable from no rule at all --
    the milestone's own transferable lesson (the false Domination Theorem
    disproof, Phase 17) is that a no-op transformation, `prune(x) -> x`,
    passes every soundness property written only in the "does not make
    things worse" direction. Each guard below therefore checks BOTH
    directions: that the rule can produce its baseline/negative outcome
    AND that it can produce its raised/positive outcome, on hand-built
    synthetic rows -- no DB, no fixtures, no network.

    Grouped into four bands, mirroring
    `test_dispatch_predictor.DispatchPredictorGuardTests` and
    `test_solver_dispatch.DispatchRetentionFloorGuardTests`' own
    non-vacuous-by-construction style:

      1. Well-formedness -- the ladders themselves are shaped correctly.
      2. Non-constancy -- each adoption/verdict/floor function can swing
         both ways on synthetic input.
      3. Derivation -- `derive_dp_deadline_seconds` behaves as a genuine
         function of its one argument, not a constant.
      4. Cross-module coupling -- the two constants this phase's real
         code introduces (`dp.DP_TRANSITION_BUDGET`,
         `dp._DEADLINE_CHECK_STRIDE`) must be members of their
         corresponding ladder here, so an off-ladder value cannot ship.

    [Amended 2026-08-17, Phase 25] Superseded, not accurate -- the class
    now also guards five more rules pinned in Phase 25 (D-10, D-11, D-12,
    D-16, D-17): PLATEAU_TRIGGER_MIN_RECOVERED_CELLS/`plateau_verdict()`,
    `band_dispatch_decision()`, BAND_CEILING_RSS_FRACTION/
    `derive_band_ceiling()`, DEMOTION_GUARD_PROBE_REPEATS/
    `demotion_guard_rewrite_qualifies()`, and
    `demotion_guard_outcome_bound()` -- eleven rules in total, none of
    which is degenerate. The sentence above is preserved verbatim, per
    this project's amend-in-place convention.
    """

    # -- 1. Well-formedness --------------------------------------------

    def test_dp_transition_budget_ladder_is_well_formed(self):
        non_none = [rung for rung in DP_TRANSITION_BUDGET_LADDER if rung is not None]
        for lower, higher in zip(non_none, non_none[1:]):
            self.assertLess(
                lower,
                higher,
                "DP_TRANSITION_BUDGET_LADDER must be strictly increasing "
                "over its non-None members",
            )
        self.assertEqual(
            DP_TRANSITION_BUDGET_LADDER[0],
            dp.DP_TRANSITION_BUDGET,
            "the ladder's baseline rung must equal the currently shipped "
            "dp.DP_TRANSITION_BUDGET, so the baseline is not a fiction",
        )
        self.assertIsNone(DP_TRANSITION_BUDGET_LADDER[-1])
        self.assertEqual(
            DP_TRANSITION_BUDGET_LADDER.count(None),
            1,
            "None (the no-gate rung) must appear exactly once, as the "
            "last member",
        )

    def test_deadline_check_stride_ladder_is_well_formed(self):
        for value in DEADLINE_CHECK_STRIDE_LADDER:
            self.assertIsInstance(value, int)
            self.assertGreater(value, 0)
        for lower, higher in zip(DEADLINE_CHECK_STRIDE_LADDER, DEADLINE_CHECK_STRIDE_LADDER[1:]):
            self.assertLess(
                lower,
                higher,
                "DEADLINE_CHECK_STRIDE_LADDER must be strictly increasing",
            )

    def test_live_probe_max_cells_stays_within_the_existing_probe_request_budget(self):
        implied_requests = (
            LIVE_PROBE_MAX_CELLS
            * test_live_latency_probe.LIVE_PROBE_REPEATS
            * len(test_live_latency_probe.LIVE_PROBE_CACHE_BUST_LADDER)
            + 1
        )
        self.assertLessEqual(
            implied_requests,
            test_live_latency_probe.LIVE_PROBE_MAX_REQUESTS,
            "LIVE_PROBE_MAX_CELLS must stay inside the existing live-probe "
            "request budget, not just this module's own arithmetic",
        )

    def test_phase_25_plateau_trigger_is_well_formed(self):
        self.assertIsInstance(PLATEAU_TRIGGER_MIN_RECOVERED_CELLS, int)
        self.assertGreater(PLATEAU_TRIGGER_MIN_RECOVERED_CELLS, 0)
        self.assertLess(PLATEAU_TRIGGER_MIN_RECOVERED_CELLS, DEMOTED_CELL_COUNT)

    def test_phase_25_band_ceiling_rss_fraction_is_well_formed(self):
        self.assertIsInstance(BAND_CEILING_RSS_FRACTION, Decimal)
        self.assertGreater(BAND_CEILING_RSS_FRACTION, Decimal(0))
        self.assertLess(BAND_CEILING_RSS_FRACTION, Decimal(1))

    def test_phase_25_demotion_guard_probe_repeats_is_well_formed(self):
        self.assertIsInstance(DEMOTION_GUARD_PROBE_REPEATS, int)
        self.assertGreaterEqual(DEMOTION_GUARD_PROBE_REPEATS, 1)

    def test_phase_25_band_labels_are_distinct_non_empty_strings(self):
        labels = (BAND_EXACT_DP_DIRECT, BAND_EXACT_DP_ATTEMPT, BAND_HEURISTIC_NO_ATTEMPT)
        for label in labels:
            self.assertIsInstance(label, str)
            self.assertTrue(label)
        self.assertEqual(len(set(labels)), 3, "the three band labels must be distinct")

    # -- 2. Non-constancy (the anti-vacuity half) -----------------------

    def test_adopt_budget_rung_is_not_constant_in_either_direction(self):
        # Every higher rung's newly-admitted cells exceed the response
        # bar (all breached, all over RESPONSE_BAR_SECONDS) -> the walk
        # must stop at the very first rung and fall back to the baseline.
        over_bar_rows = [
            {
                "slug": "over-bar-1",
                "tank_range_mi": Decimal(500),
                "estimate": 60_000,
                "worst_timed_response_seconds": Decimal(30),
                "breached": True,
            },
            {
                "slug": "over-bar-2",
                "tank_range_mi": Decimal(500),
                "estimate": 125_000,
                "worst_timed_response_seconds": Decimal(30),
                "breached": True,
            },
        ]
        self.assertEqual(
            adopt_budget_rung(over_bar_rows),
            DP_TRANSITION_BUDGET_LADDER[0],
            "a rung whose own newly-admitted cells all exceed the bar "
            "must not be adopted",
        )

        # One rung's newly-admitted cell sits comfortably inside the bar
        # and did not breach -> that rung must be adopted, strictly above
        # the baseline.
        comfortable_rows = [
            {
                "slug": "comfortable-1",
                "tank_range_mi": Decimal(500),
                "estimate": 60_000,
                "worst_timed_response_seconds": Decimal(2),
                "breached": False,
            },
        ]
        adopted = adopt_budget_rung(comfortable_rows)
        self.assertGreater(
            adopted,
            DP_TRANSITION_BUDGET_LADDER[0],
            "a rung whose own newly-admitted cell sits comfortably inside "
            "the bar and completed must be adopted above the baseline",
        )

    def test_adopt_stride_rung_is_not_constant_in_either_direction(self):
        every_rung_overshoots = [
            {"stride": stride, "worst_overshoot_seconds": Decimal("10")}
            for stride in DEADLINE_CHECK_STRIDE_LADDER
        ]
        self.assertEqual(
            adopt_stride_rung(every_rung_overshoots),
            DEADLINE_CHECK_STRIDE_LADDER[0],
            "when every rung overshoots the budget, the smallest "
            "(finest-grained) rung must be returned as the floor",
        )

        no_rung_overshoots = [
            {"stride": stride, "worst_overshoot_seconds": Decimal("0.05")}
            for stride in DEADLINE_CHECK_STRIDE_LADDER
        ]
        self.assertEqual(
            adopt_stride_rung(no_rung_overshoots),
            DEADLINE_CHECK_STRIDE_LADDER[-1],
            "when no rung overshoots the budget, the largest rung must "
            "be adopted",
        )

    def test_recovery_verdict_is_not_constant_in_either_direction(self):
        # Each case runs inside its own subTest so ALL three are
        # evaluated and reported independently -- a mutated
        # recovery_verdict that always returns "RECOVERED" must fail at
        # least two of these three, not just halt on the first.
        with self.subTest(case="empty_input"):
            self.assertEqual(recovery_verdict([]), "NOT RECOVERED")

        already_exact_rows = [
            {
                "slug": "sacramento_ca-salt_lake_city_ut",
                "tank_range_mi": Decimal(500),
                "shipped_policy_strategy": "exact_dp",
                "worst_of_repeats_solver_strategy": "exact_dp",
                "worst_of_repeats_total_response_seconds": Decimal("1"),
            },
        ]
        with self.subTest(case="nothing_needed_recovering"):
            self.assertEqual(
                recovery_verdict(already_exact_rows),
                "NOT RECOVERED",
                "nothing was recovered because nothing needed recovering -- "
                "a cell already on exact_dp under the shipped policy cannot "
                "count toward RECOVERED",
            )

        recovered_rows = [
            {
                "slug": "dallas_tx-seattle_wa",
                "tank_range_mi": Decimal(500),
                "shipped_policy_strategy": "penalty_aware_heuristic",
                "worst_of_repeats_solver_strategy": "exact_dp",
                "worst_of_repeats_total_response_seconds": Decimal(9),
            },
        ]
        with self.subTest(case="genuine_transition_recovers"):
            self.assertEqual(recovery_verdict(recovered_rows), "RECOVERED")

    def test_measurement_floor_violations_catches_each_violation_and_clears_a_compliant_row(
        self,
    ):
        compliant_row = {
            "slug": "dallas_tx-seattle_wa",
            "tank_range_mi": Decimal(500),
            "confirmed_warm": True,
            "repeats": LIVE_PROBE_REPEATS,
            "all_repeats_genuine_cache_miss": True,
            "reported_figure_kind": "worst",
        }
        self.assertEqual(measurement_floor_violations([compliant_row]), [])

        missing_confirmed_warm = dict(compliant_row, confirmed_warm=False)
        self.assertTrue(measurement_floor_violations([missing_confirmed_warm]))

        too_few_repeats = dict(compliant_row, repeats=LIVE_PROBE_REPEATS - 1)
        self.assertTrue(measurement_floor_violations([too_few_repeats]))

        not_a_genuine_cache_miss = dict(compliant_row, all_repeats_genuine_cache_miss=False)
        self.assertTrue(measurement_floor_violations([not_a_genuine_cache_miss]))

        not_worst_of_repeats = dict(compliant_row, reported_figure_kind="mean")
        self.assertTrue(measurement_floor_violations([not_worst_of_repeats]))

    def test_phase_25_plateau_verdict_is_not_constant_in_either_direction(self):
        def _demoted_rows(recovered_count):
            rows = []
            for i in range(DEMOTED_CELL_COUNT):
                estimate = 10_000 if i < recovered_count else 999_999
                rows.append(
                    {
                        "slug": f"cell-{i}",
                        "tank_range_mi": Decimal(500),
                        "was_demoted": True,
                        "estimate": estimate,
                    }
                )
            return rows

        self.assertEqual(
            plateau_verdict(_demoted_rows(2)),
            "PLATEAU",
            "fewer than PLATEAU_TRIGGER_MIN_RECOVERED_CELLS recovered "
            "cells must declare PLATEAU",
        )
        self.assertEqual(
            plateau_verdict(_demoted_rows(3)),
            "NO_PLATEAU",
            "at least PLATEAU_TRIGGER_MIN_RECOVERED_CELLS recovered cells "
            "must declare NO_PLATEAU",
        )

    def test_phase_25_band_dispatch_decision_reaches_all_three_labels(self):
        ceiling = 200_000
        self.assertEqual(
            band_dispatch_decision(dp.DP_TRANSITION_BUDGET - 1, band_ceiling=ceiling),
            BAND_EXACT_DP_DIRECT,
            "a constant-returning implementation fails at least two of "
            "these three cases",
        )
        self.assertEqual(
            band_dispatch_decision(dp.DP_TRANSITION_BUDGET + 1, band_ceiling=ceiling),
            BAND_EXACT_DP_ATTEMPT,
        )
        self.assertEqual(
            band_dispatch_decision(ceiling + 1, band_ceiling=ceiling),
            BAND_HEURISTIC_NO_ATTEMPT,
        )

    def test_phase_25_derive_band_ceiling_is_not_constant_in_either_direction(self):
        all_over_budget = [
            {"rung": rung, "peak_rss_mb": Decimal(1000)}
            for rung in DP_TRANSITION_BUDGET_LADDER
            if rung is not None
        ]
        self.assertEqual(
            derive_band_ceiling(all_over_budget),
            dp.DP_TRANSITION_BUDGET,
            "a ladder where every rung's own RSS exceeds the budget must "
            "fall back to the baseline",
        )

        one_comfortable_rung = [
            {"rung": dp.DP_TRANSITION_BUDGET, "peak_rss_mb": Decimal(1000)},
            {"rung": 70_000, "peak_rss_mb": Decimal(1)},
        ]
        self.assertGreater(
            derive_band_ceiling(one_comfortable_rung),
            dp.DP_TRANSITION_BUDGET,
            "a ladder with one comfortable higher rung must adopt "
            "strictly above the baseline",
        )

    def test_phase_25_demotion_guard_rewrite_qualifies_is_not_constant_in_either_direction(
        self,
    ):
        clean_row = {
            "source": "live",
            "host": "tankwise.onrender.com",
            "cache_miss": True,
            "status_code": 200,
            "solver_stage_seconds": Decimal(1),
        }
        clean_rows = [dict(clean_row) for _ in range(DEMOTION_GUARD_PROBE_REPEATS)]
        self.assertTrue(
            demotion_guard_rewrite_qualifies(clean_rows),
            "DEMOTION_GUARD_PROBE_REPEATS clean live rows must qualify",
        )

        slow_rows = [dict(clean_row) for _ in range(DEMOTION_GUARD_PROBE_REPEATS)]
        slow_rows[0] = dict(
            slow_rows[0], solver_stage_seconds=RESPONSE_BAR_SECONDS + 1
        )
        self.assertFalse(
            demotion_guard_rewrite_qualifies(slow_rows),
            "one row over RESPONSE_BAR_SECONDS must not qualify",
        )

    def test_phase_25_demotion_guard_outcome_bound_reads_both_axes(self):
        fast_ok = {"status_code": 200, "solver_stage_seconds": Decimal(1)}
        self.assertTrue(demotion_guard_outcome_bound(fast_ok))

        failed = {"status_code": 500, "solver_stage_seconds": Decimal(1)}
        self.assertFalse(
            demotion_guard_outcome_bound(failed),
            "a 500 must fail regardless of how fast it was",
        )

        slow_but_ok_status = {
            "status_code": 200,
            "solver_stage_seconds": RESPONSE_BAR_SECONDS + 1,
        }
        self.assertFalse(
            demotion_guard_outcome_bound(slow_but_ok_status),
            "a 200 that breaches RESPONSE_BAR_SECONDS must fail -- "
            "neither input axis may be ignored",
        )

    # -- 3. Derivation ----------------------------------------------------

    def test_derive_dp_deadline_seconds_is_strictly_decreasing_positive_and_bounded(self):
        smaller_upstream = derive_dp_deadline_seconds(Decimal("1"))
        larger_upstream = derive_dp_deadline_seconds(Decimal("2"))
        self.assertGreater(
            smaller_upstream,
            larger_upstream,
            "derive_dp_deadline_seconds must be strictly decreasing in "
            "its upstream argument",
        )

        # The one measured upstream figure on record: 18-11's own
        # full stage-breakdown capture, corridor + route + eia + cache +
        # index ~= 5.44s (routing.tests.test_live_latency_probe's own
        # anomaly-reproduction docstring).
        recorded_upstream = Decimal("5.44")
        result = derive_dp_deadline_seconds(recorded_upstream)
        self.assertGreater(result, Decimal(0))
        self.assertIsInstance(result, Decimal)

        # D-09's secondary check, asserted here so a value that clears
        # the product bar but not the worker timeout cannot pass
        # silently.
        worst_case_total = (
            recorded_upstream
            + ROUTE_ALTERNATIVES_FANOUT * (result + DEADLINE_OVERSHOOT_BUDGET_SECONDS)
            + HEURISTIC_FALLBACK_ALLOWANCE_SECONDS
        )
        self.assertLessEqual(
            worst_case_total,
            GUNICORN_TIMEOUT_SECONDS - GUNICORN_TIMEOUT_MARGIN_SECONDS,
            "the derived deadline must also clear the worker timeout "
            "with margin, not merely the product response bar",
        )

        with self.assertRaises(ValueError):
            derive_dp_deadline_seconds(Decimal("20"))

    def test_phase_25_derive_band_ceiling_is_monotone_and_validates_rung(self):
        lower_rss_rows = [{"rung": 70_000, "peak_rss_mb": Decimal(1)}]
        higher_rss_rows = [{"rung": 70_000, "peak_rss_mb": Decimal(200)}]
        self.assertGreaterEqual(
            derive_band_ceiling(lower_rss_rows),
            derive_band_ceiling(higher_rss_rows),
            "lowering every row's peak_rss_mb must never lower the "
            "returned ceiling",
        )

        with self.assertRaises(ValueError):
            derive_band_ceiling([{"rung": 65_000, "peak_rss_mb": Decimal(1)}])

        with self.assertRaises(ValueError):
            derive_band_ceiling([{"rung": None, "peak_rss_mb": Decimal(1)}])

    def test_phase_25_demotion_guard_outcome_bound_is_arm_blind(self):
        exact_dp_row = {
            "status_code": 200,
            "solver_stage_seconds": Decimal(1),
            "solver_strategy": "exact_dp",
        }
        heuristic_row = dict(exact_dp_row, solver_strategy="penalty_aware_heuristic")
        self.assertEqual(
            demotion_guard_outcome_bound(exact_dp_row),
            demotion_guard_outcome_bound(heuristic_row),
            "the arm/strategy field must never change the outcome -- "
            "this is what makes Phase 25 D-17's arm-agnosticism checkable "
            "rather than claimed",
        )

    # -- 4. Cross-module coupling ------------------------------------------

    def test_dp_transition_budget_is_a_member_of_the_ladder(self):
        self.assertIn(
            dp.DP_TRANSITION_BUDGET,
            DP_TRANSITION_BUDGET_LADDER,
            "an off-ladder dp.DP_TRANSITION_BUDGET value must not be "
            "shippable unnoticed",
        )

    @unittest.skipUnless(
        hasattr(dp, "_DEADLINE_CHECK_STRIDE"),
        "dp._DEADLINE_CHECK_STRIDE does not exist yet -- plan 04 introduces it; "
        "this guard activates automatically the moment it lands",
    )
    def test_deadline_check_stride_is_a_member_of_the_ladder(self):
        self.assertIn(
            dp._DEADLINE_CHECK_STRIDE,
            DEADLINE_CHECK_STRIDE_LADDER,
            "an off-ladder dp._DEADLINE_CHECK_STRIDE value must not be "
            "shippable unnoticed",
        )

    def test_phase_25_derive_band_ceiling_no_qualifying_rung_falls_back_to_transition_budget(
        self,
    ):
        all_over_budget = [
            {"rung": rung, "peak_rss_mb": Decimal(1000)}
            for rung in DP_TRANSITION_BUDGET_LADDER
            if rung is not None
        ]
        self.assertEqual(
            derive_band_ceiling(all_over_budget),
            dp.DP_TRANSITION_BUDGET,
            "the no-qualifying-rung fallback must equal "
            "dp.DP_TRANSITION_BUDGET exactly",
        )

    def test_phase_25_derive_band_ceiling_accepts_every_non_none_ladder_rung(self):
        for rung in DP_TRANSITION_BUDGET_LADDER:
            if rung is None:
                continue
            derive_band_ceiling([{"rung": rung, "peak_rss_mb": Decimal(1)}])

    def test_phase_25_demoted_cell_count_matches_admission_manifest(self):
        from routing.tests.test_solver_dispatch import ADMISSION_MANIFEST

        false_count = sum(
            1 for admitted in ADMISSION_MANIFEST.values() if admitted is False
        )
        self.assertEqual(
            DEMOTED_CELL_COUNT,
            false_count,
            "DEMOTED_CELL_COUNT must track ADMISSION_MANIFEST's own False "
            "count, imported live, so a manifest re-pin in a later phase "
            "cannot leave this denominator silently stale",
        )

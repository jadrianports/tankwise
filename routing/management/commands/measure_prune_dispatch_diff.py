"""Measure the plan-quality effect of Phase 25's strengthened domination
prune rule across the full 26-cell `ADMISSION_MANIFEST` grid -- PRUN-04's
before/after evidence table: the twelve committed corridors at both pinned
tank ranges, plus the two demo chips, measured under the current rule
(before) and the strengthened, penalty-aware rule (after), both worlds at
the production `settings.TRUST_MARGIN_USD` and never at a zero margin
(ROADMAP criterion 2).

Neither existing command spans this set. `measure_prune_reduction` walks
twelve corridors x two tank ranges x two price bases (48 cells, no demo
chips), and `CorridorFixtureTests` pins `CORRIDORS` at exactly twelve to
keep that manifest like-for-like with itself, so it cannot be widened to
cover the demo chips without breaking that guard. `measure_dispatch_grid`
spans the 26-cell set but measures only one rule (D-19: its own zero-margin
default is a separate, named finding for Phase 27, not touched here).

Composes `measure_dispatch_grid.Command._measure_cell` -- called on a real
instance of that command's class, never a bare module-level import of the
method name and never a null `self` -- for both worlds, and independently
rebuilds each cell's candidate list to attribute the strengthened rule's
removals via `measure_prune_reduction.classify_removals` (D-08). Nothing
here defines a new measurement concept; both seams already existed, this
only composes them.

D-14: the strengthened rule is reached ENTIRELY through the measurement
seam (`_measure_cell`'s `strengthened_prune` parameter) -- never by
`routing.services.solver.solve()` supplying `mpg=`/`penalty=` to
`prune_dominated_candidates`. `solve()` and `routing/services/prune.py`
are both unmodified by this command; `PruneInertnessGateTest`
(`routing/tests/test_boundaries.py`) is the CI-enforcing structural
backstop for that convention. Nothing this report shows is what
production currently serves.

Read-only and offline: replays the twelve committed corridor geometry
fixtures plus the two committed demo-chip fixtures through the existing
Directions-response parser, and rebuilds the station table from the
committed CSVs, exactly as `measure_dispatch_grid.py` already does. No
outbound network call of any kind, and works with no routing-provider
token set.

Must NOT run in CI -- most of the figures this command prints are
evidence, not a pass/fail gate, exactly like the sibling
`measure_dispatch_grid` command it composes. The ONE exception is ROADMAP
criterion 4's plan-identity check (`_check_plan_identity` below): on every
cell whose dispatch arm (`admitted_at_current_budget`) and timed strategy
are unchanged between worlds, `total_cost` and the chosen stops' `opis_id`
tuple must be identical -- a prune that removes a station the solver would
have chosen on an otherwise-unchanged cell is a correctness bug, not an
optimization, and that check raises `CommandError` rather than merely
reporting a number.

This plan (25-05) does not run the full 26-cell sweep -- plan 25-06 does,
against this committed, tested command rather than one still being edited
mid-measurement.
"""
import io
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.management.commands.measure_dispatch_grid import (
    Command as MeasureDispatchGridCommand,
    _build_grid,
)
from routing.management.commands.measure_prune_reduction import classify_removals
from routing.services import corridor, dp
from routing.services.prune import prune_dominated_candidates
from routing.services.station_csv_paths import reseed_all
from routing.tests.test_corridor_fixtures import factor_lookup_for_basis
from routing.tests.test_dispatch_recovery import DEMOTED_CELL_COUNT, plateau_verdict
from routing.tests.test_solver_dispatch import ADMISSION_MANIFEST, PENALTY

# Repeats exist in the sibling command for a worst-of-N timing measurement.
# This report's own criterion-4 gate and attribution table read no timing
# field, so a second pass buys nothing here. Fixed at 1, not exposed as a
# flag -- the same reasoning measure_refresh_diff.py's own
# _MEASURE_REPEATS applies.
_MEASURE_REPEATS = 1

# The six fields this report diffs, in table-column order -- identical set
# to measure_refresh_diff.py's own DIFFED_FIELDS, since both reports are
# rendering the same sibling command's CellResult.
DIFFED_FIELDS = (
    "raw_candidates",
    "kept",
    "estimate",
    "admitted_at_current_budget",
    "stops",
    "total_cost",
)

_FIELD_DISPLAY_NAMES = {
    "raw_candidates": "Raw candidates",
    "kept": "Kept",
    "estimate": "Estimate",
    "admitted_at_current_budget": "Admitted",
    "stops": "Stops",
    "total_cost": "Total cost",
}

# The three cells this report always names a dedicated summary line for,
# whatever their outcome (D-15, and this project's own dallas_tx-seattle_wa
# lineage -- 25-CONTEXT.md, ADMISSION_MANIFEST's own dated notes). Never
# conditional on a happy outcome.
_HOUSTON_CELL_KEY = ("houston_tx-chicago_il", Decimal(500))
_DALLAS_CELL_KEY = ("dallas_tx-seattle_wa", Decimal(500))
_DEMO_CELL_KEY = ("demo_la_ca-new_york_ny", Decimal(1050))


def _measure_sweep(grid_command, repeats, *, trust_margin, strengthened_prune):
    """Measure every row `_build_grid(None)` returns (the full 26-cell
    sweep) through the sibling command's own `_measure_cell`, called on a
    real instance -- never a bare module-level import of the method name
    and never a null `self`, the same discipline
    `measure_refresh_diff._measure_grid` documents. Routed through its own
    function so command-level tests can mock this ONE seam instead of
    running two real 26-cell sweeps, which is minutes of solver work by
    design.

    `trust_margin` and `strengthened_prune` are both required, keyword-
    only -- which world this call measures (which margin, which rule) is
    always the caller's explicit decision, never a silent default.
    Forwarded to every cell as
    `grid_command._measure_cell(row, repeats, trust_margin=trust_margin,
    strengthened_prune=strengthened_prune)`."""
    grid = _build_grid(None)
    return [
        grid_command._measure_cell(
            row,
            repeats,
            trust_margin=trust_margin,
            strengthened_prune=strengthened_prune,
        )
        for row in grid
    ]


def _attribute_cell(row):
    """Independently rebuild ONE grid row's candidate list and attribute
    the strengthened rule's removals to one of `classify_removals`'s four
    classes (D-08) -- co_located, tail, margin_blocked, penalty_dominated.

    Never routed through `_measure_cell`, which exposes only counts
    (`raw_candidates`, `kept`), not the candidate objects `classify_
    removals` needs. This is therefore a NEW production
    `prune_dominated_candidates` call site -- `test_boundaries.py`'s
    pinned `PRUNE_CALL_SITE_PRODUCTION_COUNT`/`PRUNE_CALL_SITE_TOTAL_COUNT`
    inventory is bumped in the same commit that adds this function to
    account for it, never left stale.

    Returns the four-tuple `classify_removals` returns:
    `(co_located, tail, margin_blocked, penalty_dominated)`. Lets any
    `CommandError` it raises propagate unchanged -- an unattributable
    removal must stop this report, not annotate it."""
    route = row["loader"](row["slug"])
    factor_for = factor_lookup_for_basis(row["price_basis"])
    candidates = corridor.candidates(route, factor_for=factor_for)
    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )
    retained = prune_dominated_candidates(
        candidates,
        tank_range_mi=row["tank_range_mi"],
        total_route_mi=route.total_route_mi,
        mpg=row["mpg"],
        penalty=PENALTY,
    )
    return classify_removals(
        ordered,
        retained,
        tank_range_mi=row["tank_range_mi"],
        total_route_mi=route.total_route_mi,
        mpg=row["mpg"],
        penalty=PENALTY,
    )


@dataclass
class CellDiff:
    """One cell's before/after comparison between the current domination
    rule (`before`) and the strengthened rule (`after`) -- D-18, mirroring
    `measure_refresh_diff.CellDiff`'s shape with the before/after semantics
    renamed from margin-world to rule-world. `before`/`after` are the raw
    `CellResult` objects, kept around so the report renderer can read any
    field it needs, not only the six diffed ones. `co_located`/`tail`/
    `margin_blocked`/`penalty_dominated` are the AFTER world's
    `classify_removals()` attribution for this cell (D-08), computed
    independently via `_attribute_cell` -- always `0` until attribution
    has run."""

    slug: str
    tank_range_mi: Decimal
    before: object = None
    after: object = None
    changed_fields: list = field(default_factory=list)
    censorship_transition: str = None
    censorship_reason: str = ""
    co_located: int = 0
    tail: int = 0
    margin_blocked: int = 0
    penalty_dominated: int = 0

    @property
    def is_changed(self):
        return bool(self.changed_fields or self.censorship_transition)


def diff_cell_results(before_results, after_results):
    """Pure diff over two lists of `CellResult`: no file or database
    access, so it is testable against synthetic inputs. Cells are keyed by
    `(slug, tank_range_mi)`. Both worlds are always built from
    `_build_grid(None)` -- the same, complete 26-cell grid -- so this
    raises `CommandError` if the two key sets ever differ, rather than
    silently reporting a partial diff. A censorship flip in either
    direction is recorded as its own kind of event, carrying the reason,
    and is never coerced into one of the six numeric fields below -- a
    cell whose `solve()` never ran has no real stop count or cost to
    report as zero."""
    before_by_key = {(r.slug, r.tank_range_mi): r for r in before_results}
    after_by_key = {(r.slug, r.tank_range_mi): r for r in after_results}
    if set(before_by_key) != set(after_by_key):
        raise CommandError(
            "the before and after worlds' cell keys differ -- both sweeps "
            "must cover the identical 26-cell grid: "
            f"before-only={sorted(set(before_by_key) - set(after_by_key))}, "
            f"after-only={sorted(set(after_by_key) - set(before_by_key))}"
        )
    all_keys = sorted(before_by_key, key=lambda k: (k[0], str(k[1])))

    diffs = []
    for slug, tank_range_mi in all_keys:
        before = before_by_key[(slug, tank_range_mi)]
        after = after_by_key[(slug, tank_range_mi)]
        diff = CellDiff(slug=slug, tank_range_mi=tank_range_mi, before=before, after=after)

        before_censored = before.censored
        after_censored = after.censored
        if before_censored != after_censored:
            if after_censored:
                diff.censorship_transition = "measurable_to_censored"
                diff.censorship_reason = after.censored_reason
            else:
                diff.censorship_transition = "censored_to_measurable"
                diff.censorship_reason = before.censored_reason
        elif not before_censored and not after_censored:
            for field_name in DIFFED_FIELDS:
                before_value = getattr(before, field_name)
                after_value = getattr(after, field_name)
                if before_value != after_value:
                    diff.changed_fields.append((field_name, before_value, after_value))
        # both censored: nothing meaningful to compare among the six
        # numeric fields; not treated as a change.

        diffs.append(diff)

    return diffs


def _check_plan_identity(diffs):
    """ROADMAP criterion 4's gate -- the one genuine pass/fail check this
    command owns. For every cell where BOTH worlds are measurable (neither
    censored), `admitted_at_current_budget` is the same in both worlds,
    AND `timed_strategy` is the same in both worlds, `total_cost` and the
    chosen stops' `opis_id` tuple must be identical. Cells whose dispatch
    arm changed are NOT checked here -- those are exactly the recoveries
    this sweep exists to find, and applying this gate to them would forbid
    the very thing being measured.

    Callers must attribute every diff (populate `co_located`/`tail`/
    `margin_blocked`/`penalty_dominated`) BEFORE calling this function --
    see the amendment below for why.

    [Amended 2026-08-17, Phase 25] Also exempts any cell whose AFTER-world
    attribution shows `penalty_dominated > 0`. `prune.py`'s own "Bound,
    precisely" derivation (condition 4, D-01/D-02) PROVES a
    penalty-dominated removal can raise the true optimum by up to (but
    strictly less than) `penalty` -- a real, proven, BOUNDED cost
    movement, not a substitution bug. Without this exemption the gate
    would flag every such cell as a correctness failure; real committed
    data confirms this actually happens on an ADMITTED, arm-unchanged
    cell: `houston_tx-chicago_il@500mi` moves $241.9747... -> $242.9016...
    (stop opis_id 66643 -> 64617) under the strengthened rule, a $0.93
    increase, comfortably under the $35 penalty bound -- exactly the
    counterexample class `prune.py`'s own docstring already documents,
    now reproduced on real data rather than a synthetic Hypothesis draw.
    Conditions 1-3 (co_located, tail, margin_blocked) remain fully
    exact-substitution-safe (zero regret, proven in `prune.py`'s
    "Soundness" section) and stay subject to this identity check exactly
    as ROADMAP criterion 4 requires -- only the ONE condition proven to
    carry bounded, nonzero regret is exempted, and only on the cells
    where it actually fired (never on the strength of `kept` alone,
    which can also move from conditions 1-3 without breaking identity;
    the attribution tally is the precise signal).

    Collects every violation and raises ONE `CommandError` naming each
    violating cell with both costs and both stop tuples, rather than
    stopping at the first."""
    violations = []
    for d in diffs:
        before, after = d.before, d.after
        if before.censored or after.censored:
            continue
        if before.admitted_at_current_budget != after.admitted_at_current_budget:
            continue
        if before.timed_strategy != after.timed_strategy:
            continue
        if d.penalty_dominated > 0:
            continue
        if before.total_cost != after.total_cost or before.stop_opis_ids != after.stop_opis_ids:
            violations.append(
                f"{d.slug} @{d.tank_range_mi}mi: total_cost "
                f"{before.total_cost} -> {after.total_cost}, stop opis_ids "
                f"{before.stop_opis_ids} -> {after.stop_opis_ids}"
            )
    if violations:
        raise CommandError(
            "Plan-identity gate (ROADMAP criterion 4) failed on cell(s) "
            "whose dispatch arm did NOT change between worlds: "
            + "; ".join(violations)
            + ". A prune that removes a station the solver would have "
            "chosen is a correctness bug, not an optimization."
        )


def _plateau_rows(diffs):
    """Build `plateau_verdict()`'s own required input shape from this
    report's diffs: `was_demoted` is each cell's PRE-Phase-25 admission
    state, read from `ADMISSION_MANIFEST` (never recomputed), and
    `estimate` is the AFTER (strengthened-rule) world's post-prune
    estimate."""
    rows = []
    for d in diffs:
        manifest_key = (d.slug, int(d.tank_range_mi))
        was_admitted = ADMISSION_MANIFEST[manifest_key]
        rows.append(
            {
                "slug": d.slug,
                "tank_range_mi": d.tank_range_mi,
                "was_demoted": not was_admitted,
                "estimate": d.after.estimate,
            }
        )
    return rows


def _diff_for(diffs, slug, tank_range_mi):
    for d in diffs:
        if d.slug == slug and d.tank_range_mi == tank_range_mi:
            return d
    return None


def _field_display(result, field_name):
    if result is None:
        return "(absent)"
    if result.censored:
        return "CENSORED"
    value = getattr(result, field_name)
    if field_name == "total_cost":
        return f"${value:.2f}"
    return str(value)


def _render_changed_cell_line(d):
    if d.censorship_transition:
        return (
            f"- {d.slug} @{d.tank_range_mi}mi: {d.censorship_transition} "
            f"-- {d.censorship_reason}"
        )
    field_bits = ", ".join(
        f"{name} {before}->{after}" for name, before, after in d.changed_fields
    )
    return f"- {d.slug} @{d.tank_range_mi}mi: {field_bits}"


def _render_changed_section(title, diffs):
    lines = [f"### {title}", ""]
    if not diffs:
        lines.append("(none)")
    else:
        for d in diffs:
            lines.append(_render_changed_cell_line(d))
    lines.append("")
    return lines


def _render_measurement_basis_section(production_trust_margin):
    """`## Measurement basis` -- this report's own objective statement
    (D-09): which margin both worlds were measured at and why neither is
    ever `Decimal(0)` (ROADMAP criterion 2), that the after world reaches
    the strengthened rule through this measurement seam and NEVER through
    `solve()` (D-14), what criterion 4's plan-identity gate checked, and
    the preflight-ordering fidelity deviation Task 1 (plan 25-05) recorded.
    Mirrors `measure_refresh_diff._render_measurement_basis_section`'s
    heading-plus-lines-plus-blank-terminator shape; the content is new,
    not inherited -- this section does NOT carry that file's now-corrected
    margin-independence claim (D-09's own amendment already fixed the
    source; this command declines the bullet entirely rather than cloning
    it)."""
    return [
        "## Measurement basis",
        "",
        (
            "- Both worlds -- the current rule (before) and the "
            "strengthened rule (after) -- were measured at the SAME "
            f"production trust margin (${production_trust_margin}), read "
            "live from settings.TRUST_MARGIN_USD. Neither world is ever "
            "measured at a zero margin -- ROADMAP criterion 2 forbids it, "
            "the exact W4 bias class this project already found and "
            "corrected once (measure_refresh_diff.py, Phase 24)."
        ),
        (
            "- The after world reaches the strengthened rule ENTIRELY "
            "through this measurement seam -- measure_dispatch_grid."
            "Command._measure_cell's strengthened_prune parameter -- and "
            "NEVER by routing.services.solver.solve() itself supplying "
            "mpg= or penalty= to prune_dominated_candidates. solver.py is "
            "unmodified (D-14; PruneInertnessGateTest is the CI-enforcing "
            "structural backstop for that convention). Nothing this "
            "report shows is what production currently serves."
        ),
        (
            "- Criterion 4's plan-identity gate: on every cell whose "
            "dispatch arm (admitted_at_current_budget) and timed strategy "
            "are unchanged between worlds AND whose after-world attribution "
            "shows zero penalty-dominated removals, total_cost and the "
            "chosen stops' opis_id tuple were asserted identical -- a "
            "removed-but-chosen station on a conditions-1-3-only cell "
            "would be a correctness bug, not an optimization. Cells with "
            "a nonzero penalty-dominated count are exempted from this "
            "identity check, not from measurement: prune.py's own "
            "'Bound, precisely' derivation proves that condition (bounded "
            "regret strictly under `penalty`, never exact substitution), "
            "so a cost movement there is expected, proven behaviour, not "
            "a violation. This report only ever renders once the (scoped) "
            "check has passed; a violation raises CommandError instead of "
            "a rendered table."
        ),
        (
            "- Fidelity deviation (Task 1, plan 25-05): in production, "
            "solve() runs dp.preflight_gap_check over the UNPRUNED "
            "candidate list, then prunes afterwards. This report's after "
            "world instead passes an already-pruned search set with "
            "prune=False, so that same check runs over the PRUNED list -- "
            "sound by prune.py's own D-05 reach-safety argument (removal "
            "never manufactures a new infeasibility, so a gap invisible "
            "in the pruned list was never a real gap in the unpruned one "
            "either). A divergence here is itself a finding and such a "
            "cell is censored with an explicit reason, never silently "
            "dropped."
        ),
        "",
    ]


def _render_summary_sections(diffs):
    lines = ["## Summary", ""]

    recovered = [
        d
        for d in diffs
        if not ADMISSION_MANIFEST[(d.slug, int(d.tank_range_mi))]
        and not d.after.censored
        and d.after.admitted_at_current_budget
    ]
    lines.append(
        f"- {len(recovered)} of {DEMOTED_CELL_COUNT} previously-demoted "
        "cells are admitted at dp.DP_TRANSITION_BUDGET "
        f"({dp.DP_TRANSITION_BUDGET}) under the strengthened rule."
    )

    verdict = plateau_verdict(_plateau_rows(diffs))
    lines.append(f"- Plateau verdict (Phase 25 D-10, plateau_verdict()): {verdict}")

    houston = _diff_for(diffs, *_HOUSTON_CELL_KEY)
    if houston is not None and not houston.before.censored and not houston.after.censored:
        before_est, after_est = houston.before.estimate, houston.after.estimate
        if after_est < before_est:
            direction = "further inside dp.DP_TRANSITION_BUDGET"
        elif after_est > before_est:
            direction = "toward the boundary"
        else:
            direction = "unchanged"
        lines.append(
            "- houston_tx-chicago_il@500mi (currently admitted at "
            f"{before_est} of {dp.DP_TRANSITION_BUDGET}): moved "
            f"{direction} under the strengthened rule ({before_est} -> "
            f"{after_est}). A sweep reporting recoveries must also report "
            "anything that moved the other way."
        )

    dallas = _diff_for(diffs, *_DALLAS_CELL_KEY)
    if dallas is not None:
        dallas_admitted = (
            not dallas.after.censored and dallas.after.admitted_at_current_budget
        )
        dallas_estimate = "CENSORED" if dallas.after.censored else dallas.after.estimate
        lines.append(
            "- dallas_tx-seattle_wa@500mi: "
            f"{'admitted' if dallas_admitted else 'still demoted'} under "
            f"the strengthened rule (estimate {dallas_estimate}). D-15: "
            "if this cell is admitted, that is recorded as a finding and "
            "acted on by NOBODY in Phase 25 -- no guard changes and "
            "nothing ships. Phase 26 reconciles it on deployed evidence."
        )

    demo = _diff_for(diffs, *_DEMO_CELL_KEY)
    if demo is not None:
        demo_admitted = not demo.after.censored and demo.after.admitted_at_current_budget
        demo_estimate = "CENSORED" if demo.after.censored else demo.after.estimate
        lines.append(
            "- demo_la_ca-new_york_ny@1050mi: "
            f"{'admitted' if demo_admitted else 'still demoted'} under "
            f"the strengthened rule (estimate {demo_estimate})."
        )

    lines.append("")
    return lines


def render_report(*, production_trust_margin, diffs):
    """Pure renderer: takes the production trust margin and the full list
    of per-cell `CellDiff` objects (each already carrying its after-world
    attribution tallies) and returns the report text as one string. No
    file or database access, so it is testable against synthetic diffs."""
    changed_diffs = [d for d in diffs if d.is_changed]
    admission_flips = [
        d
        for d in changed_diffs
        if any(name == "admitted_at_current_budget" for name, _, _ in d.changed_fields)
    ]
    censorship_transitions = [d for d in changed_diffs if d.censorship_transition]
    other_movements = [
        d
        for d in changed_diffs
        if d not in admission_flips and d not in censorship_transitions
    ]

    lines = [
        "# Strengthened Domination Prune -- 26-Cell Dispatch Diff (PRUN-04)",
        "",
        f"- Cells measured: {len(diffs)}",
        (
            f"- Changed cells: {len(changed_diffs)} (admission flips: "
            f"{len(admission_flips)}, censorship transitions: "
            f"{len(censorship_transitions)}, other movements: "
            f"{len(other_movements)})"
        ),
        f"- penalty=${PENALTY} for every cell.",
        "",
    ]
    lines += _render_measurement_basis_section(production_trust_margin)

    field_headers = [_FIELD_DISPLAY_NAMES[name] for name in DIFFED_FIELDS]
    lines += [
        "## Per-cell table",
        "",
        "| Cell | Tank (mi) | "
        + " | ".join(field_headers)
        + " | co_located | tail | margin_blocked | penalty_dominated | Changed |",
        "|" + "---|" * (2 + len(field_headers) + 4 + 1),
    ]
    for d in diffs:
        cells = [
            f"{_field_display(d.before, name)}->{_field_display(d.after, name)}"
            for name in DIFFED_FIELDS
        ]
        marker = "CHANGED" if d.is_changed else ""
        lines.append(
            f"| {d.slug} | {d.tank_range_mi} | "
            + " | ".join(cells)
            + f" | {d.co_located} | {d.tail} | {d.margin_blocked} | "
            f"{d.penalty_dominated} | {marker} |"
        )
    lines.append("")

    lines += ["## Changed cells", ""]
    lines += _render_changed_section("Admission flips", admission_flips)
    lines += _render_changed_section("Censorship transitions", censorship_transitions)
    lines += _render_changed_section("Other movements (cost/stops/etc.)", other_movements)

    lines += _render_summary_sections(diffs)

    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "Measure Phase 25's strengthened domination prune's plan-quality "
        "effect across the full 26-cell ADMISSION_MANIFEST grid (PRUN-04): "
        "twelve corridors at both pinned tank ranges plus the two demo "
        "chips, current rule vs. strengthened rule, both worlds measured "
        "at the production trust margin (settings.TRUST_MARGIN_USD, never "
        "Decimal(0) -- ROADMAP criterion 2). Offline: replays committed "
        "corridor and demo-chip geometry fixtures and rebuilds the "
        "station table from the committed CSVs, no outbound network call "
        "of any kind, works with no routing-provider (Mapbox) token set. "
        "Must NOT run in CI -- the figures this command prints are "
        "evidence, not a pass/fail gate, EXCEPT ROADMAP criterion 4's "
        "plan-identity check, which IS a genuine gate: it raises "
        "CommandError if a conditions-1-3-only removal (co_located/tail/"
        "margin_blocked -- proven exact-substitution-safe) ever changes a "
        "cell's plan on a cell whose dispatch arm did not change. Cells "
        "where condition 4 (penalty domination) actually fired are "
        "exempted from that identity check by design -- prune.py's own "
        "'Bound, precisely' derivation proves that condition carries "
        "bounded, nonzero regret, so a cost movement there is expected, "
        "not a bug."
    )

    def handle(self, *args, **options):
        grid_command = MeasureDispatchGridCommand()

        # Read live, near the top of handle() -- no pinned literal for the
        # production value and no assertion that it equals the committed
        # default (D-19), the same call-site style measure_refresh_diff.py
        # and probe_live_latency.py already use. Unlike measure_refresh_
        # diff.py, there is NO baseline zero-margin world here at all:
        # ROADMAP criterion 2 requires this sweep be taken at the
        # production margin and never at Decimal(0) -- both worlds below
        # read this same value.
        production_trust_margin = settings.TRUST_MARGIN_USD

        self.stdout.write(
            "Rebuilding the station table from the committed CSVs "
            "(manage.py seed_stations, idempotent replay, no network "
            "call)..."
        )
        reseed_all(stdout=io.StringIO())
        corridor.reset_index()

        self.stdout.write(
            "Measuring the BEFORE world (current domination rule) at the "
            f"production trust margin (${production_trust_margin}) -- "
            "this takes minutes..."
        )
        before_results = _measure_sweep(
            grid_command,
            _MEASURE_REPEATS,
            trust_margin=production_trust_margin,
            strengthened_prune=False,
        )

        self.stdout.write(
            "Measuring the AFTER world (strengthened domination rule) at "
            f"the SAME production trust margin (${production_trust_margin}"
            ") -- this takes minutes..."
        )
        after_results = _measure_sweep(
            grid_command,
            _MEASURE_REPEATS,
            trust_margin=production_trust_margin,
            strengthened_prune=True,
        )

        diffs = diff_cell_results(before_results, after_results)

        self.stdout.write(
            "Attributing the strengthened rule's removals (classify_"
            "removals, D-08)..."
        )
        grid_by_key = {(row["slug"], row["tank_range_mi"]): row for row in _build_grid(None)}
        for d in diffs:
            row = grid_by_key[(d.slug, d.tank_range_mi)]
            co_located, tail, margin_blocked, penalty_dominated = _attribute_cell(row)
            d.co_located = co_located
            d.tail = tail
            d.margin_blocked = margin_blocked
            d.penalty_dominated = penalty_dominated

        # Attribution must run BEFORE this gate -- _check_plan_identity's
        # own amendment exempts cells by their penalty_dominated tally,
        # which is only populated by the loop above.
        _check_plan_identity(diffs)

        report_text = render_report(
            production_trust_margin=production_trust_margin,
            diffs=diffs,
        )
        self.stdout.write(report_text)
        self.stdout.write(
            self.style.SUCCESS(
                "Sweep complete: plan-identity gate passed, "
                f"{len(diffs)} cell(s) measured."
            )
        )

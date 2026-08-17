"""Measure the plan-quality effect of a proposed station-data change: swap
the file at the canonical Overture station path, measure the 26-cell
dispatch grid before and after, and render the diff PIPE-04 requires the
refresh pull request to report.

This is the inverse of the sibling `measure_dispatch_grid` command's own
"Must NOT run in CI" posture: THIS command IS meant to run inside the
refresh workflow. It reuses `measure_dispatch_grid._measure_cell` --
called on a real instance of that command's class, never imported as a
bare module-level name and never invoked against a null `self` -- rather
than invoking that command directly, which is precisely what keeps the
sibling command's own prohibition on running in CI literally true and
unqualified. Nothing here defines a new measurement concept; the
diff engine already existed, this only wraps it.

Read-only apart from the station-table replay `reseed_all()` triggers and
the one canonical CSV this command deliberately swaps. No outbound network
call of any kind, and it works with no routing-provider token set -- the
two 26-cell sweeps replay committed corridor and demo-chip geometry fixtures
exactly as the sibling command does.

Two worlds are produced by swapping the file at the canonical path, never
by parameterizing a seed path (D-10): measure against the committed CSVs,
copy the candidate's bytes over `data/overture_stations.csv`, measure
again, then restore that file via `git checkout` by default. `reseed_all()`
and `SeedStationsCallSiteGateTest` (Phase 22 D-29) stay untouched because
this command never passes a path to `seed_stations` -- it swaps the file
those paths point at instead.

`--keep-after` (D-23) inverts the restore step: the safe default restores
the canonical path for local and developer runs, and the refresh
pipeline's own invocation passes `--keep-after` so the candidate bytes
stay in place for the subsequent `git add`/`git commit` that carries them
into the pull request diff. Restoring unconditionally after every run
would revert the very content that commit needs to carry.
"""
import csv
import io
import shutil
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.management.commands.measure_dispatch_grid import (
    Command as MeasureDispatchGridCommand,
    _build_grid,
)
from routing.models import Station
from routing.services import corridor
from routing.services.station_csv_paths import DATA_DIR, reseed_all
from routing.tests.test_corridor_fixtures import DEMO_CHIP_VEHICLE
from routing.tests.test_solver_dispatch import ADMISSION_MANIFEST_VEHICLE, PENALTY

DEFAULT_REPORT_PATH = DATA_DIR / "overture-refresh-report.md"

# The one hand-written station CSV path in this module, derived from the
# paths module's own DATA_DIR rather than a fresh settings.BASE_DIR lookup
# -- the canonical Overture station CSV `import_overture_stations` writes
# and `CANONICAL_STATION_CSV_PATHS` already lists. Never parameterized
# (D-10); this command swaps the file sitting at this fixed path, it never
# accepts an alternate canonical path as an argument.
CANONICAL_OVERTURE_CSV_PATH = DATA_DIR / "overture_stations.csv"

# Repeats exist in the sibling command for a worst-of-N timing measurement.
# None of the six fields this command diffs (raw_candidates, kept,
# estimate, admitted_at_current_budget, stops, total_cost) is a timing, so
# a second pass buys nothing here and would double a sweep that already
# runs 26 cells twice (once per world). Fixed at 1, not exposed as a flag.
_MEASURE_REPEATS = 1

# The six fields Phase 22's own by-hand diff compared, in table-column
# order. The censorship flag is deliberately NOT a seventh member of this
# tuple -- a censorship flip is reported as its own kind of event (see
# diff_cell_results), never coerced into one of these six.
DIFFED_FIELDS = (
    "raw_candidates",
    "kept",
    "estimate",
    "admitted_at_current_budget",
    "stops",
    "total_cost",
)

# Display names for DIFFED_FIELDS, in the same order -- used to build both
# margin worlds' per-cell table column headings (Phase 24, D-01).
_FIELD_DISPLAY_NAMES = {
    "raw_candidates": "Raw candidates",
    "kept": "Kept",
    "estimate": "Estimate",
    "admitted_at_current_budget": "Admitted",
    "stops": "Stops",
    "total_cost": "Total cost",
}


def _count_csv_data_rows(path):
    """Count data rows in a CSV file, excluding its header row. Raises
    FileNotFoundError if `path` does not exist -- callers translate that
    into a CommandError naming the problem."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        return sum(1 for _ in reader)


def _canonical_csv_is_dirty():
    """True when CANONICAL_OVERTURE_CSV_PATH carries any uncommitted
    modification (staged or unstaged) against HEAD. The restore step below
    is a checkout of this exact file, so starting from a dirty file means
    the run could not put the tree back the way it found it."""
    result = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", str(CANONICAL_OVERTURE_CSV_PATH)],
        cwd=settings.BASE_DIR,
    )
    return result.returncode != 0


def _restore_canonical_csv():
    """Shell out to `git checkout -- <canonical path>`, putting the working
    tree's canonical Overture station CSV back exactly as it was found.
    Routed through its own function -- never inlined -- so tests can assert
    on invocation, and never swallows a failure: a silent restore failure
    would leave a swapped dataset sitting at the canonical path with no
    signal that anything is wrong."""
    result = subprocess.run(
        ["git", "checkout", "--", str(CANONICAL_OVERTURE_CSV_PATH)],
        cwd=settings.BASE_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CommandError(
            f"git checkout of {CANONICAL_OVERTURE_CSV_PATH} failed: "
            f"{result.stderr.strip()}"
        )


def _preflight(candidate_path):
    """Refuse to start, naming the problem, rather than risk leaving the
    tree in a state this command cannot restore. Returns nothing; raises
    CommandError on any failure."""
    if not candidate_path.exists():
        raise CommandError(f"Candidate CSV does not exist: {candidate_path}")
    if _count_csv_data_rows(candidate_path) < 1:
        raise CommandError(
            f"Candidate CSV {candidate_path} has no data rows beyond its header"
        )
    if not CANONICAL_OVERTURE_CSV_PATH.exists():
        raise CommandError(
            "Canonical Overture station CSV does not exist: "
            f"{CANONICAL_OVERTURE_CSV_PATH}"
        )
    if _canonical_csv_is_dirty():
        raise CommandError(
            f"{CANONICAL_OVERTURE_CSV_PATH} has uncommitted modifications -- "
            "commit or discard them before running this command, so the "
            "restore step at the end can put the tree back exactly as it "
            "found it."
        )


def _measure_grid(grid_command, repeats, *, trust_margin):
    """Measure every row `_build_grid(None)` returns (the full 26-cell
    sweep) through the sibling command's own `_measure_cell`, called on a
    real instance -- never a bare module-level import of the method name,
    and never a null `self` (D-11), even though the method body happens not
    to use it today. Routed through its own function so command-level
    tests can mock this one seam instead of running two real 26-cell
    sweeps, which is minutes of solver work by design.

    `trust_margin` is required, keyword-only -- which margin world this
    call measures is now the caller's explicit decision (Phase 24), never
    a silent default. Forwarded to every cell as
    `grid_command._measure_cell(row, repeats, trust_margin=trust_margin)`."""
    grid = _build_grid(None)
    return [
        grid_command._measure_cell(row, repeats, trust_margin=trust_margin)
        for row in grid
    ]


@dataclass
class CellDiff:
    """One cell's before/after comparison. `before`/`after` are the raw
    CellResult objects (or None when the cell is absent from that world) --
    kept around so the report renderer can read any field it needs, not
    only the six diffed ones."""

    slug: str
    tank_range_mi: Decimal
    before: object = None
    after: object = None
    changed_fields: list = field(default_factory=list)
    censorship_transition: str = None
    censorship_reason: str = ""
    presence_event: str = None

    @property
    def is_changed(self):
        return bool(
            self.changed_fields or self.censorship_transition or self.presence_event
        )


def diff_cell_results(before_results, after_results):
    """Pure diff over two lists of CellResult: no file or database access,
    so it is testable against synthetic inputs. Cells are keyed by
    (slug, tank_range_mi). A censorship flip in either direction is
    recorded as its own kind of event, carrying the reason, and is never
    coerced into one of the six numeric fields below -- a cell whose
    solve() never ran has no real stop count or cost to report as zero. A
    cell present in one world and absent from the other is also its own
    event, never a silent skip."""
    before_by_key = {(r.slug, r.tank_range_mi): r for r in before_results}
    after_by_key = {(r.slug, r.tank_range_mi): r for r in after_results}
    all_keys = sorted(
        set(before_by_key) | set(after_by_key), key=lambda k: (k[0], str(k[1]))
    )

    diffs = []
    for slug, tank_range_mi in all_keys:
        before = before_by_key.get((slug, tank_range_mi))
        after = after_by_key.get((slug, tank_range_mi))
        diff = CellDiff(slug=slug, tank_range_mi=tank_range_mi, before=before, after=after)

        if before is None:
            diff.presence_event = "missing_before"
            diffs.append(diff)
            continue
        if after is None:
            diff.presence_event = "missing_after"
            diffs.append(diff)
            continue

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


def _categorize_changed_cells(diffs):
    """Split the changed cells into the report's four sections, in the
    order the rendered report presents them: admission flips first, then
    censorship transitions, then presence changes, then remaining cost/stop
    (and other numeric) movements. Each diff appears in exactly one
    section."""
    admission_flips = []
    censorship_transitions = []
    presence_events = []
    other_movements = []
    for d in diffs:
        if d.presence_event:
            presence_events.append(d)
        elif d.censorship_transition:
            censorship_transitions.append(d)
        elif any(name == "admitted_at_current_budget" for name, _, _ in d.changed_fields):
            admission_flips.append(d)
        elif d.changed_fields:
            other_movements.append(d)
    return admission_flips, censorship_transitions, presence_events, other_movements


def _field_display(result, field_name):
    if result is None:
        return "(absent)"
    if result.censored:
        return "CENSORED"
    value = getattr(result, field_name)
    if field_name == "total_cost":
        return f"${value:.2f}"
    return str(value)


def _pair_margin_worlds(production_diffs, baseline_diffs):
    """Pair the two margin worlds' diff lists by (slug, tank_range_mi) --
    the same key and sort order `diff_cell_results` already uses at
    :201-203 -- yielding ordered `(production_diff, baseline_diff)` tuples.
    Either element is `None` when that key is absent from that world's own
    diff list, so a cell present in one world and missing from the other
    still pairs and renders, never raising."""
    production_by_key = {(d.slug, d.tank_range_mi): d for d in production_diffs}
    baseline_by_key = {(d.slug, d.tank_range_mi): d for d in baseline_diffs}
    all_keys = sorted(
        set(production_by_key) | set(baseline_by_key),
        key=lambda k: (k[0], str(k[1])),
    )
    for slug, tank_range_mi in all_keys:
        yield (
            production_by_key.get((slug, tank_range_mi)),
            baseline_by_key.get((slug, tank_range_mi)),
        )


def _render_world_cells(world_diff):
    """Six `before->after` cells for one margin world's `DIFFED_FIELDS`,
    built from `_field_display` exactly as before -- or six `(absent)`
    placeholders when the cell is missing from that world's diff list
    entirely (D-01's presence-safety requirement)."""
    if world_diff is None:
        return ["(absent)"] * len(DIFFED_FIELDS)
    before_cells = [_field_display(world_diff.before, name) for name in DIFFED_FIELDS]
    after_cells = [_field_display(world_diff.after, name) for name in DIFFED_FIELDS]
    return [f"{b}->{a}" for b, a in zip(before_cells, after_cells)]


def _field_headers_for_margin(margin):
    """Six column headings for one margin world, each of the form
    `<Field> @$<margin>`, interpolated from the argument -- never
    hardcoded (D-01)."""
    return [f"{_FIELD_DISPLAY_NAMES[name]} @${margin}" for name in DIFFED_FIELDS]


def _render_table_row(production_diff, baseline_diff):
    """One per-cell table row pairing both margin worlds for the same
    cell: the six production-world cells first, then the six
    baseline-world cells, both still in the existing `before->after` form.
    The trailing `CHANGED` marker reflects the production world's own
    `is_changed` only (D-03) -- the baseline world can never set it."""
    anchor = production_diff or baseline_diff
    all_cells = _render_world_cells(production_diff) + _render_world_cells(baseline_diff)
    marker = "CHANGED" if production_diff is not None and production_diff.is_changed else ""
    return (
        f"| {anchor.slug} | {anchor.tank_range_mi} | "
        + " | ".join(all_cells)
        + f" | {marker} |"
    )


def _render_changed_cell_line(d):
    if d.presence_event:
        which_world = "after" if d.presence_event == "missing_after" else "before"
        return (
            f"- {d.slug} @{d.tank_range_mi}mi: cell is absent from the "
            f"{which_world} world ({d.presence_event})"
        )
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


def _render_measurement_basis_section(production_trust_margin, baseline_trust_margin):
    """`## Measurement basis` -- the report's own objective statement
    (D-09): which margin each column set was measured at, which one
    drives the changed-cell verdict, which fields the margin can and
    cannot move (D-10), and the direction of this report's own bias.
    Mirrors `_render_changed_section`'s heading-plus-lines-plus-blank-
    terminator shape; each bullet is a multi-sentence string in the same
    style `## Reviewer notes`'s own bullets already use."""
    return [
        "## Measurement basis",
        "",
        (
            "- Production column set: every solve in it ran at the "
            f"production trust margin (${production_trust_margin}), read "
            "live from settings.TRUST_MARGIN_USD, which is the objective "
            "a driver's plan is actually computed under. This is the "
            "authoritative column set for the reviewer's decision."
        ),
        (
            "- Baseline column set: every solve in it ran at a zero trust "
            f"margin (${baseline_trust_margin}), the objective every "
            "Phase-22 hand measurement and every pinned calibration table "
            "in this project was taken under. It is shown for "
            "comparability with the project's own recorded history, never "
            "for the reviewer's decision."
        ),
        (
            "- Which set drives the verdict: the changed-cell count in "
            "the header above and all four ## Changed cells subsections "
            "below are computed from the production column set alone. A "
            "cell that moves only in the baseline column set is not "
            "counted as changed and is not listed in any of those "
            "sections."
        ),
        (
            # [Amended 2026-08-17, Phase 25] The prior wording below stated
            # the four fields cannot move with the margin BECAUSE
            # prune_dominated_candidates takes no trust-margin argument at
            # all. That causal claim no longer holds: Phase 25 widened the
            # function's signature with keyword-only mpg/penalty parameters
            # (both defaulting to None), so it now CAN read a dollar-valued
            # penalty. The conclusion below is still literally true of THIS
            # command, though, because measure_dispatch_grid (which this
            # command drives) always calls the unstrengthened default path
            # -- Phase 25 lands the strengthened rule INERT (D-14), and a
            # dedicated AST gate (test_boundaries.PruneInertnessGateTest)
            # proves solve() and every measure_dispatch_grid call site never
            # supply those two parameters. The narrowed, TRUE cause is
            # stated below in place of the superseded one.
            "- Margin independence: the trust margin can move only "
            "stops and total_cost. raw_candidates, kept, estimate and "
            "admitted_at_current_budget cannot move with it for the rule "
            "this command drives -- the unstrengthened, margin-blind "
            "default path prune_dominated_candidates() always runs on here "
            "(Phase 25 landed a strengthened, penalty-aware branch of that "
            "same function, but it ships inert: this command never "
            "supplies the two parameters that would activate it). Four "
            "identical column pairs is therefore the expected result "
            "below, not a rendering bug -- proven against real committed "
            "data by TwoMarginWorldRenderTests."
        ),
        (
            "- Direction of the bias: every Overture row is priced as an "
            "eia_regional_estimate, precisely the rows the trust margin "
            "exists to suppress, so the zero-margin (baseline) columns "
            "overstate how often those rows get selected. This states the "
            "direction only; it makes no attempt to quantify the size of "
            "the effect, in keeping with this project's standing "
            "rejection of false precision."
        ),
        "",
    ]


def render_report(
    *,
    candidate_path,
    canonical_path,
    before_overture_row_count,
    after_overture_row_count,
    before_routable_count,
    after_routable_count,
    production_diffs,
    baseline_diffs,
    production_trust_margin,
    baseline_trust_margin,
):
    """Pure renderer: takes the two header figure pairs, both margin
    worlds' diff lists and both margin values, and returns report text.
    No file or database access, so it is testable against synthetic
    diffs. This is the exact text that lands both in the refresh pull
    request body and as the committed data/overture-refresh-report.md
    (D-11/D-12) -- one artifact, two destinations, never a forked or
    shortened second renderer.

    The production diff list is authoritative (D-03): changed_count and
    all four `## Changed cells` sections below are computed from it
    alone. The baseline diff list feeds only the per-cell table's second
    column set."""
    admission_flips, censorship_transitions, presence_events, other_movements = (
        _categorize_changed_cells(production_diffs)
    )
    changed_count = sum(1 for d in production_diffs if d.is_changed)

    lines = [
        "# Overture Refresh Diff Report",
        "",
        f"- Candidate path: {candidate_path}",
        f"- Canonical path: {canonical_path}",
        (
            "- overture_stations.csv rows: "
            f"before={before_overture_row_count} after={after_overture_row_count}"
        ),
        (
            "- Routable stations (Station.objects.routable()): "
            f"before={before_routable_count} after={after_routable_count}"
        ),
        f"- Cells measured: {len(production_diffs)}",
        (
            f"- Changed cells: {changed_count} (admission flips: "
            f"{len(admission_flips)}, censorship transitions: "
            f"{len(censorship_transitions)}, presence changes: "
            f"{len(presence_events)}, other movements: {len(other_movements)})"
        ),
        (
            f"- Corridor cells use mpg={ADMISSION_MANIFEST_VEHICLE['mpg']}, "
            f"starting_fuel={ADMISSION_MANIFEST_VEHICLE['starting_fuel']}, "
            f"price_basis={ADMISSION_MANIFEST_VEHICLE['price_basis']}; demo "
            f"cells use the SPA hero preset mpg={DEMO_CHIP_VEHICLE['mpg']}, "
            f"tank_range_mi={DEMO_CHIP_VEHICLE['tank_range_mi']}, "
            f"starting_fuel={DEMO_CHIP_VEHICLE['starting_fuel']} -- these two "
            f"vehicles are never conflated. penalty=${PENALTY} for every cell."
        ),
        "",
    ]
    lines += _render_measurement_basis_section(
        production_trust_margin, baseline_trust_margin
    )

    field_headers = _field_headers_for_margin(
        production_trust_margin
    ) + _field_headers_for_margin(baseline_trust_margin)
    lines += [
        "## Per-cell table",
        "",
        "| Cell | Tank (mi) | " + " | ".join(field_headers) + " | Changed |",
        "|" + "---|" * (2 + len(field_headers) + 1),
    ]
    for production_diff, baseline_diff in _pair_margin_worlds(
        production_diffs, baseline_diffs
    ):
        lines.append(_render_table_row(production_diff, baseline_diff))
    lines.append("")

    lines += ["## Changed cells", ""]
    lines += _render_changed_section("Admission flips", admission_flips)
    lines += _render_changed_section("Censorship transitions", censorship_transitions)
    lines += _render_changed_section("Presence changes", presence_events)
    lines += _render_changed_section("Other movements (cost/stops/etc.)", other_movements)

    lines += [
        "## Reviewer notes",
        "",
        (
            "- The two large CSVs this pull request carries "
            "(data/overture_stations.csv and data/overture_raw_extract.csv) "
            "will not render a usable line-by-line diff in the review UI "
            "because of their size -- this table is the reviewable "
            "artifact by design."
        ),
        (
            "- A red DispatchAdmissionManifestTests guard inside this pull "
            "request is the design working, not a defect in this pipeline. "
            "It must be resolved by a deliberate hand re-pin of "
            "ADMISSION_MANIFEST, never by a regenerate path -- there is no "
            "regenerate path, and none should be added."
        ),
        (
            "- The committed prose station counts in README.md and "
            "docs/ALGORITHM.md are hand-maintained and may now be stale. "
            "Use this report's own before/after overture_stations.csv-row "
            "and routable-station counts above when updating them."
        ),
        "",
    ]
    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "Measure the plan-quality effect of a proposed station-data "
        "change: measure the 26-cell dispatch grid against the committed "
        "CSVs, swap in the candidate CSV at the canonical Overture station "
        "path, measure again, and render the before/after diff PIPE-04 "
        "requires the refresh pull request to report. This command IS "
        "meant to run inside the refresh workflow -- it reuses "
        "measure_dispatch_grid._measure_cell (called on an instance) "
        "rather than invoking that command, which is exactly what keeps "
        "that command's own 'Must NOT run in CI' docstring literally true "
        "and unqualified. Read-only apart from the station-table replay it "
        "triggers and the one canonical CSV it deliberately swaps; no "
        "outbound network call of any kind; works with no "
        "routing-provider token set."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "candidate_path",
            type=str,
            help="Path to the candidate Overture station CSV to measure against.",
        )
        parser.add_argument(
            "--report-path",
            dest="report_path",
            default=str(DEFAULT_REPORT_PATH),
            help=(
                "Where to write the refresh diff report. Default: "
                "data/overture-refresh-report.md"
            ),
        )
        parser.add_argument(
            "--keep-after",
            dest="keep_after",
            action="store_true",
            default=False,
            help=(
                "Leave the candidate bytes at the canonical path after "
                "measuring, instead of restoring via git checkout. The "
                "refresh pipeline's own invocation passes this flag so the "
                "subsequent git add/git commit captures the candidate "
                "data (D-23); the default restores, the safe behaviour "
                "for local and developer runs."
            ),
        )

    def handle(self, *args, **options):
        candidate_path = Path(options["candidate_path"])
        report_path = Path(options["report_path"])
        keep_after = options["keep_after"]

        _preflight(candidate_path)

        grid_command = MeasureDispatchGridCommand()

        # Read live, near the top of handle() -- no pinned literal for the
        # production value and no assertion that it equals the committed
        # default (D-04), the same call-site style routing/views.py:635
        # and probe_live_latency.py:381 already use. The baseline world
        # measures at zero, the objective every Phase-22 hand measurement
        # and every pinned calibration table in this project was taken
        # under.
        production_trust_margin = settings.TRUST_MARGIN_USD
        baseline_trust_margin = Decimal(0)

        self.stdout.write(
            "Rebuilding the station table from the committed CSVs "
            "(measuring the BEFORE world)..."
        )
        reseed_all(stdout=io.StringIO())
        corridor.reset_index()
        self.stdout.write(
            "Measuring the BEFORE world's 26-cell dispatch grid at the "
            f"production trust margin (${production_trust_margin}) -- "
            "this takes minutes..."
        )
        before_production_results = _measure_grid(
            grid_command, _MEASURE_REPEATS, trust_margin=production_trust_margin
        )
        self.stdout.write(
            "Measuring the BEFORE world's 26-cell dispatch grid at the "
            f"zero baseline margin (${baseline_trust_margin}) -- this "
            "takes minutes..."
        )
        before_baseline_results = _measure_grid(
            grid_command, _MEASURE_REPEATS, trust_margin=baseline_trust_margin
        )
        before_overture_row_count = _count_csv_data_rows(CANONICAL_OVERTURE_CSV_PATH)
        before_routable_count = Station.objects.routable().count()
        self.stdout.write(
            f"BEFORE world measured: {len(before_production_results)} "
            "cell(s) per margin world, overture_stations.csv rows="
            f"{before_overture_row_count}, routable stations="
            f"{before_routable_count}"
        )

        self.stdout.write(
            f"Swapping the candidate CSV over {CANONICAL_OVERTURE_CSV_PATH}..."
        )
        shutil.copyfile(candidate_path, CANONICAL_OVERTURE_CSV_PATH)

        self.stdout.write(
            "Rebuilding the station table from the candidate CSV "
            "(measuring the AFTER world)..."
        )
        reseed_all(stdout=io.StringIO())
        corridor.reset_index()
        self.stdout.write(
            "Measuring the AFTER world's 26-cell dispatch grid at the "
            f"production trust margin (${production_trust_margin}) -- "
            "this takes minutes..."
        )
        after_production_results = _measure_grid(
            grid_command, _MEASURE_REPEATS, trust_margin=production_trust_margin
        )
        self.stdout.write(
            "Measuring the AFTER world's 26-cell dispatch grid at the "
            f"zero baseline margin (${baseline_trust_margin}) -- this "
            "takes minutes..."
        )
        after_baseline_results = _measure_grid(
            grid_command, _MEASURE_REPEATS, trust_margin=baseline_trust_margin
        )
        after_overture_row_count = _count_csv_data_rows(CANONICAL_OVERTURE_CSV_PATH)
        after_routable_count = Station.objects.routable().count()
        self.stdout.write(
            f"AFTER world measured: {len(after_production_results)} "
            "cell(s) per margin world, overture_stations.csv rows="
            f"{after_overture_row_count}, routable stations="
            f"{after_routable_count}"
        )

        if keep_after:
            self.stdout.write(
                self.style.WARNING(
                    "--keep-after set: leaving the candidate bytes at "
                    f"{CANONICAL_OVERTURE_CSV_PATH} for a subsequent git "
                    "add/git commit (D-23). A developer running this "
                    "locally usually wants the default restore instead."
                )
            )
        else:
            self.stdout.write(
                f"Restoring {CANONICAL_OVERTURE_CSV_PATH} via git checkout..."
            )
            _restore_canonical_csv()

        # The production-margin diff is authoritative (D-03): it alone
        # feeds changed_count, every `_categorize_changed_cells` section
        # and the closing stdout success line below. The baseline diff
        # feeds only the report's second column set -- a cell that moves
        # solely at margin zero is never counted as changed.
        production_diffs = diff_cell_results(
            before_production_results, after_production_results
        )
        baseline_diffs = diff_cell_results(
            before_baseline_results, after_baseline_results
        )
        report_text = render_report(
            candidate_path=candidate_path,
            canonical_path=CANONICAL_OVERTURE_CSV_PATH,
            before_overture_row_count=before_overture_row_count,
            after_overture_row_count=after_overture_row_count,
            before_routable_count=before_routable_count,
            after_routable_count=after_routable_count,
            production_diffs=production_diffs,
            baseline_diffs=baseline_diffs,
            production_trust_margin=production_trust_margin,
            baseline_trust_margin=baseline_trust_margin,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")

        admission_flips, _, _, _ = _categorize_changed_cells(production_diffs)
        changed_count = sum(1 for d in production_diffs if d.is_changed)
        self.stdout.write(
            self.style.SUCCESS(
                f"Refresh diff complete: {changed_count} changed cell(s) "
                f"at the production trust margin, {len(admission_flips)} "
                f"admission flip(s). Report written to {report_path}"
            )
        )

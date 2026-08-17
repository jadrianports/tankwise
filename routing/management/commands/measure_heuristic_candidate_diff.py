"""Measure -- never ship -- what production loses by feeding the
penalty-aware heuristic the FULL unpruned candidate list instead of the
shipped domination prune's own search set (D-16: this command's entire
purpose is measurement; nothing it computes ever reaches `solve()`).

D-17: a two-worlds harness varying exactly ONE input -- the heuristic's
own candidate-list argument -- on the same build, at the same production
`settings.TRUST_MARGIN_USD` and the same `settings.FUEL_STOP_PENALTY_USD`,
never at a zero margin.

- **World A** -- `heuristic.solve_penalty_aware_heuristic(raw_candidates,
  ...)` -- what `solve()` actually feeds the heuristic today, on both of
  its own call sites (`routing/services/solver.py:544-552`,
  the deadline-breach fallback, and `:557-565`, the over-budget branch;
  both pass the function's own full, original `candidates` argument,
  never the pruned `search_set`).
- **World B** -- `prune_dominated_candidates(raw_candidates, ...)` with
  `mpg`/`penalty` both OMITTED (their default, `None`) -- the SHIPPED,
  three-condition domination rule, never the strengthened, penalty-aware
  branch Phase 25 landed inert -- then the heuristic over that reduced
  search set.

**The load-bearing reason this harness never calls `routing.services.
solver.solve()`, stated plainly (RESEARCH.md `<d17_harness>`, Pitfall 3):**
`solve()`'s own two heuristic call sites always pass its ORIGINAL, full
`candidates` argument, never `search_set` -- confirmed by direct read of
both call sites, and by the module's own comment at `solver.py:539-540`
stating this explicitly. Composing `solve()` (mirroring
`measure_prune_dispatch_diff.py`'s `solve_candidates`/`solve_prune` swap)
would therefore feed the heuristic the SAME full candidate list in both
worlds regardless of what was swapped -- measuring nothing. This harness
sidesteps that entirely by calling
`heuristic.solve_penalty_aware_heuristic()` directly, once per world, and
imports neither `routing.services.solver` nor the name `solve` anywhere
in this module.

Scoped to exactly the 14 `ADMISSION_MANIFEST` cells whose pinned value is
`False` (`HEURISTIC_DIFF_CELLS`, derived, never hand-listed) -- the only
cells production actually dispatches to this arm under the shipped
`dp.DP_TRANSITION_BUDGET`. Measuring an exact-arm cell here would be
evidence about a code path nothing executes for that cell.

Read-only and offline: replays the demoted cells' committed corridor and
demo-chip geometry fixtures through the existing Directions-response
parser, and rebuilds the station table from the committed CSVs, exactly
as `measure_dispatch_grid.py` already does. No outbound network call of
any kind, and works with no routing-provider (Mapbox) token set.

Must NOT run in CI -- every figure this command prints is evidence for
D-18's three-place landing of the finding, not a pass/fail gate. Exits 0
unconditionally on a successful sweep.
"""
import io
import subprocess
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.services import corridor, heuristic
from routing.services.exceptions import InfeasibleRouteError
from routing.services.prune import prune_dominated_candidates
from routing.services.station_csv_paths import reseed_all
from routing.tests.test_corridor_fixtures import (
    DEMO_CHIP_VEHICLE,
    DEMO_CHIPS,
    factor_lookup_for_basis,
    load_corridor_route,
    load_demo_chip_route,
)
from routing.tests.test_dispatch_recovery import DEMOTED_CELL_COUNT
from routing.tests.test_solver_dispatch import ADMISSION_MANIFEST, ADMISSION_MANIFEST_VEHICLE

# The 14 demoted cells -- derived from ADMISSION_MANIFEST's own False
# entries, never hand-listed, so a future re-pin of the manifest is
# reflected here automatically rather than silently drifting stale.
# Order follows ADMISSION_MANIFEST's own declared order (insertion
# order, Python 3.7+ dict semantics).
HEURISTIC_DIFF_CELLS = tuple(
    (slug, tank_range_mi)
    for (slug, tank_range_mi), admitted in ADMISSION_MANIFEST.items()
    if not admitted
)

_DEMO_CHIP_SLUGS = frozenset(chip.slug for chip in DEMO_CHIPS)


def plan_objective(total_cost, stop_count):
    """The quantity the solver's fixed-charge objective actually
    minimises (D-17): `total_cost` plus a flat
    `settings.FUEL_STOP_PENALTY_USD` charge per stop. Reads the penalty
    LIVE from Django settings on every call -- never a hardcoded literal
    -- exactly like `settings.TRUST_MARGIN_USD` is read live elsewhere in
    this command, so a future default change is reflected automatically
    rather than silently going stale. A plan can be cheaper on raw fuel
    cost and worse on this objective (more stops), which is exactly why
    D-17 requires this figure reported alongside `total_cost`, not
    instead of it.
    """
    return total_cost + settings.FUEL_STOP_PENALTY_USD * stop_count


@dataclass
class CellDiff:
    """One demoted cell's World A (full unpruned candidates) versus
    World B (shipped-prune search set) comparison, both worlds run
    directly through `heuristic.solve_penalty_aware_heuristic()` at the
    same production penalty and trust margin. `verdict` is computed on
    the OBJECTIVE (`plan_objective`), not on `total_cost` alone -- D-17's
    own requirement, since a cheaper-fuel/more-stops plan can be worse on
    the quantity the solver actually minimises."""

    slug: str
    tank_range_mi: Decimal
    raw_candidates: int
    world_b_search_set: int
    stops_a: int
    stops_b: int
    total_cost_a: Decimal
    total_cost_b: Decimal
    objective_a: Decimal
    objective_b: Decimal
    objective_delta: Decimal
    verdict: str


def _row_for_cell(slug, tank_range_mi):
    """Resolve the correct route loader and vehicle for one
    `HEURISTIC_DIFF_CELLS` entry -- `DEMO_CHIP_VEHICLE`/
    `load_demo_chip_route` for the two demo slugs, `ADMISSION_MANIFEST_
    VEHICLE`/`load_corridor_route` for every other (corridor) slug --
    mirroring `measure_dispatch_grid.py`'s own `_build_grid` row shape
    and `test_solver_dispatch.py`'s `_load_manifest_cell_route_and_
    vehicle` match-then-represent rule (D-14), built independently here
    rather than importing that module's own underscore-prefixed helper.
    """
    if slug in _DEMO_CHIP_SLUGS:
        return {
            "slug": slug,
            "tank_range_mi": Decimal(tank_range_mi),
            "loader": load_demo_chip_route,
            "mpg": DEMO_CHIP_VEHICLE["mpg"],
            "starting_fuel": DEMO_CHIP_VEHICLE["starting_fuel"],
            "price_basis": DEMO_CHIP_VEHICLE["price_basis"],
        }
    return {
        "slug": slug,
        "tank_range_mi": Decimal(tank_range_mi),
        "loader": load_corridor_route,
        "mpg": ADMISSION_MANIFEST_VEHICLE["mpg"],
        "starting_fuel": ADMISSION_MANIFEST_VEHICLE["starting_fuel"],
        "price_basis": ADMISSION_MANIFEST_VEHICLE["price_basis"],
    }


def _measure_cell(row, *, penalty, trust_margin):
    """Measure one cell's two worlds and return its `CellDiff`. Never
    routed through `routing.services.solver.solve()` -- see the module
    docstring's load-bearing paragraph. `mpg`/`penalty` are omitted
    (left at their `None` default) from the ONE
    `prune_dominated_candidates` call below -- the SHIPPED,
    unstrengthened three-condition rule -- so the strengthened,
    penalty-aware branch never activates (D-17). This is a NEW
    production `prune_dominated_candidates` call site; `test_boundaries.
    py`'s pinned `PRUNE_CALL_SITE_PRODUCTION_COUNT`/`PRUNE_CALL_SITE_
    TOTAL_COUNT` inventory is bumped in the same commit that adds this
    function, never left stale (the same discipline `measure_prune_
    dispatch_diff.py`'s own `_attribute_cell` docstring states).
    """
    slug = row["slug"]
    tank_range_mi = row["tank_range_mi"]
    loader = row["loader"]
    mpg = row["mpg"]
    starting_fuel = row["starting_fuel"]
    factor_for = factor_lookup_for_basis(row["price_basis"])

    route = loader(slug)
    raw_candidates = corridor.candidates(route, factor_for=factor_for)

    try:
        plan_a = heuristic.solve_penalty_aware_heuristic(
            raw_candidates,
            route.total_route_mi,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
            penalty=penalty,
            trust_margin=trust_margin,
        )
    except InfeasibleRouteError as exc:
        raise CommandError(
            f"{slug}@{tank_range_mi}mi: World A (full unpruned "
            f"candidates) raised InfeasibleRouteError -- unexpected, "
            f"since production dispatches this exact cell to the "
            f"heuristic over the same full candidate list today: {exc}"
        ) from exc

    search_set_b = prune_dominated_candidates(
        raw_candidates,
        tank_range_mi=tank_range_mi,
        total_route_mi=route.total_route_mi,
        # mpg=None, penalty=None (the default) -- the SHIPPED,
        # unstrengthened three-condition rule. The strengthened branch
        # never enters (D-17).
    )

    try:
        plan_b = heuristic.solve_penalty_aware_heuristic(
            search_set_b,
            route.total_route_mi,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
            penalty=penalty,
            trust_margin=trust_margin,
        )
    except InfeasibleRouteError as exc:
        raise CommandError(
            f"{slug}@{tank_range_mi}mi: World B (shipped-prune search "
            f"set) raised InfeasibleRouteError -- this would be a real "
            f"prune.py D-05 reach-safety violation, not a measurement "
            f"artifact: {exc}"
        ) from exc

    stops_a = len(plan_a.stops)
    stops_b = len(plan_b.stops)
    objective_a = plan_objective(plan_a.total_cost, stops_a)
    objective_b = plan_objective(plan_b.total_cost, stops_b)
    delta = objective_b - objective_a
    if delta < 0:
        verdict = "BETTER"
    elif delta > 0:
        verdict = "WORSE"
    else:
        verdict = "SAME"

    return CellDiff(
        slug=slug,
        tank_range_mi=tank_range_mi,
        raw_candidates=len(raw_candidates),
        world_b_search_set=len(search_set_b),
        stops_a=stops_a,
        stops_b=stops_b,
        total_cost_a=plan_a.total_cost,
        total_cost_b=plan_b.total_cost,
        objective_a=objective_a,
        objective_b=objective_b,
        objective_delta=delta,
        verdict=verdict,
    )


def _git_sha():
    """Best-effort `git rev-parse HEAD`, degrading to a stated placeholder
    if `git` is unavailable -- never raises, since a missing git binary
    must not stop an otherwise-successful offline sweep."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:  # noqa: BLE001 - degrade, never fail the sweep on this
        return "(unavailable -- git rev-parse HEAD failed)"


def render_report(*, diffs, penalty, trust_margin, git_sha):
    """Pure renderer: takes the measured diffs plus the three basis facts
    and returns the report text as one string. No file, network or
    database access, so it is testable against synthetic diffs."""
    lines = [
        "# Heuristic Candidate-List Diff -- D-17 Two-Worlds Harness (measurement only, D-16)",
        "",
        f"- Cells measured: {len(diffs)} (the {DEMOTED_CELL_COUNT} demoted ADMISSION_MANIFEST cells)",
        f"- penalty=${penalty} for every cell.",
        "",
        "## Per-cell table",
        "",
        "| Cell | Tank (mi) | Raw candidates | World B search set | "
        "Stops A | Stops B | Total cost A | Total cost B | Objective A | "
        "Objective B | Objective delta | Verdict |",
        "|" + "---|" * 12,
    ]
    for d in diffs:
        lines.append(
            f"| {d.slug} | {d.tank_range_mi} | {d.raw_candidates} | "
            f"{d.world_b_search_set} | {d.stops_a} | {d.stops_b} | "
            f"${d.total_cost_a:.2f} | ${d.total_cost_b:.2f} | "
            f"${d.objective_a:.2f} | ${d.objective_b:.2f} | "
            f"${d.objective_delta:.2f} | {d.verdict} |"
        )
    lines.append("")

    worse = [d for d in diffs if d.verdict == "WORSE"]
    better = [d for d in diffs if d.verdict == "BETTER"]
    same = [d for d in diffs if d.verdict == "SAME"]

    lines += ["## Cells that get WORSE if shipped", ""]
    lines.append(
        "D-17 requires every loss named, not only the wins -- this "
        "section exists whatever the sweep's outcome."
    )
    if not worse:
        lines.append("(none -- no cell's objective increased under World B)")
    else:
        for d in worse:
            lines.append(
                f"- {d.slug} @{d.tank_range_mi}mi: objective "
                f"${d.objective_a:.2f} -> ${d.objective_b:.2f} "
                f"(+${d.objective_delta:.2f})"
            )
    lines.append("")

    total_delta = sum((d.objective_delta for d in diffs), Decimal(0))
    lines += [
        "## Aggregate",
        "",
        f"- {len(better)} of {len(diffs)} cell(s) BETTER, {len(worse)} "
        f"WORSE, {len(same)} SAME (by objective, plan_objective()).",
        "- Net objective delta (sum of World B - World A objective "
        f"across all {len(diffs)} cell(s)): ${total_delta:.2f}.",
        "",
    ]

    lines += [
        "## Measurement basis",
        "",
        (
            "- Both worlds -- World A (full unpruned candidates, what "
            "solve() actually feeds the heuristic today) and World B "
            "(the SHIPPED, unstrengthened prune's search set) -- were "
            f"measured at the SAME production trust margin (${trust_margin}"
            "), read live from settings.TRUST_MARGIN_USD. Neither world "
            "is ever measured at a zero margin."
        ),
        (
            f"- penalty=${penalty}, read live from "
            "settings.FUEL_STOP_PENALTY_USD, identical in both worlds."
        ),
        f"- Git SHA of the tree measured: {git_sha}.",
        (
            "- Offline: replays committed corridor/demo-chip geometry "
            "fixtures and the CSV-rebuilt station table. No outbound "
            "network call of any kind, and no routing-provider (Mapbox) "
            "token is required."
        ),
        (
            "- This harness bypasses routing.services.solver.solve() "
            "entirely -- both worlds call "
            "heuristic.solve_penalty_aware_heuristic() directly, "
            "because solve() hands its heuristic call sites the FULL "
            "unpruned candidates in every case (solver.py:544-552, "
            ":557-565), so a harness composed through solve() would "
            "return identical results in both worlds and measure "
            "nothing."
        ),
        (
            "- World B's prune_dominated_candidates() call omits both "
            "mpg and penalty, so only the SHIPPED three-condition "
            "domination rule ever runs -- the strengthened, "
            "penalty-aware branch never enters (D-17)."
        ),
        (
            "- Nothing about the heuristic's candidate list ships from "
            "this command -- measurement only (D-16)."
        ),
        "",
    ]
    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "Measure -- never ship -- what production loses by feeding the "
        "penalty-aware heuristic the FULL unpruned candidate list "
        "instead of the shipped domination prune's own search set "
        "(D-16/D-17), on the 14 demoted ADMISSION_MANIFEST cells. Both "
        "worlds call heuristic.solve_penalty_aware_heuristic() directly, "
        "bypassing routing.services.solver.solve() entirely, at the same "
        "production penalty and trust margin, never a zero margin. "
        "Offline: replays committed corridor/demo-chip geometry "
        "fixtures and rebuilds the station table from the committed "
        "CSVs, no outbound network call of any kind, works with no "
        "routing-provider (Mapbox) token set. Must NOT run in CI -- "
        "every figure this command prints is evidence, not a pass/fail "
        "gate."
    )

    def handle(self, *args, **options):
        penalty = settings.FUEL_STOP_PENALTY_USD
        trust_margin = settings.TRUST_MARGIN_USD

        self.stdout.write(
            "Rebuilding the station table from the committed CSVs "
            "(manage.py seed_stations, idempotent replay, no network "
            "call)..."
        )
        reseed_all(stdout=io.StringIO())
        corridor.reset_index()

        self.stdout.write(
            f"Measuring {len(HEURISTIC_DIFF_CELLS)} demoted cells -- "
            "World A (full unpruned candidates) vs World B (shipped-"
            f"prune search set), both at penalty=${penalty} and trust "
            f"margin=${trust_margin}..."
        )

        diffs = [
            _measure_cell(
                _row_for_cell(slug, tank_range_mi),
                penalty=penalty,
                trust_margin=trust_margin,
            )
            for slug, tank_range_mi in HEURISTIC_DIFF_CELLS
        ]

        git_sha = _git_sha()
        report_text = render_report(
            diffs=diffs,
            penalty=penalty,
            trust_margin=trust_margin,
            git_sha=git_sha,
        )
        self.stdout.write(report_text)
        self.stdout.write(
            self.style.SUCCESS(
                f"Sweep complete: {len(diffs)} cell(s) measured."
            )
        )

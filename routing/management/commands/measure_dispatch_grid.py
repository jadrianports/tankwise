"""Measure the widened 26-cell dispatch grid -- the twelve committed
corridors at both pinned tank ranges plus the two demo chips -- and apply
the pinned adoption and live-probe-selection rules from
`routing.tests.test_dispatch_recovery` exactly as written, with no
reinterpretation.

Must NOT run in CI -- the figures this command prints are evidence, not a
pass/fail gate. `DispatchAdmissionManifestTests`
(`routing.tests.test_solver_dispatch`) is the CI-enforcing guard that
exists instead; this command exists to report the widened figure that
guard's own 24-cell manifest is measured against, plus the two demo
cells that guard does not cover.

This module defines no corridor, tank range, vehicle, penalty, budget,
ladder rung, floor or verdict threshold of its own. Every value is
imported from `routing.tests.test_dispatch_recovery`
(`DP_TRANSITION_BUDGET_LADDER`, `adopt_budget_rung`,
`LIVE_PROBE_MAX_CELLS`, `select_live_probe_cells`,
`RESPONSE_BAR_SECONDS`), `routing.tests.test_solver_dispatch`
(`ADMISSION_MANIFEST_VEHICLE`, `PENALTY` -- the exact vehicle the stale
41.7% figure was measured at) and `routing.tests.test_corridor_fixtures`
(`CORRIDORS`, `TANK_RANGES_MI`, `DEMO_CHIPS`, `DEMO_CHIP_VEHICLE`,
`load_corridor_route`, `load_demo_chip_route`, `factor_lookup_for_basis`)
-- the single shared sources of truth pinned before this command's own
measurement ever ran.

Read-only and offline: replays the twelve committed corridor geometry
fixtures plus the two committed demo-chip fixtures through the existing
Directions-response parser, and rebuilds the station table from the
committed CSV, exactly as `measure_dispatch_predictor.py` and
`measure_plan_objective.py` already do. No outbound network call of any
kind, and works with no routing-provider token set. The one write this
command triggers is the same idempotent `seed_stations` CSV replay every
other measurement command in this codebase already performs -- never a
mutation invented here, and never a write to `routing/tests/` (D-15:
there is no regenerate flag and none should ever be added -- see this
command's own `help` string).

Three distinct vehicles appear in this command's output and must never be
conflated (D-14): the 24 corridor cells are measured at
`ADMISSION_MANIFEST_VEHICLE` (mpg=10, starting_fuel=0.5, neutral basis --
the same vehicle the stale 41.7% figure was measured at, so this run's
replacement figure is comparable to it); the 2 demo cells are measured at
`DEMO_CHIP_VEHICLE` (the SPA hero preset: 6.5 mpg / 1050 mi tank / full
tank), because that is literally what a visitor clicking the chip sends;
and the API default (10 mpg / 500 mi tank / full tank, starting_fuel=1)
is a third, distinct vehicle used elsewhere (`DeployedHardwareDispatchTests`,
the post-deploy smoke gate) and is only named here for the record, never
measured by this command.

**The "timed" column's `worst_timed_response_seconds` approximation.**
`adopt_budget_rung`'s own docstring asks for "the worst-of-repeats total
end-to-end-equivalent response time" -- this command is offline-only and
issues no live request, so it approximates that figure with its own
solve()-only wall-clock time under the production deadline. This is an
approximation, stated as such at the point it is used; the true
end-to-end figure is plan 09's live spot-check, and this offline verdict
does not substitute for it.
"""
import io
import time
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from routing.services import corridor, dp
from routing.services.exceptions import InfeasibleRouteError
from routing.services.prune import prune_dominated_candidates
from routing.services.solver import solve
from routing.services.station_csv_paths import reseed_all
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    DEMO_CHIP_VEHICLE,
    DEMO_CHIPS,
    TANK_RANGES_MI,
    factor_lookup_for_basis,
    load_corridor_route,
    load_demo_chip_route,
)
from routing.tests.test_dispatch_recovery import (
    DP_TRANSITION_BUDGET_LADDER,
    LIVE_PROBE_MAX_CELLS,
    RESPONSE_BAR_SECONDS,
    adopt_budget_rung,
    select_live_probe_cells,
)
from routing.tests.test_solver_dispatch import ADMISSION_MANIFEST_VEHICLE, PENALTY

# The historical figure this run supersedes (non-scalar-dispatch-rule.md),
# reported side by side with this run's measured figures below, never
# reconciled. No parameter above was adjusted after seeing how this run's
# own numbers compare to these (mirrors measure_plan_objective.py's own
# `_HISTORICAL_*` convention for the same reason).
_HISTORICAL_DEMOTED_COUNT = 10
_HISTORICAL_TOTAL_COUNT = 24
_HISTORICAL_DEMOTED_PERCENT = 41.7
_HISTORICAL_CORRIDOR_COUNT = 7
_HISTORICAL_CORRIDOR_TOTAL = 12

# Named only, for the record -- never measured by this command (D-14).
_API_DEFAULT_VEHICLE = {
    "mpg": Decimal(10),
    "tank_range_mi": Decimal(500),
    "starting_fuel": Decimal(1),
}

_DEFAULT_REPEATS = 2


@dataclass
class CellResult:
    slug: str
    tank_range_mi: Decimal
    is_demo_cell: bool
    raw_candidates: int = 0
    kept: int = 0
    estimate: int = 0
    admitted_at_current_budget: bool = False
    admitted_by_rung: dict = field(default_factory=dict)
    untimed_solve_seconds: Decimal = Decimal(0)
    untimed_strategy: str = ""
    timed_strategy: str = ""
    timed_solve_seconds: Decimal = Decimal(0)
    breached: bool = False
    stops: int = 0
    total_cost: Decimal = Decimal(0)
    demo_stop_detail: list = field(default_factory=list)
    censored: bool = False
    censored_reason: str = ""
    # [Amended 2026-08-17, Phase 25] The chosen stops' opis_id values, as a
    # tuple, populated from untimed_plan.stops for EVERY cell (not only
    # demo cells). Consumer: plan 25-05's measure_prune_dispatch_diff
    # command, ROADMAP criterion 4's plan-identity gate -- stops (a count)
    # plus total_cost cannot detect a same-count, same-cost station
    # substitution, so the chosen set itself must be comparable.
    stop_opis_ids: tuple = ()


def _parse_cell_filter(raw_cells):
    if not raw_cells:
        return None
    parsed = set()
    for item in raw_cells:
        if "@" not in item:
            raise CommandError(
                f"--cell must be 'slug@tank' (e.g. "
                f"'dallas_tx-seattle_wa@500'), got {item!r}"
            )
        slug, _, tank_str = item.rpartition("@")
        try:
            parsed.add((slug, Decimal(tank_str)))
        except Exception as exc:  # noqa: BLE001 - re-raised as a CommandError
            raise CommandError(
                f"--cell tank component must be a number, got {item!r}"
            ) from exc
    return parsed


def _build_grid(cell_filter):
    grid = []
    for c in CORRIDORS:
        for tank in TANK_RANGES_MI:
            grid.append(
                {
                    "slug": c.slug,
                    "tank_range_mi": tank,
                    "is_demo_cell": False,
                    "loader": load_corridor_route,
                    "mpg": ADMISSION_MANIFEST_VEHICLE["mpg"],
                    "starting_fuel": ADMISSION_MANIFEST_VEHICLE["starting_fuel"],
                    "price_basis": ADMISSION_MANIFEST_VEHICLE["price_basis"],
                }
            )
    for chip in DEMO_CHIPS:
        grid.append(
            {
                "slug": chip.slug,
                "tank_range_mi": DEMO_CHIP_VEHICLE["tank_range_mi"],
                "is_demo_cell": True,
                "loader": load_demo_chip_route,
                "mpg": DEMO_CHIP_VEHICLE["mpg"],
                "starting_fuel": DEMO_CHIP_VEHICLE["starting_fuel"],
                "price_basis": DEMO_CHIP_VEHICLE["price_basis"],
            }
        )
    if cell_filter is None:
        return grid
    return [row for row in grid if (row["slug"], row["tank_range_mi"]) in cell_filter]


class Command(BaseCommand):
    help = (
        "Measure the widened 26-cell dispatch grid (the twelve committed "
        "corridors at both pinned tank ranges, plus the two demo chips) "
        "and apply the pinned adoption and live-probe-selection rules "
        "from routing.tests.test_dispatch_recovery exactly as written, "
        "with no reinterpretation. Read-only beyond the seed_stations "
        "replay it triggers; no outbound network calls; works with no "
        "routing-provider token set. Must NOT run in CI -- the figures "
        "printed are evidence, not a pass/fail gate; "
        "DispatchAdmissionManifestTests in "
        "routing/tests/test_solver_dispatch.py is the CI-enforcing guard "
        "that exists instead."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--cell",
            action="append",
            default=None,
            help=(
                "Re-measure a single cell instead of the full 26-cell "
                "sweep, given as 'slug@tank' (e.g. "
                "'dallas_tx-seattle_wa@500'). Repeatable."
            ),
        )
        parser.add_argument(
            "--repeats",
            type=int,
            default=_DEFAULT_REPEATS,
            help=(
                "Number of repeats for the untimed (deadline=None) "
                f"worst-of-N measurement (default {_DEFAULT_REPEATS})."
            ),
        )

    def handle(self, *args, **options):
        repeats = options["repeats"]
        cell_filter = _parse_cell_filter(options.get("cell"))

        self.stdout.write(
            "Rebuilding the station table from the committed CSV "
            "(manage.py seed_stations, idempotent replay, no network "
            "call)..."
        )
        # reseed_all(), not a bare call_command("seed_stations", ...): a
        # measurement that seeds only a subset of the canonical CSV list
        # reports the wrong world -- the same failure class
        # SeedStationsCallSiteGateTest (routing/tests/test_boundaries.py)
        # exists to catch statically.
        reseed_all(stdout=io.StringIO())
        corridor.reset_index()
        self.stdout.write("")

        self.stdout.write(
            self.style.SUCCESS(
                "Three vehicles appear below and must never be conflated "
                "(D-14): corridor cells use "
                f"mpg={ADMISSION_MANIFEST_VEHICLE['mpg']}, "
                f"starting_fuel={ADMISSION_MANIFEST_VEHICLE['starting_fuel']}, "
                f"price_basis={ADMISSION_MANIFEST_VEHICLE['price_basis']} "
                "(comparable to the stale 41.7% figure); demo cells use "
                "the SPA hero preset "
                f"mpg={DEMO_CHIP_VEHICLE['mpg']}, "
                f"tank_range_mi={DEMO_CHIP_VEHICLE['tank_range_mi']}, "
                f"starting_fuel={DEMO_CHIP_VEHICLE['starting_fuel']}; the "
                f"API default (mpg={_API_DEFAULT_VEHICLE['mpg']}, "
                f"tank_range_mi={_API_DEFAULT_VEHICLE['tank_range_mi']}, "
                f"starting_fuel={_API_DEFAULT_VEHICLE['starting_fuel']}) is "
                "a third, distinct vehicle, named here but never measured "
                f"by this command. penalty=${PENALTY} for every cell."
            )
        )
        self.stdout.write("")

        grid = _build_grid(cell_filter)
        results = [self._measure_cell(row, repeats) for row in grid]

        self._print_table(results)
        self._print_summary(results)
        self._print_rung_table(results)
        adopted_rung = self._print_verdict(results)
        self._print_probe_selection(results, adopted_rung)
        self._print_disclaimer()

    def _measure_cell(
        self, row, repeats, trust_margin=Decimal(0), *, strengthened_prune=False
    ):
        slug = row["slug"]
        tank_range_mi = row["tank_range_mi"]
        loader = row["loader"]
        mpg = row["mpg"]
        starting_fuel = row["starting_fuel"]
        factor_for = factor_lookup_for_basis(row["price_basis"])

        result = CellResult(
            slug=slug,
            tank_range_mi=tank_range_mi,
            is_demo_cell=row["is_demo_cell"],
        )

        route = loader(slug)
        raw_candidates = corridor.candidates(route, factor_for=factor_for)
        result.raw_candidates = len(raw_candidates)

        # [Amended 2026-08-17, Phase 25] `strengthened_prune` (plan 25-05,
        # D-18/D-19) selects which prune rule this cell is measured under.
        # `handle()`'s own call (`results = [self._measure_cell(row,
        # repeats) for row in grid]`) passes neither this parameter nor
        # `trust_margin`, so it always takes the False branch below --
        # this method's published behaviour at its existing call site is
        # therefore unmoved. False (the default): mpg=None, penalty=None
        # are passed EXPLICITLY, which is byte-identical to omitting them
        # -- `prune_dominated_candidates` defaults both to `None`, and the
        # "Penalty domination" branch in `prune.py` only activates when
        # BOTH are supplied (D-04). True: the cell's own `row["mpg"]` and
        # this module's pinned `PENALTY` constant are threaded through,
        # activating the strengthened rule for this cell's search set
        # alone -- `routing/services/solver.py` is never touched (D-14).
        search_set = prune_dominated_candidates(
            raw_candidates,
            tank_range_mi=tank_range_mi,
            total_route_mi=route.total_route_mi,
            mpg=mpg if strengthened_prune else None,
            penalty=PENALTY if strengthened_prune else None,
        )
        result.kept = len(search_set)

        estimate = dp.estimate_transition_count(
            search_set,
            total_route_mi=route.total_route_mi,
            tank_range_mi=tank_range_mi,
            starting_fuel=starting_fuel,
        )
        result.estimate = estimate
        result.admitted_at_current_budget = estimate <= dp.DP_TRANSITION_BUDGET
        for rung in DP_TRANSITION_BUDGET_LADDER:
            label = "None" if rung is None else str(rung)
            result.admitted_by_rung[label] = (rung is None) or (estimate <= rung)

        # `penalty=PENALTY` and `trust_margin=Decimal(0)` are passed as
        # explicit literal keywords at every solve() call site below
        # (never bundled into a **kwargs dict) so `SolvePenaltyKwargGateTest`
        # and `SolveTrustMarginKwargGateTest`'s AST gates (PROV-03,
        # `routing/tests/test_boundaries.py`) can see them statically.
        # `Decimal(0)` keeps this D-22 baseline command's own behaviour
        # provably unchanged -- it measures dispatch admission, not the
        # trust margin's effect.
        #
        # [AMENDED 2026-08-13, Phase 24] `trust_margin` is now passed as a
        # variable reference (`trust_margin=trust_margin`, the method's own
        # new parameter) at both solve() call sites below, not the literal
        # `Decimal(0)` the paragraph above describes -- that sentence is
        # therefore no longer literally true of the parameterized path.
        # This stays gate-safe: `SolveTrustMarginKwargGateTest` walks the
        # AST checking only that a `trust_margin=` keyword is present at
        # each solve() call, never that its value is a literal, so a
        # variable reference satisfies it exactly as the literal did. This
        # command's own behaviour still stays unmoved -- the parameter
        # defaults to `Decimal(0)` and the sole caller at
        # `results = [self._measure_cell(row, repeats) for row in grid]`
        # passes no `trust_margin`, so every figure this command pins is
        # unchanged.
        solve_kwargs = dict(
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )

        # [Amended 2026-08-17, Phase 25] Which candidate list solve()
        # searches, and whether it is asked to prune that list itself,
        # both now depend on `strengthened_prune`. False (the only branch
        # `handle()`'s own call ever takes): `solve_candidates` is the
        # FULL `raw_candidates` and `solve_prune` is `True` -- solve()
        # receives no explicit `prune=` difference from before this
        # amendment and runs its own internal, unstrengthened
        # `prune_dominated_candidates` call (`solver.py`'s own
        # `mpg=`/`penalty=None`, always -- `PruneInertnessGateTest`'s
        # subject). True: `solve_candidates` is the already-strengthened-
        # pruned `search_set` computed above and `solve_prune` is `False`,
        # so the DP searches exactly that reduced set without `solve()`
        # itself ever supplying `mpg=`/`penalty=` to the prune -- the
        # strengthened rule is reached entirely through this measurement
        # seam, never through `solve()` (D-14).
        #
        # Fidelity note: in production, `solve()` runs
        # `dp.preflight_gap_check` over the UNPRUNED candidate list, then
        # prunes afterwards (see `solve()`'s own docstring, step (2)
        # before step (3)). Passing an already-pruned `search_set` with
        # `prune=False` instead runs that same check over the PRUNED list
        # -- the one genuine fidelity deviation this measurement seam
        # introduces relative to a real strengthened-rule deployment.
        # `prune.py`'s D-05 structural reach-safety ("Reach-safety (D-05)
        # restated for this condition") is what makes the two equivalent:
        # removal never manufactures a new infeasibility, so a gap
        # invisible in the pruned list was never a real gap in the
        # unpruned one either. A divergence here -- an
        # `InfeasibleRouteError` on the after world for a cell the before
        # world solves -- is itself a finding, evidence of an unsound
        # removal, not a measurement artifact to route around; such a
        # cell must be CENSORED with an explicit reason (the
        # `result.censored`/`result.censored_reason` branches below
        # already carry one) rather than silently dropped from the
        # report.
        solve_candidates = search_set if strengthened_prune else raw_candidates
        solve_prune = not strengthened_prune

        # Untimed column (deadline=None), worst-of-`repeats` -- the input
        # to D-17's largest-offline-solve-time live-probe selection rule.
        worst_untimed = Decimal(0)
        untimed_plan = None
        try:
            for _ in range(repeats):
                started = time.perf_counter()
                plan = solve(
                    solve_candidates,
                    route.total_route_mi,
                    deadline=None,
                    penalty=PENALTY,
                    trust_margin=trust_margin,
                    prune=solve_prune,
                    **solve_kwargs,
                )
                elapsed = Decimal(str(time.perf_counter() - started))
                if elapsed > worst_untimed:
                    worst_untimed = elapsed
                untimed_plan = plan
        except InfeasibleRouteError as exc:
            result.censored = True
            result.censored_reason = f"InfeasibleRouteError (untimed): {exc}"
            return result

        result.untimed_solve_seconds = worst_untimed
        result.untimed_strategy = untimed_plan.strategy
        result.stops = len(untimed_plan.stops)
        result.total_cost = untimed_plan.total_cost
        result.stop_opis_ids = tuple(stop.opis_id for stop in untimed_plan.stops)
        if row["is_demo_cell"]:
            result.demo_stop_detail = [
                (stop.name, stop.gallons, stop.purchase_reason)
                for stop in untimed_plan.stops
            ]

        # Timed column: a single solve() call inheriting the production
        # deadline (dp.DP_DEADLINE_SECONDS, solve()'s own default) -- what
        # a live request would actually get.
        try:
            started = time.perf_counter()
            timed_plan = solve(
                solve_candidates,
                route.total_route_mi,
                penalty=PENALTY,
                trust_margin=trust_margin,
                prune=solve_prune,
                **solve_kwargs,
            )
            elapsed = Decimal(str(time.perf_counter() - started))
        except InfeasibleRouteError as exc:
            result.censored = True
            result.censored_reason = f"InfeasibleRouteError (timed): {exc}"
            return result

        result.timed_strategy = timed_plan.strategy
        result.timed_solve_seconds = elapsed
        result.breached = timed_plan.deadline_breached

        return result

    def _print_table(self, results):
        self.stdout.write(
            self.style.SUCCESS(
                "PER-CELL TABLE -- 24 corridor cells at "
                "ADMISSION_MANIFEST_VEHICLE plus 2 demo cells at "
                "DEMO_CHIP_VEHICLE:"
            )
        )
        for r in results:
            label = "demo" if r.is_demo_cell else "corridor"
            if r.censored:
                self.stdout.write(
                    f"    {r.slug} @{r.tank_range_mi}mi [{label}]: "
                    f"CENSORED -- {r.censored_reason}"
                )
                continue
            self.stdout.write(
                f"    {r.slug} @{r.tank_range_mi}mi [{label}]: "
                f"raw={r.raw_candidates} kept={r.kept} "
                f"estimate={r.estimate} "
                f"admitted_current={r.admitted_at_current_budget} "
                f"untimed_strategy={r.untimed_strategy} "
                f"untimed_seconds={r.untimed_solve_seconds:.4f} "
                f"timed_strategy={r.timed_strategy} "
                f"timed_seconds={r.timed_solve_seconds:.4f} "
                f"breached={r.breached} stops={r.stops} "
                f"total_cost={r.total_cost}"
            )
            for name, gallons, reason in r.demo_stop_detail:
                self.stdout.write(
                    f"        stop: {name} gallons={gallons} reason={reason}"
                )
        self.stdout.write("")

    def _print_summary(self, results):
        measured = [r for r in results if not r.censored]
        demoted = [r for r in measured if not r.admitted_at_current_budget]
        corridors_affected = sorted({r.slug for r in demoted})
        total = len(measured)
        count = len(demoted)
        percent = (count / total * 100) if total else 0.0

        self.stdout.write(
            self.style.SUCCESS(
                "SUMMARY -- demoted-cell count at the CURRENT "
                f"dp.DP_TRANSITION_BUDGET={dp.DP_TRANSITION_BUDGET}:"
            )
        )
        self.stdout.write(
            f"    {count} of {total} measured cells ({percent:.1f}%) "
            f"dispatch to the heuristic, spanning "
            f"{len(corridors_affected)} corridor(s): "
            f"{', '.join(corridors_affected) if corridors_affected else '(none)'}"
        )
        self.stdout.write(
            self.style.WARNING(
                f"    SUPERSEDED BY this measurement, NOT replaced-and-"
                f"deleted -- the historical figure from "
                f"non-scalar-dispatch-rule.md: {_HISTORICAL_DEMOTED_COUNT} "
                f"of {_HISTORICAL_TOTAL_COUNT} cells "
                f"({_HISTORICAL_DEMOTED_PERCENT}%), spanning "
                f"{_HISTORICAL_CORRIDOR_COUNT} of "
                f"{_HISTORICAL_CORRIDOR_TOTAL} corridors. The two figures "
                "are recorded side by side and deliberately not "
                "reconciled."
            )
        )
        self.stdout.write("")

    def _print_rung_table(self, results):
        self.stdout.write(
            self.style.SUCCESS(
                "PER-RUNG ADMISSION TABLE -- cells newly admitted at each "
                "DP_TRANSITION_BUDGET_LADDER rung, relative to the "
                f"current budget ({dp.DP_TRANSITION_BUDGET}), with their "
                "measured untimed solve times:"
            )
        )
        measured = [r for r in results if not r.censored]
        lower_bound = DP_TRANSITION_BUDGET_LADDER[0]
        for rung in DP_TRANSITION_BUDGET_LADDER[1:]:
            newly = [
                r
                for r in measured
                if r.estimate > lower_bound and (rung is None or r.estimate <= rung)
            ]
            label = "None (no gate)" if rung is None else str(rung)
            self.stdout.write(f"    rung={label}:")
            if not newly:
                self.stdout.write("        (nothing newly admitted)")
            for r in sorted(newly, key=lambda row: row.estimate):
                self.stdout.write(
                    f"        {r.slug} @{r.tank_range_mi}mi "
                    f"estimate={r.estimate} "
                    f"untimed_seconds={r.untimed_solve_seconds:.4f} "
                    f"untimed_strategy={r.untimed_strategy}"
                )
            if rung is not None:
                lower_bound = rung
        self.stdout.write("")

    def _print_verdict(self, results):
        self.stdout.write(
            self.style.SUCCESS(
                "VERDICT -- adopt_budget_rung applied exactly as pinned "
                "in routing.tests.test_dispatch_recovery, before this "
                "command's own numbers existed. worst_timed_response_"
                "seconds is approximated by this command's own single-"
                "request solve()-only timed_solve_seconds (this command "
                "makes no live request); the true end-to-end response-"
                "time figure is plan 09's live spot-check, which this "
                "offline verdict does not substitute for."
            )
        )
        self.stdout.write(f"    RESPONSE_BAR_SECONDS={RESPONSE_BAR_SECONDS}")

        verdict_rows = [
            {
                "slug": r.slug,
                "tank_range_mi": r.tank_range_mi,
                "estimate": r.estimate,
                "worst_timed_response_seconds": r.timed_solve_seconds,
                "breached": r.breached,
            }
            for r in results
            if not r.censored
        ]
        adopted_rung = adopt_budget_rung(verdict_rows)
        label = "None (no gate)" if adopted_rung is None else str(adopted_rung)
        self.stdout.write(
            f"    adopted rung: {label} (current "
            f"dp.DP_TRANSITION_BUDGET={dp.DP_TRANSITION_BUDGET})"
        )
        self.stdout.write(
            "    Applying this offline verdict to production is plan "
            "08's job, not this command's -- this command only prints."
        )
        self.stdout.write("")
        return adopted_rung

    def _print_probe_selection(self, results, adopted_rung):
        self.stdout.write(
            self.style.SUCCESS(
                "LIVE-PROBE SELECTION -- select_live_probe_cells applied "
                "exactly as pinned in routing.tests.test_dispatch_"
                f"recovery (LIVE_PROBE_MAX_CELLS={LIVE_PROBE_MAX_CELLS}). "
                "No cell was added or removed by hand:"
            )
        )
        probe_rows = [
            {
                "slug": r.slug,
                "tank_range_mi": r.tank_range_mi,
                "estimate": r.estimate,
                "offline_untimed_solve_seconds": r.untimed_solve_seconds,
                "is_demo_cell": r.is_demo_cell,
            }
            for r in results
            if not r.censored
        ]
        probe_cells = select_live_probe_cells(probe_rows, adopted_rung)
        for slug, tank_range_mi in probe_cells:
            self.stdout.write(f"    {slug} @{tank_range_mi}mi")
        self.stdout.write("")

    def _print_disclaimer(self):
        self.stdout.write(
            "This command measures offline dp.estimate_transition_count "
            "admission and solve()-only wall-clock time on a developer "
            "workstation, over committed corridor and demo-chip geometry, "
            "at the three named (never conflated) vehicles above. It "
            "makes NO claim about deployed-hardware time or true "
            "end-to-end response time -- that is plan 09's live "
            "spot-check -- and no threshold may be adopted from this "
            "command's output alone."
        )

"""Budget-enforcing live latency probe against the DEPLOYED TankWise
instance -- the single measurement this phase's whole gap turns on: how
long the solver actually takes on deployed hardware, per cell, read from
the live instance's own `Server-Timing` header.

MUST NOT run in CI. This module defines no URL, cell, budget, timeout or
cache-bust ladder of its own -- every one of those values is imported from
`routing.tests.test_live_latency_probe`, the single shared source of truth
pinned before this command's own measurement ever ran (see that module's
docstring for the full derivation).

This command makes REAL outbound network requests against a LIVE
PRODUCTION service and spends real Mapbox quota and a real slice of the
per-IP `ROUTE_THROTTLE_SUSTAINED_RATE=200/day` throttle -- exactly why its
budget is enforced in code (`LIVE_PROBE_MAX_REQUESTS`, checked BEFORE a
single request is issued) rather than trusted to whoever runs it.

Two modes, one shared protocol (wake, then measure, never print a response
body):

  - Default: sweep `LIVE_PROBE_CELLS`, `LIVE_PROBE_REPEATS` times each,
    busting the cache per repeat via `LIVE_PROBE_CACHE_BUST_LADDER`.
  - `--anomaly`: reproduce the exact 422-after-55s request
    (`ANOMALY_REQUEST`) and apply the falsification branch pinned in
    `test_live_latency_probe.py`'s module docstring, printing exactly one
    of two verdicts (CONFIRMED / REFUTED -- UNEXPLAINED).

Response bodies never reach stdout: a `/api/route` response carries the
Mapbox `pk.` token in `map_url` (the same hygiene rule
`.github/workflows/keep-warm.yml` already establishes for its own warm
POSTs) -- only status, wall time and parsed `Server-Timing` stage
durations are ever printed.

**Scope disclaimer, stated plainly.** This command measures ONE deployed
instance on ONE plan tier at ONE moment, on cells the CURRENT dispatch
boundary actually routes to the DP. Cells the current boundary demotes to
the penalty-aware heuristic cannot have their exact-DP LIVE time measured
this way at all -- there is no live request that would exercise that code
path on deployed hardware for those cells under the current dispatch. Those
are reported as unmeasurable-by-this-probe, with the already-recorded
censored evidence (the pre-hotfix HTTP 500s, `18-VERIFICATION.md`) cited
instead of ever being estimated.
"""
import time
from dataclasses import dataclass, field
from decimal import Decimal

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.tests.test_live_latency_probe import (
    ANOMALY_FALSIFICATION_THRESHOLD_SECONDS,
    ANOMALY_REQUEST,
    ANOMALY_SOLVER_STAGE_MAX_MS,
    LIVE_PROBE_BASE_URL,
    LIVE_PROBE_CACHE_BUST_LADDER,
    LIVE_PROBE_CELLS,
    LIVE_PROBE_INTER_REQUEST_SECONDS,
    LIVE_PROBE_MAX_REQUESTS,
    LIVE_PROBE_REPEATS,
    LIVE_PROBE_REQUEST_TIMEOUT_SECONDS,
    LIVE_PROBE_WAKE_TIMEOUT_SECONDS,
)


def parse_server_timing(header_value):
    """Parse a `name;dur=NNN.N, name2;dur=NNN.N` `Server-Timing` header
    (the exact format `routing/timing.py`'s `ServerTiming.header_value()`
    emits) into `{name: duration_ms}`. Returns `{}` for a missing or empty
    header. Never raises on a malformed segment -- skips it instead, since
    a probe parsing a header from a live, uncontrolled service must not
    crash the whole sweep on one odd response."""
    stages = {}
    if not header_value:
        return stages
    for part in header_value.split(","):
        part = part.strip()
        if not part or ";dur=" not in part:
            continue
        name, _, dur = part.partition(";dur=")
        name = name.strip()
        try:
            stages[name] = float(dur.strip())
        except ValueError:
            continue
    return stages


@dataclass
class ProbeRow:
    """One measured (or censored, or cache-hit) attempt against a single
    cell/repeat. `attempts_made` counts every HTTP request this row's own
    repeat consumed (1, or 2 if the single permitted cache-hit retry
    fired) -- used only for the printed request-accounting total, never
    for the pinned budget check itself (that check is computed from the
    MATRIX before any request is issued, per the module docstring)."""

    cell_label: str
    repeat_index: int
    ladder_value: Decimal
    status_code: int | None
    wall_time_s: float
    stages_ms: dict = field(default_factory=dict)
    censored: bool = False
    cache_hit: bool = False
    solver_strategy: str | None = None
    error: str | None = None
    attempts_made: int = 1

    @property
    def is_genuine_miss(self):
        return (not self.censored) and (not self.cache_hit) and ("solver" in self.stages_ms)


class Command(BaseCommand):
    help = (
        "Measure deployed-hardware solver latency by issuing REAL requests "
        "against the LIVE TankWise instance (LIVE_PROBE_BASE_URL). Every "
        "constant -- matrix, budget, timeouts, cache-bust ladder -- comes "
        "from routing.tests.test_live_latency_probe. MUST NOT run in CI: "
        "it spends real Mapbox quota and a real slice of the deployed "
        "per-IP request throttle."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--anomaly",
            action="store_true",
            help=(
                "Reproduce the pinned 422-after-55s anomaly request "
                "instead of the main cell sweep, and print the "
                "CONFIRMED / REFUTED-UNEXPLAINED verdict."
            ),
        )

    def handle(self, *args, **options):
        if options["anomaly"]:
            self._run_anomaly()
        else:
            self._run_sweep()

    # --- shared plumbing -----------------------------------------------

    def _enforce_budget(self, implied_count, context):
        if implied_count > LIVE_PROBE_MAX_REQUESTS:
            raise CommandError(
                f"Refusing to run ({context}): implied request count "
                f"{implied_count} exceeds LIVE_PROBE_MAX_REQUESTS="
                f"{LIVE_PROBE_MAX_REQUESTS}. This is a planning error the "
                "operator must resolve -- narrow the matrix or "
                "deliberately raise the pinned budget in "
                "test_live_latency_probe.py. This command does not "
                "silently trim the matrix to fit."
            )

    def _wake(self):
        url = f"{LIVE_PROBE_BASE_URL}/api/health"
        started = time.perf_counter()
        try:
            response = requests.get(url, timeout=LIVE_PROBE_WAKE_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise CommandError(
                f"Wake request to {url} failed: {exc!r}. Aborting -- no "
                "measurement below can be trusted without a confirmed "
                "wake (an unwaked cold boot would otherwise be charged "
                "to the solver)."
            )
        elapsed = time.perf_counter() - started
        self.stdout.write(
            f"WAKE: status={response.status_code} elapsed={elapsed:.2f}s "
            f"(budget={LIVE_PROBE_WAKE_TIMEOUT_SECONDS}s)"
        )
        if response.status_code != 200:
            raise CommandError(
                f"Wake returned non-200 status {response.status_code}. "
                "Aborting -- cannot proceed without a confirmed-warm "
                "service."
            )
        return elapsed

    def _post_route(self, start, finish, vehicle):
        url = f"{LIVE_PROBE_BASE_URL}/api/route"
        body = {"start": start, "finish": finish, "vehicle": vehicle}
        started = time.perf_counter()
        try:
            response = requests.post(
                url, json=body, timeout=LIVE_PROBE_REQUEST_TIMEOUT_SECONDS
            )
        except requests.Timeout:
            elapsed = time.perf_counter() - started
            return None, elapsed, {}, None, "client_timeout"
        except requests.RequestException as exc:
            elapsed = time.perf_counter() - started
            return None, elapsed, {}, None, repr(exc)

        elapsed = time.perf_counter() - started
        stages = parse_server_timing(response.headers.get("Server-Timing", ""))
        solver_strategy = None
        if response.status_code == 200:
            try:
                solver_strategy = response.json().get("solver_strategy")
            except ValueError:
                solver_strategy = None
        return response.status_code, elapsed, stages, solver_strategy, None

    # --- main sweep -------------------------------------------------------

    def _run_sweep(self):
        ladder_rungs = len(LIVE_PROBE_CACHE_BUST_LADDER)
        implied_count = len(LIVE_PROBE_CELLS) * LIVE_PROBE_REPEATS * ladder_rungs + 1
        self._enforce_budget(implied_count, "main sweep")
        self.stdout.write(
            f"Budget check: implied worst-case requests={implied_count} "
            f"<= LIVE_PROBE_MAX_REQUESTS={LIVE_PROBE_MAX_REQUESTS}. "
            f"Proceeding against {LIVE_PROBE_BASE_URL}."
        )
        self.stdout.write("")

        workstation = self._measure_workstation_baseline()

        self._wake()
        requests_issued = 1

        rows = []
        for cell in LIVE_PROBE_CELLS:
            for repeat_index in range(LIVE_PROBE_REPEATS):
                row, attempts = self._measure_one_repeat(cell, repeat_index, ladder_rungs)
                requests_issued += attempts
                rows.append(row)

        self._print_table(rows)
        self._print_factor_summary(rows, workstation)
        self._print_request_accounting(requests_issued, implied_count)
        self._print_disclaimer()

    def _measure_one_repeat(self, cell, repeat_index, ladder_rungs):
        """Issue at most two attempts for one (cell, repeat): the primary
        attempt at `LIVE_PROBE_CACHE_BUST_LADDER[repeat_index]`, and -- if
        and only if that attempt is a cache hit -- exactly ONE retry at
        the next ladder rung. Never more than one retry, per the module
        docstring's own rule; a still-cache-hit second attempt is
        recorded as a cache-hit row, never re-retried."""
        attempts = 0
        row = None
        for attempt_num in range(2):
            rung_index = repeat_index if attempt_num == 0 else repeat_index + 1
            if rung_index >= ladder_rungs:
                break
            time.sleep(LIVE_PROBE_INTER_REQUEST_SECONDS)
            ladder_value = LIVE_PROBE_CACHE_BUST_LADDER[rung_index]
            vehicle = dict(cell["vehicle"])
            vehicle["starting_fuel"] = str(ladder_value)
            status, elapsed, stages, strategy, error = self._post_route(
                cell["start"], cell["finish"], vehicle
            )
            attempts += 1

            censored = error is not None or (
                status is not None and not (200 <= status < 300)
            )
            cache_hit = (
                (not censored) and ("solver" not in stages) and ("cache" in stages)
            )
            row = ProbeRow(
                cell_label=cell["label"],
                repeat_index=repeat_index,
                ladder_value=ladder_value,
                status_code=status,
                wall_time_s=elapsed,
                stages_ms=stages,
                censored=censored,
                cache_hit=cache_hit,
                solver_strategy=strategy,
                error=error,
                attempts_made=attempts,
            )
            self.stdout.write(
                f"    {cell['label']} repeat={repeat_index} "
                f"attempt={attempt_num} starting_fuel={ladder_value}: "
                f"status={status} elapsed={elapsed:.2f}s stages_ms={stages} "
                f"cache_hit={cache_hit} censored={censored} "
                f"strategy={strategy} error={error}"
            )
            if not cache_hit:
                break
        return row, attempts

    def _measure_workstation_baseline(self):
        """Fresh, in-process, offline workstation baseline over the EXACT
        SAME (cell, vehicle) pairs the live sweep below measures -- see
        `test_live_latency_probe.py`'s module docstring, "Why the
        workstation baseline is measured fresh, not cited verbatim", for
        why this is not simply read off `dp.py`'s own calibration
        comment. Costs no network call and no live-service budget.

        `solver.solve()` below passes `deadline=None` (plan 18.1-05's
        call-site audit): this baseline must report the DP's true,
        untimed solve time so the workstation-to-live transfer factor
        this command derives compares like with like -- a workstation
        run capped at the production deadline would silently understate
        how much slower the deployed hardware really is on a cell whose
        untimed time exceeds that cap.
        """
        import io

        from django.core.management import call_command

        from routing.services import corridor, solver
        from routing.tests.test_corridor_fixtures import (
            factor_lookup_for_basis,
            load_corridor_route,
        )

        self.stdout.write(
            "Measuring a fresh workstation baseline (offline, same "
            "cell/vehicle pairs as the live sweep below)..."
        )
        call_command("seed_stations", stdout=io.StringIO())
        corridor.reset_index()
        factor_for = factor_lookup_for_basis("neutral")

        baseline = {}
        for cell in LIVE_PROBE_CELLS:
            route = load_corridor_route(cell["slug"])
            candidates = corridor.candidates(route, factor_for=factor_for)
            vehicle = cell["vehicle"]
            times_s = []
            strategy = None
            for _ in range(LIVE_PROBE_REPEATS):
                started = time.perf_counter()
                plan = solver.solve(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=Decimal(vehicle["tank_range_mi"]),
                    mpg=Decimal(vehicle["mpg"]),
                    starting_fuel=Decimal(vehicle["starting_fuel"]),
                    penalty=settings.FUEL_STOP_PENALTY_USD,
                    deadline=None,  # D-05: untimed -- the workstation baseline must report the DP's true solve time so the workstation-to-live transfer factor compares like with like
                )
                times_s.append(time.perf_counter() - started)
                strategy = plan.strategy
            worst_s = max(times_s)
            baseline[cell["label"]] = {"worst_s": worst_s, "strategy": strategy}
            self.stdout.write(
                f"    workstation {cell['label']}: worst_of_{LIVE_PROBE_REPEATS}"
                f"={worst_s:.4f}s strategy={strategy}"
            )
        self.stdout.write("")
        return baseline

    def _print_table(self, rows):
        self.stdout.write(self.style.SUCCESS("PER-CELL LIVE TABLE:"))
        by_cell = {}
        for row in rows:
            by_cell.setdefault(row.cell_label, []).append(row)
        for label, cell_rows in by_cell.items():
            self.stdout.write(f"    {label}:")
            for row in cell_rows:
                solver_ms = row.stages_ms.get("solver")
                corridor_ms = row.stages_ms.get("corridor")
                route_ms = row.stages_ms.get("route")
                index_ms = row.stages_ms.get("index")
                total_ms = row.stages_ms.get("total")
                marker = (
                    "CENSORED"
                    if row.censored
                    else "CACHE-HIT (no latency figure)"
                    if row.cache_hit
                    else "GENUINE MISS"
                )
                self.stdout.write(
                    f"        repeat={row.repeat_index} status={row.status_code} "
                    f"wall={row.wall_time_s:.2f}s solver_stage_ms={solver_ms} "
                    f"corridor_stage_ms={corridor_ms} route_stage_ms={route_ms} "
                    f"index_stage_ms={index_ms} total_stage_ms={total_ms} "
                    f"strategy={row.solver_strategy} -- {marker}"
                )
        self.stdout.write("")

    def _print_factor_summary(self, rows, workstation):
        self.stdout.write(
            self.style.SUCCESS(
                "DEPLOYED-HARDWARE solver STAGE, worst-of-repeats (genuine "
                "misses only) AND WORKSTATION-TO-LIVE FACTOR PER CELL:"
            )
        )
        by_cell = {}
        for row in rows:
            by_cell.setdefault(row.cell_label, []).append(row)

        factors = []
        for label, cell_rows in by_cell.items():
            genuine = [r for r in cell_rows if r.is_genuine_miss]
            if not genuine:
                self.stdout.write(
                    f"    {label}: NO GENUINE CACHE-MISS ACHIEVED -- this "
                    "cell could not be measured for solver stage this run "
                    "(reported as such, not counted as a fast solve)."
                )
                continue
            worst_solver_s = max(r.stages_ms["solver"] for r in genuine) / 1000.0
            workstation_worst_s = workstation.get(label, {}).get("worst_s")
            if workstation_worst_s and workstation_worst_s > 0:
                factor = worst_solver_s / workstation_worst_s
                factors.append(factor)
                self.stdout.write(
                    f"    {label}: live_solver_worst={worst_solver_s:.4f}s "
                    f"workstation_worst={workstation_worst_s:.4f}s "
                    f"factor={factor:.2f}x"
                )
            else:
                self.stdout.write(
                    f"    {label}: live_solver_worst={worst_solver_s:.4f}s "
                    "(no workstation baseline to compare against)"
                )

        if factors:
            self.stdout.write(
                f"    SPREAD across cells: min={min(factors):.2f}x "
                f"max={max(factors):.2f}x. NOT collapsed into a single "
                "averaged multiplier -- whether one factor exists at all "
                "is exactly the open question; see this module's own "
                "docstring."
            )
        self.stdout.write("")

    def _print_request_accounting(self, requests_issued, implied_count):
        self.stdout.write(
            f"Total live requests issued this run: {requests_issued} "
            f"(worst-case implied budget was {implied_count}, pinned "
            f"ceiling LIVE_PROBE_MAX_REQUESTS={LIVE_PROBE_MAX_REQUESTS})."
        )
        self.stdout.write("")

    def _print_disclaimer(self):
        self.stdout.write(
            "This command measures ONE deployed instance on ONE plan tier "
            "at ONE moment, on cells the CURRENT dispatch boundary "
            "actually routes to the DP. Cells the current boundary "
            "demotes to the penalty-aware heuristic cannot have their "
            "exact-DP LIVE time measured this way at all -- see this "
            "module's own docstring's closing scope disclaimer. Plan "
            "18-12 must not adopt a dispatch threshold from this "
            "command's output alone."
        )

    # --- anomaly reproduction -----------------------------------------

    def _run_anomaly(self):
        implied_count = 2  # wake + one POST
        self._enforce_budget(implied_count, "anomaly reproduction")

        self._wake()
        time.sleep(LIVE_PROBE_INTER_REQUEST_SECONDS)
        status, elapsed, stages, strategy, error = self._post_route(
            ANOMALY_REQUEST["start"],
            ANOMALY_REQUEST["finish"],
            ANOMALY_REQUEST["vehicle"],
        )
        solver_ms = stages.get("solver")

        self.stdout.write(self.style.SUCCESS("ANOMALY REPRODUCTION:"))
        self.stdout.write(
            f"    request={ANOMALY_REQUEST} status={status} "
            f"wall_time_s={elapsed:.2f} stages_ms={stages} "
            f"solver_strategy={strategy} error={error}"
        )

        is_422 = status == 422
        within_threshold = elapsed <= ANOMALY_FALSIFICATION_THRESHOLD_SECONDS
        solver_not_substantial = (
            solver_ms is None or solver_ms <= ANOMALY_SOLVER_STAGE_MAX_MS
        )

        if is_422 and within_threshold and solver_not_substantial:
            verdict = "CONFIRMED"
            self.stdout.write(
                self.style.SUCCESS(
                    f"VERDICT: CONFIRMED -- 422 in {elapsed:.2f}s "
                    f"(<= {ANOMALY_FALSIFICATION_THRESHOLD_SECONDS}s "
                    f"threshold) with solver_stage_ms={solver_ms} "
                    f"(<= {ANOMALY_SOLVER_STAGE_MAX_MS}ms). The 55s "
                    "figure was cold-boot time plus a correct, cheap "
                    "preflight infeasibility rejection; gunicorn's "
                    "per-request timeout never applied to the boot."
                )
            )
        else:
            verdict = "REFUTED -- UNEXPLAINED"
            self.stdout.write(
                self.style.WARNING(
                    f"VERDICT: REFUTED -- UNEXPLAINED. status={status} "
                    f"(422 expected: {is_422}), elapsed={elapsed:.2f}s "
                    f"(<= {ANOMALY_FALSIFICATION_THRESHOLD_SECONDS}s: "
                    f"{within_threshold}), solver_stage_ms={solver_ms} "
                    f"(<= {ANOMALY_SOLVER_STAGE_MAX_MS}ms: "
                    f"{solver_not_substantial}). The hypothesis does not "
                    "hold against this measurement. The anomaly is "
                    "recorded as UNEXPLAINED with these figures -- no "
                    "replacement hypothesis is invented here, and no "
                    "constant above is adjusted to make it fit."
                )
            )
        self.stdout.write("")
        return verdict, status, elapsed, stages

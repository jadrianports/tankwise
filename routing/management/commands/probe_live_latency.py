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

Four modes, one shared protocol (wake -- now confirming the deployed
commit SHA before trusting anything below it, D-12 fact 1 -- then
measure, never print a response body):

  - Default: sweep `LIVE_PROBE_CELLS`, `LIVE_PROBE_REPEATS` times each,
    busting the cache per repeat via `LIVE_PROBE_CACHE_BUST_LADDER`.
  - `--anomaly`: reproduce the exact 422-after-55s request
    (`ANOMALY_REQUEST`) and apply the falsification branch pinned in
    `test_live_latency_probe.py`'s module docstring, printing exactly one
    of two verdicts (CONFIRMED / REFUTED -- UNEXPLAINED).
  - `--recovery`: sweep `RECOVERY_PROBE_CELLS` and apply D-18's
    measurement floor and `recovery_verdict`.
  - `--verdict` (Phase 26 D-11/D-12/D-13): sweep the full 26-cell
    `VERDICT_PROBE_CELLS` matrix, printing each cell's pinned OFFLINE
    admission beside its LIVE observed arm (never reconciled), then the
    same D-18-style measurement floor and headline verdict.

`--expect-commit <sha>` overrides the default expected commit (the local
`git rev-parse HEAD`) for every mode -- the wake step now hard-fails,
naming both SHAs, if the deployed build does not match. An unconfirmable
build (missing/null `commit` field) is treated the same way: no
measurement below it may be trusted.

Response bodies never reach stdout: a `/api/route` response carries the
Mapbox `pk.` token in `map_url` (the same hygiene rule
`.github/workflows/keep-warm.yml` already establishes for its own warm
POSTs) -- only status, wall time and parsed `Server-Timing` stage
durations (plus, since Phase 26, `price_index_status`/stop count/
`total_cost`, all named fields, never the raw body) are ever printed.

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
import subprocess
import time
from dataclasses import dataclass, field
from decimal import Decimal

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.services.solver import SolverStrategy
from routing.tests.test_dispatch_recovery import (
    RESPONSE_BAR_SECONDS,
    measurement_floor_violations,
    recovery_verdict,
)
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
    RECOVERY_PROBE_CELLS,
    VERDICT_PROBE_CACHE_BUST_FRACTIONS,
    VERDICT_PROBE_CELLS,
    VERDICT_PROBE_MAX_REQUESTS,
    VERDICT_PROBE_REPEATS,
    VERDICT_PROBE_WORST_CASE_ATTEMPTS_PER_REPEAT,
    relative_cache_bust_starting_fuel,
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
    # True when this repeat's `Server-Timing` header carried a
    # `dp_deadline_breach` entry (`routing/timing.py`'s `ServerTiming.
    # record()`, surfaced by plan 18.1-06) -- read from the SAME parsed
    # `stages_ms` dict every other stage duration is read from, never from
    # a second request or the response body.
    breach: bool = False
    # --- Phase 26 D-12/D-14 additions -----------------------------------
    # price_index_status/stop_count/total_cost are read from the SAME 200
    # response body `_post_route` already parses for `solver_strategy`,
    # defensively -- `None` on any malformed/non-dict body, never a raise.
    # slug/tank_range_mi/offline_admitted are copied straight off the
    # cell dict a row was measured for, so a verdict reduction can read a
    # row's own cell identity directly rather than re-deriving it from
    # `cell_label` via a second lookup.
    price_index_status: str | None = None
    stop_count: int | None = None
    total_cost: str | None = None
    slug: str | None = None
    tank_range_mi: Decimal | None = None
    offline_admitted: bool | None = None

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
        parser.add_argument(
            "--recovery",
            action="store_true",
            help=(
                "Sweep RECOVERY_PROBE_CELLS (the D-17 rule-selected "
                "criterion-3 probe set) instead of LIVE_PROBE_CELLS, then "
                "print the D-18 measurement-floor violations and the "
                "recovery_verdict. Exits non-zero if any measurement-floor "
                "violation stands -- no verdict may be declared until it "
                "is cleared."
            ),
        )
        parser.add_argument(
            "--verdict",
            action="store_true",
            help=(
                "Sweep VERDICT_PROBE_CELLS (the full 26-cell D-11 dispatch "
                "verdict matrix) at VERDICT_PROBE_REPEATS repeats, print "
                "each cell's pinned OFFLINE admission beside its LIVE "
                "observed arm, then the D-12 measurement-floor violations "
                "and the headline verdict. Exits non-zero if any "
                "measurement-floor violation stands -- no verdict may be "
                "declared over a dirty floor."
            ),
        )
        parser.add_argument(
            "--expect-commit",
            default=None,
            help=(
                "The deployed commit SHA the wake step must confirm before "
                "any measurement below is trusted (D-12 fact 1). Defaults "
                "to the local `git rev-parse HEAD` when omitted."
            ),
        )

    def handle(self, *args, **options):
        self._expect_commit = options.get("expect_commit")
        modes_selected = sum(
            [bool(options["anomaly"]), bool(options["recovery"]), bool(options["verdict"])]
        )
        if modes_selected > 1:
            raise CommandError(
                "--anomaly, --recovery, and --verdict are mutually exclusive."
            )
        if options["anomaly"]:
            self._run_anomaly()
        elif options["recovery"]:
            self._run_recovery_sweep()
        elif options["verdict"]:
            self._run_verdict_sweep()
        else:
            self._run_sweep()

    # --- shared plumbing -----------------------------------------------

    def _enforce_budget(self, implied_count, context, ceiling=LIVE_PROBE_MAX_REQUESTS):
        if implied_count > ceiling:
            raise CommandError(
                f"Refusing to run ({context}): implied request count "
                f"{implied_count} exceeds the pinned ceiling={ceiling}. "
                "This is a planning error the operator must resolve -- "
                "narrow the matrix or deliberately raise the pinned "
                "budget in test_live_latency_probe.py. This command does "
                "not silently trim the matrix to fit."
            )

    def _local_head_sha(self):
        """Resolve the local `git rev-parse HEAD` SHA -- the default
        `--expect-commit` value when the operator does not supply one
        explicitly. Returns `None` (never raises) when git is unavailable
        or the command fails for any reason -- `_wake` treats a `None`
        here the same as a missing `--expect-commit`: an unconfirmable
        build, hard failure."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        sha = result.stdout.strip()
        return sha or None

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
        if response.status_code != 200:
            self.stdout.write(
                f"WAKE: status={response.status_code} elapsed={elapsed:.2f}s "
                f"(budget={LIVE_PROBE_WAKE_TIMEOUT_SECONDS}s)"
            )
            raise CommandError(
                f"Wake returned non-200 status {response.status_code}. "
                "Aborting -- cannot proceed without a confirmed-warm "
                "service."
            )

        # D-12 fact (1): confirm the deployed build is the build under
        # test BEFORE any measurement below is trusted. quick task
        # 260817-37n closed after production sat eight commits stale for
        # four days -- this is a hard failure, never a comment.
        expected_sha = getattr(self, "_expect_commit", None) or self._local_head_sha()
        if not expected_sha:
            raise CommandError(
                "Cannot confirm the deployed build: no --expect-commit "
                "was given and the local HEAD SHA could not be resolved "
                "(git unavailable?). Aborting -- D-12 fact (1) requires a "
                "confirmed build before any measurement is trusted."
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        deployed_sha = body.get("commit") if isinstance(body, dict) else None
        if not deployed_sha:
            raise CommandError(
                f"Deployed build's health response carries no 'commit' "
                f"field (body={body!r}). Aborting -- an unconfirmable "
                "build is not a confirmed one."
            )
        if deployed_sha != expected_sha:
            raise CommandError(
                f"Deployed commit {deployed_sha!r} does not match "
                f"expected {expected_sha!r} -- no measurement below can "
                "be trusted because the deployed build is not the build "
                "under test."
            )
        self._confirmed_commit_sha = deployed_sha

        self.stdout.write(
            f"WAKE: status={response.status_code} elapsed={elapsed:.2f}s "
            f"(budget={LIVE_PROBE_WAKE_TIMEOUT_SECONDS}s) commit={deployed_sha}"
        )
        return elapsed

    def _post_route(self, start, finish, vehicle, waypoints=()):
        url = f"{LIVE_PROBE_BASE_URL}/api/route"
        body = {"start": start, "finish": finish, "vehicle": vehicle}
        if waypoints:
            # Additive request field (routing/serializers.py's
            # RouteRequestSerializer) -- only present for a multi-leg
            # cell (RECOVERY_PROBE_CELLS' own demo_la_ca-denver_co-
            # chicago_il entry). LIVE_PROBE_CELLS' two entries carry no
            # "waypoints" key, so this stays () for them and the body
            # shape is byte-identical to before this addition.
            body["waypoints"] = list(waypoints)
        started = time.perf_counter()
        try:
            response = requests.post(
                url, json=body, timeout=LIVE_PROBE_REQUEST_TIMEOUT_SECONDS
            )
        except requests.Timeout:
            elapsed = time.perf_counter() - started
            return None, elapsed, {}, None, "client_timeout", None, None, None
        except requests.RequestException as exc:
            elapsed = time.perf_counter() - started
            return None, elapsed, {}, None, repr(exc), None, None, None

        elapsed = time.perf_counter() - started
        stages = parse_server_timing(response.headers.get("Server-Timing", ""))
        solver_strategy = None
        price_index_status = None
        stop_count = None
        total_cost = None
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                solver_strategy = data.get("solver_strategy")
                price_index_status = data.get("price_index_status")
                total_cost = data.get("total_cost")
                fuel_stops = data.get("fuel_stops")
                if isinstance(fuel_stops, list):
                    stop_count = len(fuel_stops)
        # Never printed: a 200 body carries the Mapbox `pk.` token in
        # `map_url` -- only the named fields above are ever extracted.
        return (
            response.status_code,
            elapsed,
            stages,
            solver_strategy,
            None,
            price_index_status,
            stop_count,
            total_cost,
        )

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

    def _measure_one_repeat(self, cell, repeat_index, ladder_rungs, *, ladder=LIVE_PROBE_CACHE_BUST_LADDER):
        """Issue at most two attempts for one (cell, repeat): the primary
        attempt at `ladder[repeat_index]`, and -- if and only if that
        attempt is a cache hit -- exactly ONE retry at the next ladder
        rung. Never more than one retry, per the module docstring's own
        rule; a still-cache-hit second attempt is recorded as a cache-hit
        row, never re-retried.

        `ladder` defaults to the legacy absolute `LIVE_PROBE_CACHE_BUST_
        LADDER`, so both legacy sweeps' call sites (`_run_sweep`,
        `_run_recovery_sweep`) keep working with no argument change.

        Dual interpretation of `ladder[rung_index]`, keyed on the CELL,
        never on a mode flag (RESEARCH.md Pitfall 2): when `cell` carries
        `base_starting_fuel`, the rung is treated as a FRACTION fed into
        `relative_cache_bust_starting_fuel(base, rung)`, perturbing
        around that cell's own pinned vehicle -- the shape
        `VERDICT_PROBE_CACHE_BUST_FRACTIONS` needs. When `cell` carries
        no `base_starting_fuel` (every `LIVE_PROBE_CELLS`/
        `RECOVERY_PROBE_CELLS` entry), the rung is used directly as the
        absolute `starting_fuel` value, byte-identical to this method's
        pre-Phase-26 behaviour."""
        attempts = 0
        row = None
        for attempt_num in range(2):
            rung_index = repeat_index if attempt_num == 0 else repeat_index + 1
            if rung_index >= ladder_rungs:
                break
            time.sleep(LIVE_PROBE_INTER_REQUEST_SECONDS)
            rung = ladder[rung_index]
            base_starting_fuel = cell.get("base_starting_fuel")
            if base_starting_fuel is not None:
                ladder_value = relative_cache_bust_starting_fuel(base_starting_fuel, rung)
            else:
                ladder_value = rung
            vehicle = dict(cell["vehicle"])
            vehicle["starting_fuel"] = str(ladder_value)
            (
                status,
                elapsed,
                stages,
                strategy,
                error,
                price_index_status,
                stop_count,
                total_cost,
            ) = self._post_route(
                cell["start"], cell["finish"], vehicle, waypoints=cell.get("waypoints", ())
            )
            attempts += 1

            censored = error is not None or (
                status is not None and not (200 <= status < 300)
            )
            cache_hit = (
                (not censored) and ("solver" not in stages) and ("cache" in stages)
            )
            breach = "dp_deadline_breach" in stages
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
                breach=breach,
                price_index_status=price_index_status,
                stop_count=stop_count,
                total_cost=total_cost,
                slug=cell.get("slug"),
                tank_range_mi=cell.get("tank_range_mi"),
                offline_admitted=cell.get("offline_admitted"),
            )
            self.stdout.write(
                f"    {cell['label']} repeat={repeat_index} "
                f"attempt={attempt_num} starting_fuel={ladder_value}: "
                f"status={status} elapsed={elapsed:.2f}s stages_ms={stages} "
                f"cache_hit={cache_hit} censored={censored} "
                f"strategy={strategy} breach={breach} "
                f"stop_count={stop_count} total_cost={total_cost} "
                f"price_index_status={price_index_status} error={error}"
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
                    # PROV-03: the production setting, matching penalty
                    # above -- this workstation baseline exists to compare
                    # like with live, so it must solve under the SAME
                    # settings a live request would.
                    trust_margin=settings.TRUST_MARGIN_USD,
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
                    f"strategy={row.solver_strategy} breach={row.breach} -- {marker}"
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
        self.stdout.write(
            "A `--recovery` sweep (if this run used that flag) is a "
            "bounded spot-check on at most three rule-selected cells, NOT "
            "a re-measurement of the offline 26-cell grid plan 18.1-07 "
            "already took, and NOT a substitute for it. The workstation- "
            "to-live factor this phase has measured before (18-11: 4.31x "
            "to 105.98x across just two cells) is not a single constant, "
            "so a `RECOVERED`/`NOT RECOVERED` verdict on these three "
            "cells says nothing, by itself, about any cell this sweep did "
            "not probe."
        )

    # --- recovery sweep (--recovery) ------------------------------------

    def _run_recovery_sweep(self):
        """Sweep `RECOVERY_PROBE_CELLS` -- the D-17 rule-selected
        criterion-3 probe set -- reusing `_wake`, `_measure_one_repeat`,
        the cache-bust ladder and the worst-of-repeats convention
        entirely (never forked). Prints the per-cell table, then D-18's
        `measurement_floor_violations` (loudly, before any verdict), then
        `recovery_verdict` -- applied verbatim, never paraphrased or
        adjusted. Exits non-zero while any measurement-floor violation
        stands; no verdict may be declared until every one is cleared.
        """
        ladder_rungs = len(LIVE_PROBE_CACHE_BUST_LADDER)
        implied_count = len(RECOVERY_PROBE_CELLS) * LIVE_PROBE_REPEATS * ladder_rungs + 1
        self._enforce_budget(implied_count, "recovery sweep")
        self.stdout.write(
            f"RECOVERY SWEEP -- Budget check: implied worst-case "
            f"requests={implied_count} <= LIVE_PROBE_MAX_REQUESTS="
            f"{LIVE_PROBE_MAX_REQUESTS}. Proceeding against "
            f"{LIVE_PROBE_BASE_URL}. RESPONSE_BAR_SECONDS={RESPONSE_BAR_SECONDS}."
        )
        self.stdout.write("")

        self._wake()
        requests_issued = 1

        rows = []
        for cell in RECOVERY_PROBE_CELLS:
            for repeat_index in range(LIVE_PROBE_REPEATS):
                row, attempts = self._measure_one_repeat(cell, repeat_index, ladder_rungs)
                requests_issued += attempts
                rows.append(row)

        self._print_table(rows)
        self._print_request_accounting(requests_issued, implied_count)
        self._print_disclaimer()

        verdict_rows, floor_rows = self._build_recovery_verdict_input(rows)

        self.stdout.write(self.style.WARNING("MEASUREMENT-FLOOR VIOLATIONS:"))
        violations = measurement_floor_violations(floor_rows)
        if violations:
            for violation in violations:
                self.stdout.write(f"    {violation}")
        else:
            self.stdout.write("    (none)")
        self.stdout.write("")

        if violations:
            self.stdout.write(
                self.style.ERROR(
                    "NO VERDICT MAY BE DECLARED: at least one measurement-"
                    "floor violation stands above. Re-run against a "
                    "confirmed-warm instance with every repeat a genuine "
                    "cache miss rather than accepting this reading."
                )
            )
            raise CommandError(
                "Measurement floor violated -- see MEASUREMENT-FLOOR "
                "VIOLATIONS above. No recovery verdict declared."
            )

        verdict = recovery_verdict(verdict_rows)
        self.stdout.write(self.style.SUCCESS(f"RECOVERY VERDICT: {verdict}"))
        return verdict

    def _build_recovery_verdict_input(self, rows):
        """Reduce this sweep's raw `ProbeRow`s (one per attempt) into the
        one-row-per-cell shapes `recovery_verdict`/`measurement_floor_
        violations` (`routing.tests.test_dispatch_recovery`) require --
        worst-of-repeats over GENUINE cache misses only, per D-18's own
        floor. A cell with zero genuine misses this run reports a
        deliberately-large placeholder response time (never a fast
        figure that could accidentally look RECOVERED) and
        `all_repeats_genuine_cache_miss=False`, so it always shows up as
        a measurement-floor violation rather than silently vanishing
        from the verdict input."""
        verdict_rows = []
        floor_rows = []
        for cell in RECOVERY_PROBE_CELLS:
            cell_rows = [r for r in rows if r.cell_label == cell["label"]]
            genuine = [r for r in cell_rows if r.is_genuine_miss]
            all_genuine = bool(cell_rows) and all(r.is_genuine_miss for r in cell_rows)

            if genuine:
                worst_row = max(genuine, key=lambda r: r.wall_time_s)
                worst_total_s = Decimal(str(worst_row.wall_time_s))
                worst_strategy = worst_row.solver_strategy
            else:
                worst_total_s = Decimal(RESPONSE_BAR_SECONDS) * Decimal(1000)
                worst_strategy = None

            verdict_rows.append(
                {
                    "slug": cell["slug"],
                    "tank_range_mi": cell["tank_range_mi"],
                    "shipped_policy_strategy": cell["pre_phase_shipped_arm"],
                    "worst_of_repeats_solver_strategy": worst_strategy,
                    "worst_of_repeats_total_response_seconds": worst_total_s,
                }
            )
            floor_rows.append(
                {
                    "slug": cell["slug"],
                    "tank_range_mi": cell["tank_range_mi"],
                    "confirmed_warm": True,  # this sweep always calls _wake() first
                    "repeats": len(cell_rows),
                    "all_repeats_genuine_cache_miss": all_genuine,
                    "reported_figure_kind": "worst",
                }
            )
        return verdict_rows, floor_rows

    # --- verdict sweep (--verdict, D-11/D-12/D-13) -----------------------

    def _run_verdict_sweep(self):
        """Sweep `VERDICT_PROBE_CELLS` -- the full 26-cell D-11 dispatch
        verdict matrix -- reusing `_wake`/`_measure_one_repeat`/
        `measurement_floor_violations`/`recovery_verdict` entirely,
        mirroring `_run_recovery_sweep`'s structure. D-12 fact (3) (no
        measured row is ever a cold-start row) needs no new tracking
        here: `_wake()` gates the whole sweep before the measurement loop
        below issues a single request, the exact same "always True
        because `_wake()` ran" pattern `_build_recovery_verdict_input`
        already relies on. Exits non-zero while any measurement-floor
        violation stands; no verdict may be declared over a dirty floor.
        """
        ladder_rungs = len(VERDICT_PROBE_CACHE_BUST_FRACTIONS)
        implied_count = (
            len(VERDICT_PROBE_CELLS)
            * VERDICT_PROBE_REPEATS
            * VERDICT_PROBE_WORST_CASE_ATTEMPTS_PER_REPEAT
            + 1
        )
        self._enforce_budget(implied_count, "verdict sweep", ceiling=VERDICT_PROBE_MAX_REQUESTS)
        self.stdout.write(
            f"VERDICT SWEEP -- Budget check: implied worst-case "
            f"requests={implied_count} <= VERDICT_PROBE_MAX_REQUESTS="
            f"{VERDICT_PROBE_MAX_REQUESTS}. Proceeding against "
            f"{LIVE_PROBE_BASE_URL}."
        )
        self.stdout.write("")

        self._wake()
        requests_issued = 1

        rows = []
        for cell in VERDICT_PROBE_CELLS:
            for repeat_index in range(VERDICT_PROBE_REPEATS):
                row, attempts = self._measure_one_repeat(
                    cell, repeat_index, ladder_rungs, ladder=VERDICT_PROBE_CACHE_BUST_FRACTIONS
                )
                requests_issued += attempts
                rows.append(row)

        self._print_verdict_table(rows)
        self._print_d12_facts(rows)
        self._print_request_accounting(requests_issued, implied_count)

        verdict_rows, floor_rows = self._build_verdict_input(rows)

        self.stdout.write(self.style.WARNING("MEASUREMENT-FLOOR VIOLATIONS:"))
        violations = measurement_floor_violations(floor_rows)
        if violations:
            for violation in violations:
                self.stdout.write(f"    {violation}")
        else:
            self.stdout.write("    (none)")
        self.stdout.write("")

        if violations:
            self.stdout.write(
                self.style.ERROR(
                    "NO VERDICT MAY BE DECLARED: at least one measurement-"
                    "floor violation stands above. Re-run against a "
                    "confirmed-warm instance with every repeat a genuine "
                    "cache miss rather than accepting this reading."
                )
            )
            raise CommandError(
                "Measurement floor violated -- see MEASUREMENT-FLOOR "
                "VIOLATIONS above. No verdict declared."
            )

        verdict = recovery_verdict(verdict_rows)
        self.stdout.write(self.style.SUCCESS(f"DISPATCH VERDICT: {verdict}"))
        return verdict

    def _print_verdict_table(self, rows):
        """Per-cell table: the pinned OFFLINE admission and the LIVE
        observed arm printed SIDE BY SIDE, never reconciled (D-13) --
        plus solver-stage ms, wall time, stop count, total cost,
        price_index_status, cache-hit/censored flags, and a `DISAGREES`
        marker where the two arm columns differ."""
        self.stdout.write(self.style.SUCCESS("26-CELL VERDICT TABLE:"))
        self.stdout.write(
            "    NOTE: the OFFLINE and LIVE arm columns below are printed "
            "side by side and never reconciled (D-13). Disagreement is "
            "EXPECTED, not a defect -- DispatchAdmissionManifestTests' own "
            "docstring states it deliberately does not assert live "
            "strategy, because an admitted cell can still breach the "
            "2.8s deadline on a slow box and fall back to the heuristic."
        )
        by_cell = {}
        for row in rows:
            by_cell.setdefault(row.cell_label, []).append(row)
        for label, cell_rows in by_cell.items():
            offline_admitted = cell_rows[0].offline_admitted
            offline_arm = (
                SolverStrategy.EXACT_DP
                if offline_admitted
                else SolverStrategy.PENALTY_AWARE_HEURISTIC
            )
            self.stdout.write(f"    {label} (offline_arm={offline_arm}):")
            for row in cell_rows:
                solver_ms = row.stages_ms.get("solver")
                total_ms = row.stages_ms.get("total")
                marker = (
                    "CENSORED"
                    if row.censored
                    else "CACHE-HIT (no latency figure)"
                    if row.cache_hit
                    else "GENUINE MISS"
                )
                disagrees = (
                    (not row.censored)
                    and (not row.cache_hit)
                    and row.solver_strategy is not None
                    and row.solver_strategy != offline_arm
                )
                self.stdout.write(
                    f"        repeat={row.repeat_index} status={row.status_code} "
                    f"wall={row.wall_time_s:.2f}s solver_stage_ms={solver_ms} "
                    f"total_stage_ms={total_ms} live_arm={row.solver_strategy} "
                    f"stop_count={row.stop_count} total_cost={row.total_cost} "
                    f"price_index_status={row.price_index_status} "
                    f"cache_hit={row.cache_hit} censored={row.censored} -- "
                    f"{marker}" + (" -- DISAGREES" if disagrees else "")
                )
        self.stdout.write("")

    def _print_d12_facts(self, rows):
        """D-12's four facts, printed loudly: (1) the confirmed deployed
        SHA `_wake()` already verified before a single row above was
        measured; (2) the genuine-cache-miss count out of the attempted
        total; (3) the statement that a wake preceded every measured
        request (structural, not per-row tracked -- see `_run_verdict_
        sweep`'s own docstring); (4) the distinct `price_index_status`
        values observed."""
        self.stdout.write(self.style.SUCCESS("D-12 MEASUREMENT-FLOOR FACTS:"))
        genuine = [r for r in rows if r.is_genuine_miss]
        price_statuses = sorted(
            {r.price_index_status for r in rows if r.price_index_status is not None}
        )
        self.stdout.write(
            f"    (1) confirmed deployed commit SHA: "
            f"{getattr(self, '_confirmed_commit_sha', None)}"
        )
        self.stdout.write(
            f"    (2) genuine cache misses: {len(genuine)} of {len(rows)} attempted rows"
        )
        self.stdout.write(
            "    (3) a wake preceded every measured request in this sweep "
            "-- the whole sweep is gated on one successful _wake() call "
            "before the measurement loop begins, so no row above is ever "
            "a cold-start row."
        )
        self.stdout.write(f"    (4) distinct price_index_status values observed: {price_statuses}")
        self.stdout.write("")

    def _build_verdict_input(self, rows):
        """Reduce this sweep's raw `ProbeRow`s (one per attempt) into the
        one-row-per-cell shapes `recovery_verdict`/`measurement_floor_
        violations` require -- worst-of-repeats over GENUINE cache misses
        only, exactly as `_build_recovery_verdict_input` already reduces
        `RECOVERY_PROBE_CELLS`' rows. `reported_figure_kind` is fixed at
        `"worst"` on every row."""
        verdict_rows = []
        floor_rows = []
        for cell in VERDICT_PROBE_CELLS:
            cell_rows = [r for r in rows if r.cell_label == cell["label"]]
            genuine = [r for r in cell_rows if r.is_genuine_miss]
            all_genuine = bool(cell_rows) and all(r.is_genuine_miss for r in cell_rows)

            if genuine:
                worst_row = max(genuine, key=lambda r: r.wall_time_s)
                worst_total_s = Decimal(str(worst_row.wall_time_s))
                worst_strategy = worst_row.solver_strategy
            else:
                worst_total_s = Decimal(RESPONSE_BAR_SECONDS) * Decimal(1000)
                worst_strategy = None

            verdict_rows.append(
                {
                    "slug": cell["slug"],
                    "tank_range_mi": cell["tank_range_mi"],
                    "shipped_policy_strategy": cell["pre_phase_shipped_arm"],
                    "worst_of_repeats_solver_strategy": worst_strategy,
                    "worst_of_repeats_total_response_seconds": worst_total_s,
                }
            )
            floor_rows.append(
                {
                    "slug": cell["slug"],
                    "tank_range_mi": cell["tank_range_mi"],
                    "confirmed_warm": True,  # this sweep always calls _wake() first
                    "repeats": len(cell_rows),
                    "all_repeats_genuine_cache_miss": all_genuine,
                    "reported_figure_kind": "worst",
                }
            )
        return verdict_rows, floor_rows

    # --- anomaly reproduction -----------------------------------------

    def _run_anomaly(self):
        implied_count = 2  # wake + one POST
        self._enforce_budget(implied_count, "anomaly reproduction")

        self._wake()
        time.sleep(LIVE_PROBE_INTER_REQUEST_SECONDS)
        status, elapsed, stages, strategy, error, _price_index_status, _stop_count, _total_cost = (
            self._post_route(
                ANOMALY_REQUEST["start"],
                ANOMALY_REQUEST["finish"],
                ANOMALY_REQUEST["vehicle"],
            )
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

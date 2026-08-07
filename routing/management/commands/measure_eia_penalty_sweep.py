"""Measure the EIA-regional-multiplier x fixed-charge-penalty coupling
(D-23..D-26): apply a synthetic multiplier ladder on top of each
candidate's already-EIA-priced `price_per_gallon`, across the twelve
pinned corridors, at the UI-default vehicle and the sourced `$35` penalty,
and report the stop count and station set at each rung.

Read-only: no writes, no database mutation beyond the `seed_stations`
replay every corridor-measurement command in this codebase triggers, no
network calls. It works with no routing-provider token set in the
environment, because it never constructs a routing-provider client at
all -- the twelve corridor geometries are committed fixtures, replayed
offline through the existing Directions-response parser, exactly as
`measure_prune_reduction.py` and `measure_plan_objective.py` already do.

Must NOT run in CI -- the figures below are evidence, not a pass/fail
gate, exactly as those two commands are evidence for their own subjects.
The CI-enforcing guard for the coupling this command measures in full is
`EiaPenaltyCouplingGuardTests` in `routing/tests/test_eia_penalty_sweep.py`,
which asserts the coupling's stated DIRECTION on one corridor and two
multipliers, not any exact stop count.

This module defines no corridor, vehicle, penalty, multiplier ladder, or
verdict threshold of its own: `CORRIDORS` and `load_corridor_route` come
from `routing.tests.test_corridor_fixtures`; `OBJECTIVE_PARAMS` (the
UI-default vehicle and the sourced `$35` penalty) comes from
`routing.tests.test_plan_objective`; `EIA_MULTIPLIER_LADDER`,
`EIA_SWING_STATED` and `EIA_SWING_VERDICT_MAX_STOP_DELTA` come from
`routing.tests.test_eia_penalty_sweep` -- pinned in that module, in its
own commit, before this command or the sweep it runs existed at all
(D-25). Every value printed below is read off those shared sources of
truth, never redeclared here.

The multiplier is applied SYNTHETICALLY, on top of the corridor's normal
`factor_lookup_for_basis(PRICE_BASIS_NEUTRAL)` build: each already-priced
`Candidate`'s `price_per_gallon` is scaled again by the current ladder
rung via `dataclasses.replace` (`Candidate` is frozen), never by feeding
a fabricated EIA response through `eia.py`'s own parser and never by
editing `routing/services/eia.py` or `routing/services/corridor.py` --
both stay byte-unchanged this phase; this command characterises the
coupling those two modules produce, it does not modify either of them.
"""
import dataclasses
from dataclasses import dataclass
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand

from routing.services import corridor, solver
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    PRICE_BASIS_NEUTRAL,
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_eia_penalty_sweep import (
    EIA_MULTIPLIER_LADDER,
    EIA_SWING_STATED,
    EIA_SWING_VERDICT_MAX_STOP_DELTA,
)
from routing.tests.test_plan_objective import OBJECTIVE_PARAMS


@dataclass(frozen=True)
class CorridorEiaSweepRow:
    """One measured (corridor, multiplier) cell."""

    slug: str
    label: str
    multiplier: Decimal
    stop_count: int
    station_ids: tuple
    total_cost: Decimal
    changed_vs_neutral: int


def _scale_candidates(candidates, multiplier):
    """Return a new list of `Candidate`s with `price_per_gallon` scaled by
    `multiplier` -- `Candidate` is frozen, so this is `dataclasses.replace`
    per candidate, never an in-place mutation. Applied on top of whatever
    price `corridor.candidates()` already computed (the neutral basis's
    1.0 factor, per D-24), so this is the sweep's OWN synthetic seam, not
    a second, competing EIA application."""
    return [
        dataclasses.replace(c, price_per_gallon=c.price_per_gallon * multiplier)
        for c in candidates
    ]


class Command(BaseCommand):
    help = (
        "Measure the EIA-regional-multiplier x fixed-charge-penalty "
        "coupling across the twelve pinned real-geometry corridors and "
        "EIA_MULTIPLIER_LADDER's four rungs, at OBJECTIVE_PARAMS' vehicle "
        "and the sourced $35 penalty, then apply D-25's pre-fixed verdict "
        "rule. Read-only beyond the seed_stations replay it triggers; no "
        "network calls; works with no routing-provider token set. Must "
        "NOT run in CI -- the figures are evidence, not a pass/fail gate; "
        "EiaPenaltyCouplingGuardTests in "
        "routing/tests/test_eia_penalty_sweep.py is the CI-enforcing "
        "guard that exists instead."
    )

    def handle(self, *args, **options):
        self._reseed()

        neutral_factor_for = factor_lookup_for_basis(PRICE_BASIS_NEUTRAL)
        rows_by_slug = {}
        for corridor_def in CORRIDORS:
            rows_by_slug[corridor_def.slug] = self._measure_corridor(
                corridor_def, neutral_factor_for
            )

        self._print_table(rows_by_slug)
        self._print_verdict(rows_by_slug)
        self._print_disclaimer()

    def _reseed(self):
        self.stdout.write(
            "Rebuilding the station table from the committed CSV "
            "(manage.py seed_stations, idempotent replay, no network "
            "call)..."
        )
        call_command("seed_stations", stdout=self.stdout)
        corridor.reset_index()
        self.stdout.write("")

    def _measure_corridor(self, corridor_def, factor_for):
        route = load_corridor_route(corridor_def.slug)
        base_candidates = corridor.candidates(route, factor_for=factor_for)

        rows = []
        neutral_station_ids = None
        for multiplier in EIA_MULTIPLIER_LADDER:
            scaled = _scale_candidates(base_candidates, multiplier)
            plan = solver.solve(
                scaled,
                route.total_route_mi,
                tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
                mpg=OBJECTIVE_PARAMS.mpg,
                starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
                penalty=OBJECTIVE_PARAMS.penalty,
                # PROV-03/D-18: this command measures the EIA x penalty
                # coupling (18-07), a subject the trust margin has nothing
                # to do with -- Decimal(0) keeps this measurement's own
                # behaviour provably unchanged.
                trust_margin=Decimal(0),
                deadline=None,  # D-05: untimed -- the coupling verdict is about station selection, not timing
            )
            station_ids = tuple(sorted(s.opis_id for s in plan.stops))
            if multiplier == Decimal("1.0"):
                neutral_station_ids = station_ids
            rows.append(
                CorridorEiaSweepRow(
                    slug=corridor_def.slug,
                    label=corridor_def.label,
                    multiplier=multiplier,
                    stop_count=len(plan.stops),
                    station_ids=station_ids,
                    total_cost=plan.total_cost,
                    changed_vs_neutral=0,  # filled in below, once the 1.0 rung is known
                )
            )

        # A second pass to compute changed_vs_neutral once the 1.0 rung's
        # station set is known -- EIA_MULTIPLIER_LADDER is not guaranteed
        # to list 1.0 first, so this cannot be folded into the loop above.
        neutral_ids = set(neutral_station_ids or ())
        rows = [
            dataclasses.replace(
                row,
                changed_vs_neutral=len(set(row.station_ids) ^ neutral_ids),
            )
            for row in rows
        ]
        return rows

    def _print_table(self, rows_by_slug):
        self.stdout.write(
            self.style.SUCCESS(
                "EIA x penalty coupling sweep -- neutral basis scaled "
                f"synthetically by EIA_MULTIPLIER_LADDER={EIA_MULTIPLIER_LADDER}, "
                f"at mpg={OBJECTIVE_PARAMS.mpg}, "
                f"tank_range_mi={OBJECTIVE_PARAMS.tank_range_mi}, "
                f"starting_fuel={OBJECTIVE_PARAMS.starting_fuel}, "
                f"penalty=${OBJECTIVE_PARAMS.penalty}:"
            )
        )
        for corridor_def in CORRIDORS:
            rows = rows_by_slug[corridor_def.slug]
            self.stdout.write(f"    {corridor_def.label} ({corridor_def.slug}):")
            for row in rows:
                self.stdout.write(
                    f"        multiplier={row.multiplier}: "
                    f"stops={row.stop_count} "
                    f"stations={row.station_ids} "
                    f"cost=${row.total_cost:.2f} "
                    f"changed_vs_1.0={row.changed_vs_neutral}"
                )

    def _print_verdict(self, rows_by_slug):
        low, high = EIA_SWING_STATED
        max_delta = 0
        max_delta_slug = None
        for corridor_def in CORRIDORS:
            rows = {row.multiplier: row for row in rows_by_slug[corridor_def.slug]}
            delta = abs(rows[high].stop_count - rows[low].stop_count)
            if delta > max_delta:
                max_delta = delta
                max_delta_slug = corridor_def.slug

        self.stdout.write("")
        self.stdout.write(
            f"Maximum observed stop-count delta across the stated swing "
            f"{EIA_SWING_STATED} (multiplier {low} -> {high}): {max_delta} "
            f"stop(s) ({max_delta_slug or 'n/a'})."
        )
        if max_delta <= EIA_SWING_VERDICT_MAX_STOP_DELTA:
            self.stdout.write(
                self.style.SUCCESS(
                    "VERDICT: RATIFIED -- the maximum observed stop-count "
                    f"delta ({max_delta}) is at most "
                    f"EIA_SWING_VERDICT_MAX_STOP_DELTA="
                    f"{EIA_SWING_VERDICT_MAX_STOP_DELTA}. The EIA x penalty "
                    "coupling is correct-as-designed: the fixed $35 "
                    "penalty appropriately trades off against a price "
                    "that moves with the EIA regional index, and no "
                    "further change is needed."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "VERDICT: NEEDS FOLLOW-UP -- the maximum observed "
                    f"stop-count delta ({max_delta}) exceeds "
                    f"EIA_SWING_VERDICT_MAX_STOP_DELTA="
                    f"{EIA_SWING_VERDICT_MAX_STOP_DELTA}. Candidate fix: "
                    "scale the fixed-charge penalty by the same EIA "
                    "factor, making it real-terms constant rather than a "
                    "flat dollar amount that means less when diesel is "
                    "expensive and more when it is cheap."
                )
            )
        self.stdout.write(
            "D-25's verdict rule (the ladder, the stated swing, and this "
            "threshold) was fixed in its own commit in "
            "routing/tests/test_eia_penalty_sweep.py before this sweep "
            "was ever run -- see that commit's log entry, which strictly "
            "precedes the commit adding this command."
        )

    def _print_disclaimer(self):
        self.stdout.write("")
        self.stdout.write(
            "This command characterises the coupling's shape on twelve "
            "corridors at one vehicle profile and one penalty ($35) and "
            "makes no claim about any other vehicle, penalty, or corridor "
            "set. It does not modify routing/services/eia.py or "
            "routing/services/corridor.py, and it does not implement "
            "penalty scaling -- D-25's verdict rule was fixed before this "
            "measurement ran, in the commit adding "
            "routing/tests/test_eia_penalty_sweep.py's "
            "EIA_MULTIPLIER_LADDER/EIA_SWING_STATED/"
            "EIA_SWING_VERDICT_MAX_STOP_DELTA constants."
        )

"""Run D-10's two trust-margin sweeps across the twelve pinned corridors:
the attribution sweep (tag only, price unchanged) and the realism sweep
(tag plus PADD-average price substitution), report them side by side, and
record the D-11 tagging weakness.

Read-only: no writes of its own beyond the `seed_stations` replay every
corridor-measurement command in this codebase triggers, no network calls.
It works with no routing-provider token set in the environment, because
it never constructs a routing-provider client at all -- the twelve
corridor geometries are committed fixtures, replayed offline through the
existing Directions-response parser, exactly as `measure_prune_reduction.py`
and `measure_eia_penalty_sweep.py` already do.

Must NOT run in CI -- the figures below are evidence, not a pass/fail
gate, exactly as those two commands are evidence for their own subjects.
There is no single CI-enforcing guard for the FULL sweep this command
runs; `routing/tests/test_trust_margin_rule.py`'s adoption round-trip
test (plan 21-08 Task 3) is the CI-enforcing guard for the ONE conclusion
this sweep feeds -- the shipped `TRUST_MARGIN_USD` default.

This module defines no corridor, vehicle, penalty, margin ladder, tag
share, tag seed or adoption rule of its own: `CORRIDORS`,
`PRICE_BASIS_NEUTRAL`, `factor_lookup_for_basis` and `load_corridor_route`
come from `routing.tests.test_corridor_fixtures`; `OBJECTIVE_PARAMS` (the
UI-default vehicle and the sourced `$35` penalty) comes from
`routing.tests.test_plan_objective`; `MARGIN_LADDER`, `TAG_SHARE_LADDER`,
`TAG_SEED`, `PADD_AVERAGE_PRICE`, `is_tagged` and `tagged_candidates` all
come from `routing.tests.test_trust_margin_rule`, pinned in plan 21-02
before any number existed and instantiated in plan 21-03 before this
sweep ever ran (D-09). `measure_plan_objective.py` is deliberately NOT
extended for this sweep's parameters (D-12): that module's own docstring
states it has "no basis, vehicle, penalty or trivial-stop threshold of
its own", and adding a margin ladder to it would contradict that stated
design and mix two subjects inside one evidence artifact. This command
also does not itself apply D-08's adoption rule -- `adopt_rung()` is
applied exactly once, by plan 21-08's Task 3, directly against this
command's own printed artifact; nothing here calls it, so the rule stays
applied in exactly one place.

D-10's non-reconciliation rule: the two sweeps below answer different
questions and are printed side by side without being reconciled. The
**attribution sweep** tags candidates and leaves their prices alone, so
the margin is the sole variable and any stop-set movement is provably
the margin's -- that sweep, and only that sweep, drives D-08's adoption
rule. The **realism sweep** tags and then substitutes each tagged
candidate's PADD average price, reproducing the real property that
same-region estimates share one price and carry zero differentiation --
that sweep, and only that sweep, supplies plan 21-09's copy witness. This
is this project's standing habit for figures that would otherwise look
like they should agree (34.50% vs 55%; 39/22 vs 45/26; the three latency
figures) -- printed side by side, never merged into one number.
"""
import dataclasses
from dataclasses import dataclass
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from routing.models import Station
from routing.services import corridor, regions, solver
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    PRICE_BASIS_NEUTRAL,
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_plan_objective import OBJECTIVE_PARAMS
from routing.tests.test_trust_margin_rule import (
    MARGIN_LADDER,
    PADD_AVERAGE_PRICE,
    TAG_SEED,
    TAG_SHARE_LADDER,
    is_tagged,
    tagged_candidates,
)

# The rung ladder every corridor/share cell is solved at: the margin-zero
# control first, then MARGIN_LADDER's three rungs, in ascending order --
# exactly the shape `adopt_rung()` (test_trust_margin_rule.py) requires of
# its own input.
_CONTROL_RUNG = Decimal("0")


def _rungs():
    return (_CONTROL_RUNG, *MARGIN_LADDER)


@dataclass(frozen=True)
class BypassDetail:
    """One kept real-priced stop that passed over a strictly cheaper
    estimate-priced station -- the criterion-5 witness harvest."""

    stop_name: str
    stop_opis_id: int
    bypassed_estimate_count: int
    bypassed_estimate_saving_forgone: Decimal


@dataclass(frozen=True)
class TrustMarginSweepRow:
    """One measured (sweep, corridor, tag share, rung) cell."""

    sweep_name: str
    slug: str
    label: str
    tag_share: Decimal
    realised_tagged_count: int
    rung: Decimal
    stop_count: int
    station_ids: tuple
    total_cost: Decimal
    strategy: str
    bypassed_estimate_total: int
    bypass_details: tuple


class Command(BaseCommand):
    help = (
        "Run the attribution and realism trust-margin sweeps across the "
        "twelve pinned real-geometry corridors, at OBJECTIVE_PARAMS' "
        "vehicle and the sourced $35 penalty, and report them side by "
        "side, unreconciled (D-10). Read-only beyond the seed_stations "
        "replay it triggers; no network calls; works with no "
        "routing-provider token set. Must NOT run in CI -- the figures "
        "are evidence, not a pass/fail gate."
    )

    def handle(self, *args, **options):
        self._reseed()
        padd_lookup = self._build_padd_lookup()

        neutral_factor_for = factor_lookup_for_basis(PRICE_BASIS_NEUTRAL)

        attribution_rows = []
        realism_rows = []
        for corridor_def in CORRIDORS:
            route = load_corridor_route(corridor_def.slug)
            base_candidates = corridor.candidates(route, factor_for=neutral_factor_for)
            attribution_rows.extend(
                self._measure_attribution(corridor_def, route, base_candidates)
            )
            realism_rows.extend(
                self._measure_realism(corridor_def, route, base_candidates, padd_lookup)
            )

        self._check_cell_count(attribution_rows, "attribution")
        self._check_cell_count(realism_rows, "realism")
        self._check_rung_key_parity(attribution_rows, "attribution")
        self._check_rung_key_parity(realism_rows, "realism")
        attribution_totals = self._check_nonzero_tagging(attribution_rows, "attribution")
        realism_totals = self._check_nonzero_tagging(realism_rows, "realism")

        self.stdout.write(
            self.style.SUCCESS(
                f"Cell counts -- attribution={len(attribution_rows)} "
                f"realism={len(realism_rows)} (both expected "
                f"{self._expected_cell_count()} = {len(CORRIDORS)} corridors x "
                f"{len(TAG_SHARE_LADDER)} shares x {len(_rungs())} rungs "
                f"including the zero control)."
            )
        )
        self.stdout.write(f"TAG_SEED={TAG_SEED}")
        self.stdout.write("")

        self._print_table(
            "ATTRIBUTION SWEEP -- tag only, price unchanged. The margin is "
            "the sole variable here, so any stop-set movement is provably "
            "the margin's. This sweep, and only this sweep, drives D-08's "
            "adoption rule (applied separately, once, by plan 21-08 Task 3).",
            attribution_rows,
            attribution_totals,
        )
        self._print_non_reconciliation_note()
        self._print_table(
            "REALISM SWEEP -- tag plus PADD-average price substitution, "
            "reproducing the real property that same-region estimates "
            "share one price and carry zero differentiation among "
            "themselves. This sweep, and only this sweep, supplies plan "
            "21-09's criterion-5 copy witness.",
            realism_rows,
            realism_totals,
        )
        self._print_d11_weakness()
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

    def _build_padd_lookup(self):
        """opis_id -> PADD region code, built once off the seeded routable
        Station rows (D-11's realism sweep needs a real region per
        candidate; `Candidate` itself carries no state field). Raises
        `CommandError` if any routable station's state maps to no PADD, or
        if any resulting PADD is absent from `PADD_AVERAGE_PRICE` -- a
        tagged candidate that silently kept its OPIS price in the realism
        sweep would make that sweep quietly part-attribution, precisely
        the confusion D-10 exists to prevent."""
        lookup = {}
        missing_region = []
        missing_price_table_entry = []
        for station in Station.objects.routable():
            region = regions.region_for_state(station.state)
            if region is None:
                missing_region.append(station.opis_id)
                continue
            if region not in PADD_AVERAGE_PRICE:
                missing_price_table_entry.append((station.opis_id, region))
                continue
            lookup[station.opis_id] = region

        if missing_region:
            raise CommandError(
                f"{len(missing_region)} routable station(s) map to no PADD "
                f"region (opis_id sample={missing_region[:20]}) -- a tagged "
                "candidate at one of these stations would silently keep its "
                "OPIS price in the realism sweep, the exact "
                "attribution/realism confusion D-10 exists to prevent."
            )
        if missing_price_table_entry:
            raise CommandError(
                f"{len(missing_price_table_entry)} routable station(s) map "
                f"to a PADD region absent from PADD_AVERAGE_PRICE (sample="
                f"{missing_price_table_entry[:20]}) -- same failure mode as "
                "above, one step later."
            )
        return lookup

    @staticmethod
    def _expected_cell_count():
        return len(CORRIDORS) * len(TAG_SHARE_LADDER) * len(_rungs())

    def _measure_attribution(self, corridor_def, route, base_candidates):
        rows = []
        for share in TAG_SHARE_LADDER:
            tagged_ids = {c.opis_id for c in base_candidates if is_tagged(c.opis_id, share)}
            tagged_list = tagged_candidates(base_candidates, share)
            for rung in _rungs():
                plan = solver.solve(
                    tagged_list,
                    route.total_route_mi,
                    tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
                    mpg=OBJECTIVE_PARAMS.mpg,
                    starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
                    penalty=OBJECTIVE_PARAMS.penalty,
                    trust_margin=rung,
                    deadline=None,  # D-05 precedent: untimed -- this verdict is about station selection, not timing
                )
                rows.append(
                    self._build_row(
                        "attribution", corridor_def, share, len(tagged_ids), rung, plan
                    )
                )
        return rows

    def _measure_realism(self, corridor_def, route, base_candidates, padd_lookup):
        rows = []
        for share in TAG_SHARE_LADDER:
            tagged_ids = {c.opis_id for c in base_candidates if is_tagged(c.opis_id, share)}
            tagged_list = tagged_candidates(base_candidates, share)
            realism_list = self._apply_realism_substitution(tagged_list, padd_lookup)
            for rung in _rungs():
                plan = solver.solve(
                    realism_list,
                    route.total_route_mi,
                    tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
                    mpg=OBJECTIVE_PARAMS.mpg,
                    starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
                    penalty=OBJECTIVE_PARAMS.penalty,
                    trust_margin=rung,
                    deadline=None,  # D-05 precedent: untimed -- this verdict is about station selection, not timing
                )
                rows.append(
                    self._build_row(
                        "realism", corridor_def, share, len(tagged_ids), rung, plan
                    )
                )
        return rows

    def _apply_realism_substitution(self, tagged_list, padd_lookup):
        """This sweep's OWN synthetic seam (mirrors
        `measure_eia_penalty_sweep.py`'s `_scale_candidates()`): every
        TAGGED candidate becomes a `dataclasses.replace` copy whose
        `price_per_gallon` is its PADD's value from `PADD_AVERAGE_PRICE`,
        applied on top of the neutral basis's already-computed price.
        Every untagged candidate is carried through unchanged. Never
        feeds a fabricated response through `eia.py`'s parser and never
        edits `routing/services/eia.py` or `routing/services/corridor.py`,
        both of which stay byte-unchanged this phase."""
        substituted = []
        for candidate in tagged_list:
            if not solver.is_estimate_priced(candidate):
                substituted.append(candidate)
                continue
            padd = padd_lookup.get(candidate.opis_id)
            if padd is None:
                raise CommandError(
                    f"Tagged candidate opis_id={candidate.opis_id} has no PADD "
                    "in the lookup built from the routable Station table -- "
                    "it would silently keep its OPIS price."
                )
            substituted.append(
                dataclasses.replace(candidate, price_per_gallon=PADD_AVERAGE_PRICE[padd])
            )
        return substituted

    @staticmethod
    def _build_row(sweep_name, corridor_def, share, realised_tagged_count, rung, plan):
        bypass_details = tuple(
            BypassDetail(
                stop_name=stop.name,
                stop_opis_id=stop.opis_id,
                bypassed_estimate_count=stop.bypassed_estimate_count,
                bypassed_estimate_saving_forgone=stop.bypassed_estimate_saving_forgone,
            )
            for stop in plan.stops
            if stop.bypassed_estimate_count > 0
        )
        return TrustMarginSweepRow(
            sweep_name=sweep_name,
            slug=corridor_def.slug,
            label=corridor_def.label,
            tag_share=share,
            realised_tagged_count=realised_tagged_count,
            rung=rung,
            stop_count=len(plan.stops),
            station_ids=tuple(sorted(s.opis_id for s in plan.stops)),
            total_cost=plan.total_cost,
            strategy=plan.strategy,
            bypassed_estimate_total=sum(s.bypassed_estimate_count for s in plan.stops),
            bypass_details=bypass_details,
        )

    def _check_cell_count(self, rows, sweep_name):
        expected = self._expected_cell_count()
        if len(rows) != expected:
            raise CommandError(
                f"{sweep_name} sweep produced {len(rows)} cell(s), expected "
                f"{expected} (= {len(CORRIDORS)} corridors x "
                f"{len(TAG_SHARE_LADDER)} shares x {len(_rungs())} rungs "
                "including the zero control) -- a truncated sweep must not "
                "be able to reach the adoption rule."
            )

    def _check_rung_key_parity(self, rows, sweep_name):
        keys_by_rung = {}
        for row in rows:
            keys_by_rung.setdefault(row.rung, set()).add((row.slug, row.tag_share))
        key_sets = list(keys_by_rung.values())
        control_set = key_sets[0]
        for other in key_sets[1:]:
            if other != control_set:
                raise CommandError(
                    f"{sweep_name} sweep: at least one rung's (corridor, "
                    "share) key set differs from the others -- a truncated "
                    "or asymmetric sweep must not be silently adoptable "
                    "from. Computed, not assumed."
                )

    @staticmethod
    def _check_nonzero_tagging(rows, sweep_name):
        control_rows = [r for r in rows if r.rung == _CONTROL_RUNG]
        totals_by_share = {}
        per_corridor_by_share = {}
        for row in control_rows:
            totals_by_share[row.tag_share] = (
                totals_by_share.get(row.tag_share, 0) + row.realised_tagged_count
            )
            per_corridor_by_share.setdefault(row.tag_share, []).append(
                (row.slug, row.realised_tagged_count)
            )
        for share, total in totals_by_share.items():
            if total <= 0:
                raise CommandError(
                    f"{sweep_name} sweep: total realised tagged-candidate "
                    f"count at share={share} is {total} across all twelve "
                    "corridors -- zero tagged candidates makes 'the margin "
                    "is the sole variable' a claim about nothing."
                )
        return {"totals_by_share": totals_by_share, "per_corridor_by_share": per_corridor_by_share}

    def _print_table(self, header, rows, totals):
        self.stdout.write(self.style.SUCCESS(header))
        self.stdout.write(
            f"    at mpg={OBJECTIVE_PARAMS.mpg}, "
            f"tank_range_mi={OBJECTIVE_PARAMS.tank_range_mi}, "
            f"starting_fuel={OBJECTIVE_PARAMS.starting_fuel}, "
            f"penalty=${OBJECTIVE_PARAMS.penalty}, rungs={_rungs()}:"
        )
        rows_by_corridor = {}
        for row in rows:
            rows_by_corridor.setdefault(row.slug, []).append(row)

        for corridor_def in CORRIDORS:
            corridor_rows = rows_by_corridor[corridor_def.slug]
            self.stdout.write(f"    {corridor_def.label} ({corridor_def.slug}):")
            rows_by_share = {}
            for row in corridor_rows:
                rows_by_share.setdefault(row.tag_share, []).append(row)
            for share in TAG_SHARE_LADDER:
                share_rows = rows_by_share[share]
                realised = share_rows[0].realised_tagged_count
                self.stdout.write(
                    f"        share={share} (realised_tagged={realised}):"
                )
                for row in sorted(share_rows, key=lambda r: r.rung):
                    self.stdout.write(
                        f"            rung=${row.rung:.2f} [{row.strategy}]: "
                        f"stops={row.stop_count} stations={row.station_ids} "
                        f"cost=${row.total_cost:.2f} "
                        f"bypassed_estimate_total={row.bypassed_estimate_total}"
                    )
                    for detail in row.bypass_details:
                        self.stdout.write(
                            f"                -> BYPASS: stop={detail.stop_name!r} "
                            f"opis_id={detail.stop_opis_id} "
                            f"bypassed_estimate_count={detail.bypassed_estimate_count} "
                            "bypassed_estimate_saving_forgone="
                            f"${detail.bypassed_estimate_saving_forgone:.2f}"
                        )

        self.stdout.write("")
        self.stdout.write("    Realised tagged-candidate totals across all 12 corridors:")
        for share in TAG_SHARE_LADDER:
            self.stdout.write(
                f"        share={share}: total={totals['totals_by_share'][share]} "
                f"per_corridor={totals['per_corridor_by_share'][share]}"
            )
        self.stdout.write("")

    def _print_non_reconciliation_note(self):
        self.stdout.write(
            self.style.WARNING(
                "The attribution and realism sweeps above are reported "
                "side by side and are deliberately NOT reconciled (D-10) "
                "-- they answer different questions (which margin value "
                "changes station selection, versus what a real "
                "regionally-priced estimate looks like once selected) and "
                "neither validates the other. This project's standing "
                "habit for figures that could look like they should agree "
                "but do not (34.50% vs 55%; 39/22 vs 45/26; the three "
                "latency figures)."
            )
        )
        self.stdout.write("")

    def _print_d11_weakness(self):
        self.stdout.write(
            self.style.WARNING(
                "D-11 recorded weakness: tagging is a deterministic share "
                "ladder over EXISTING routable rows, so tagged stations "
                "are geographically arbitrary rather than gap-shaped. The "
                "I-5 band from 32N to 43N holds ZERO routable stations -- "
                "that is exactly why it is the gap -- so nothing there can "
                "ever be tagged by this sweep. Only 4 of the 12 pinned "
                "corridors touch the West Coast at all, and three of those "
                "four enter outside the 32N-43N band (Seattle 47.6N, "
                "Hillsboro 45.5N, San Diego 32.8N). This sweep therefore "
                "cannot characterise the margin's effect inside the real "
                "coverage gap -- only on existing, arbitrarily-tagged rows "
                "elsewhere on the twelve corridors."
            )
        )
        self.stdout.write("")

    def _print_disclaimer(self):
        self.stdout.write(
            "This command characterises the trust margin's effect on "
            "twelve corridors at one vehicle profile and one penalty "
            "($35) and makes no claim about any other vehicle, penalty, "
            "or corridor set. It does not modify routing/services/eia.py "
            "or routing/services/corridor.py, and it does not itself "
            "apply D-08's adoption rule -- CORRIDORS/PRICE_BASIS_NEUTRAL/"
            "factor_lookup_for_basis/load_corridor_route come from "
            "routing.tests.test_corridor_fixtures; OBJECTIVE_PARAMS comes "
            "from routing.tests.test_plan_objective; MARGIN_LADDER/"
            "TAG_SHARE_LADDER/TAG_SEED/PADD_AVERAGE_PRICE/is_tagged/"
            "tagged_candidates come from routing.tests.test_trust_margin_rule, "
            "all pinned before this sweep ever ran (D-09). "
            "measure_plan_objective.py was deliberately NOT extended for "
            "this sweep's own parameters, per its own docstring's 'no "
            "basis, vehicle, penalty or trivial-stop threshold of its "
            "own' statement (D-12)."
        )

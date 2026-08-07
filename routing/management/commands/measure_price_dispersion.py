"""Measure D-07's derivation chain: the per-PADD dispersion of routable
station retail prices around their own region's mean, and the typical
per-purchase fill across the twelve pinned corridors, then apply the
already-pinned `derive_margin_rungs()` to the two resulting figures.

Read-only beyond the idempotent `seed_stations` CSV replay every
corridor-measurement command in this codebase triggers; no network calls.
It works with no routing-provider token set in the environment, because it
never constructs a routing-provider client at all -- the twelve corridor
geometries are committed fixtures, replayed offline through the existing
Directions-response parser, exactly as `measure_prune_reduction.py` and
`measure_eia_penalty_sweep.py` already do.

Must NOT run in CI -- the figures below are evidence, not a pass/fail
gate, exactly as those two commands are evidence for their own subjects.
The CI-enforcing guard is the derivation round-trip test in
`routing/tests/test_trust_margin_rule.py`, which asserts
`derive_margin_rungs(<measured dispersion literal>, <measured fill
literal>) == MARGIN_LADDER` on every commit.

This module defines no dispersion statistic, no typical-fill rule and no
ladder multipliers of its own: `DISPERSION_STATISTIC_NAME`,
`TYPICAL_FILL_RULE_NAME`, `MARGIN_LADDER_MULTIPLIERS` and
`derive_margin_rungs()` all come from `routing.tests.test_trust_margin_rule`,
pinned by plan 21-02 before this measurement was ever taken (D-09). This
command applies that already-committed formula to a real measurement; it
does not restate or re-derive it.

The same pass that measures dispersion also produces the per-PADD average
retail price table -- plan 21-08's realism sweep substitutes these prices
into tagged candidates, so the table is computed once here and used twice
(D-07/D-10), rather than risking a second, independent computation of the
same regional averages drifting from this one.

Two correctness guards, following `measure_prune_reduction.py`'s own
alternate-computation-must-agree idiom -- either raises `CommandError`
rather than printing a table that quietly contradicts itself:

  1. The per-PADD station counts must sum to the total routable count. A
     station whose state maps to no PADD (`regions.region_for_state`
     returning `None`) would otherwise silently vanish from the dispersion
     pool instead of being counted or explained.
  2. The pooled deviation sample size must equal the total routable count,
     and the typical-fill sample size must be strictly greater than zero.
     A median taken over an empty or silently truncated sample is the
     failure mode that would hand this phase an unsourced number wearing a
     derivation's clothes.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
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
    DISPERSION_STATISTIC_NAME,
    MARGIN_LADDER_MULTIPLIERS,
    TYPICAL_FILL_RULE_NAME,
    derive_margin_rungs,
)


@dataclass(frozen=True)
class PaddPriceRow:
    """One measured PADD's station count and mean retail price."""

    region_code: str
    label: str
    station_count: int
    mean_retail_price: Decimal


def _median(values):
    """Median of a non-empty sequence of `Decimal`s, computed exactly in
    `Decimal` -- never `float`. Averages the two middle values on an even
    count, exactly as `statistics.median` would, but without importing a
    stdlib helper whose internal arithmetic is not contractually pinned to
    `Decimal`."""
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


class Command(BaseCommand):
    help = (
        "Measure the per-PADD dispersion of routable station retail "
        "prices around their own region's mean, and the typical "
        "per-purchase fill across the twelve pinned corridors, then apply "
        "the already-pinned derive_margin_rungs() to the two resulting "
        "figures (D-07/D-09). Read-only beyond the seed_stations replay "
        "it triggers; no network calls; works with no routing-provider "
        "token set. Must NOT run in CI -- the figures are evidence, not a "
        "pass/fail gate; the derivation round-trip test in "
        "routing/tests/test_trust_margin_rule.py is the CI-enforcing "
        "guard that exists instead."
    )

    def handle(self, *args, **options):
        self._reseed()

        padd_rows, padd_prices_by_region = self._measure_padd_prices()
        pooled_deviations = self._measure_pooled_deviations(padd_rows, padd_prices_by_region)
        self._check_padd_counts_guard(padd_rows, pooled_deviations)

        fill_samples = self._measure_typical_fill()
        self._check_dispersion_and_fill_guard(pooled_deviations, fill_samples)

        dispersion_median = _median(pooled_deviations)
        fill_median = _median(fill_samples)

        self._print_padd_table(padd_rows)
        self._print_dispersion(pooled_deviations, dispersion_median)
        self._print_typical_fill(fill_samples, fill_median)
        self._print_rungs(dispersion_median, fill_median)
        self._print_disclaimer()

    # -- Measurement passes ------------------------------------------------

    def _reseed(self):
        self.stdout.write(
            "Rebuilding the station table from the committed CSV "
            "(manage.py seed_stations, idempotent replay, no network "
            "call)..."
        )
        call_command("seed_stations", stdout=self.stdout)
        corridor.reset_index()
        self.stdout.write("")

    def _measure_padd_prices(self):
        """Group every routable Station by the PADD its state maps to, and
        compute each PADD's station count and mean retail price. Stations
        whose state maps to no PADD are tracked separately and surfaced by
        `_check_padd_counts_guard` rather than silently dropped."""
        prices_by_region = {}
        unmapped_states = {}
        for station in Station.objects.routable().only("state", "retail_price"):
            region_code = regions.region_for_state(station.state)
            if region_code is None:
                unmapped_states[station.state] = unmapped_states.get(station.state, 0) + 1
                continue
            prices_by_region.setdefault(region_code, []).append(station.retail_price)

        self._unmapped_states = unmapped_states

        rows = []
        for region_code in sorted(prices_by_region):
            prices = prices_by_region[region_code]
            mean_price = sum(prices) / Decimal(len(prices))
            rows.append(
                PaddPriceRow(
                    region_code=region_code,
                    label=regions.REGION_LABELS.get(region_code, region_code),
                    station_count=len(prices),
                    mean_retail_price=mean_price,
                )
            )
        return rows, prices_by_region

    def _measure_pooled_deviations(self, padd_rows, padd_prices_by_region):
        """For every routable station with a known PADD, the absolute
        deviation of its retail price from its own PADD's mean -- pooled
        across all PADDs. This is DISPERSION_STATISTIC_NAME applied
        literally: the pooled list this returns is what `_median()` is
        applied to for the headline dispersion figure."""
        mean_by_region = {row.region_code: row.mean_retail_price for row in padd_rows}
        deviations = []
        for region_code, prices in padd_prices_by_region.items():
            mean_price = mean_by_region[region_code]
            for price in prices:
                deviations.append(abs(price - mean_price))
        return deviations

    def _measure_typical_fill(self):
        """Solve all twelve pinned corridors at the pinned UI-default
        vehicle and the neutral price basis, and collect every
        `FuelStop.gallons` across all twelve shipped plans -- this is
        TYPICAL_FILL_RULE_NAME applied literally."""
        factor_for = factor_lookup_for_basis(PRICE_BASIS_NEUTRAL)
        gallons = []
        for corridor_def in CORRIDORS:
            route = load_corridor_route(corridor_def.slug)
            candidates = corridor.candidates(route, factor_for=factor_for)
            plan = solver.solve(
                candidates,
                route.total_route_mi,
                tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
                mpg=OBJECTIVE_PARAMS.mpg,
                starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
                penalty=settings.FUEL_STOP_PENALTY_USD,
                # PROV-03/D-07: this command derives the margin's own
                # typical-fill input from the shipped, pre-margin plan --
                # Decimal(0) keeps it that way, never circularly measuring
                # a fill size the margin itself would have changed.
                trust_margin=Decimal(0),
                deadline=None,  # untimed -- this figure is about purchase sizes, not timing
            )
            gallons.extend(stop.gallons for stop in plan.stops)
        return gallons

    # -- Guards --------------------------------------------------------

    def _check_padd_counts_guard(self, padd_rows, pooled_deviations):
        total_routable = Station.objects.routable().count()
        padd_sum = sum(row.station_count for row in padd_rows)
        if padd_sum != total_routable:
            raise CommandError(
                f"per-PADD station counts sum to {padd_sum}, which does not "
                f"match the total routable count {total_routable} -- "
                f"{sum(self._unmapped_states.values())} station(s) have a "
                f"state that maps to no PADD ({self._unmapped_states!r}) and "
                "would otherwise silently vanish from the dispersion pool."
            )

    def _check_dispersion_and_fill_guard(self, pooled_deviations, fill_samples):
        total_routable = Station.objects.routable().count()
        if len(pooled_deviations) != total_routable:
            raise CommandError(
                f"pooled deviation sample size {len(pooled_deviations)} does "
                f"not equal the total routable count {total_routable} -- a "
                "median over a truncated sample cannot be trusted."
            )
        if len(fill_samples) == 0:
            raise CommandError(
                "typical-fill sample size is zero -- no FuelStop was "
                "produced across all twelve pinned corridors, so no median "
                "fill can be derived."
            )

    # -- Reporting -------------------------------------------------------

    def _print_padd_table(self, padd_rows):
        self.stdout.write(self.style.SUCCESS("Per-PADD station count and mean retail price:"))
        total = 0
        for row in padd_rows:
            self.stdout.write(
                f"    {row.label} ({row.region_code}): "
                f"station_count={row.station_count} "
                f"mean_retail_price=${row.mean_retail_price:.8f}"
            )
            total += row.station_count
        routable_total = Station.objects.routable().count()
        self.stdout.write(
            f"    sum_of_padd_station_counts={total} "
            f"routable_station_count={routable_total}"
        )
        self.stdout.write("")

    def _print_dispersion(self, pooled_deviations, dispersion_median):
        self.stdout.write(
            self.style.SUCCESS(
                f"Dispersion statistic ({DISPERSION_STATISTIC_NAME}):"
            )
        )
        self.stdout.write(
            f"    pooled_deviation_sample_size={len(pooled_deviations)}"
        )
        self.stdout.write(
            f"    pooled_median_absolute_deviation_usd_per_gallon="
            f"{dispersion_median:.8f}"
        )
        self.stdout.write("")

    def _print_typical_fill(self, fill_samples, fill_median):
        self.stdout.write(
            self.style.SUCCESS(f"Typical-fill statistic ({TYPICAL_FILL_RULE_NAME}):")
        )
        self.stdout.write(f"    typical_fill_sample_size={len(fill_samples)}")
        self.stdout.write(f"    typical_fill_median_gallons={fill_median:.8f}")
        self.stdout.write("")

    def _print_rungs(self, dispersion_median, fill_median):
        rungs = derive_margin_rungs(dispersion_median, fill_median)
        self.stdout.write(self.style.SUCCESS("Margin ladder (derive_margin_rungs applied):"))
        for multiplier, rung in zip(MARGIN_LADDER_MULTIPLIERS, rungs):
            self.stdout.write(f"    multiplier={multiplier}x: rung=${rung}")
        self.stdout.write("")

    def _print_disclaimer(self):
        self.stdout.write(
            "This command defines no dispersion statistic, typical-fill "
            "rule, or ladder multiplier of its own -- DISPERSION_STATISTIC_"
            "NAME, TYPICAL_FILL_RULE_NAME, MARGIN_LADDER_MULTIPLIERS and "
            "derive_margin_rungs() all come from "
            "routing.tests.test_trust_margin_rule, pinned before this "
            "measurement was taken (D-09). The per-PADD station counts and "
            "prices above come from the currently-seeded Station table "
            "(the committed data/stations_geocoded.csv, replayed via "
            "seed_stations); the typical-fill figure comes from solving "
            "the twelve corridors in routing.tests.test_corridor_fixtures "
            "at the pinned vehicle in routing.tests.test_plan_objective."
        )

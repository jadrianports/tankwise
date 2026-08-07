"""The single place the price-source provenance chain is asserted.

`price_source` travels model -> CSV -> pipeline commands -> corridor
candidate -> solver -> serializer -> UI, an eight-hop chain (D-18):

    1.  `Station` row (the model layer)
    2.  `IndexedStation` / `corridor._build_index()`
    3.  `Candidate` (the AST-gated pure solver boundary)
    4a. `dp.py`'s winning-edge bookkeeping (the exact-DP arm)
    4b. `heuristic.py`'s walk (the penalty-aware-heuristic arm)
    5.  `FuelStop` -- `solver.solve()`'s own rebuild loop, which converges
        both arms back onto one shape
    6/7. `FuelStopSerializer` (a top-level key, never nested inside
        `_rationale_repr()`) AND the committed cross-language fixture
        (`frontend/src/test/fixtures/route-response.json`) that carries
        that same shape across the Python -> TypeScript boundary (D-19)
    8.  the rendered stop row -- asserted on the OTHER side of that
        boundary, in `frontend/src/features/results/JustificationPopup.test.tsx`,
        which reads the very fixture hop 6/7 asserts here. This module
        cannot execute TypeScript, so its own hop-8 test is a POINTER,
        not an assertion: it confirms that file still exists and still
        names the two provenance-qualifier tests, so a rename or deletion
        on the frontend side breaks a test on this side too.

Each hop gets one named assertion class here (search this file for
`test_hop` to find every one), with both solver arms covered explicitly
wherever the hop touches the solver -- criterion 1's documented failure
mode is a chain complete in the model and serializer but silently dropped
in the UI, or complete on the exact-DP arm and dropped on the heuristic
arm, and neither is visible from an end-to-end test alone.
`HopCoverageGuardHopTests` at the bottom of this file is the anti-vacuity
guard: it pins the eight-hop set as a literal tuple and fails, naming the
missing hop, if any hop's `test_hop{N}_...`-tagged method goes missing.
"""

import csv
import io
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase
from shapely.geometry import LineString

from routing.management.commands.geocode_stations import EXPORT_HEADER
from routing.management.commands.seed_stations import STRAIGHT_FIELDS
from routing.models import GeocodePrecision, GeocodeStatus, PriceSource, Station
from routing.serializers import (
    FuelStopSerializer,
    RouteResponseSerializer,
    station_data_note,
)
from routing.services import dp as dp_module
from routing.services.corridor import candidates, price_source_counts, reset_index
from routing.services.dp import solve_fixed_charge
from routing.services.legs import Leg
from routing.services.mapbox import Route
from routing.services.naive_baseline import Savings
from routing.services.solver import (
    Candidate,
    FuelPlan,
    FuelStop,
    PurchaseReason,
    SolverStrategy,
    solve,
)
# Imported as a MODULE, never as `from ... import ResponseContractTests` --
# Django's test loader discovers every `TestCase` subclass that is a direct
# attribute of a scanned test module, so importing the class itself here
# would silently double-run `ResponseContractTests` (once from
# `test_serializers.py`, once from here). A module reference is not a
# `TestCase` subclass, so it is invisible to that discovery and safe to
# hold a live reference to `CURRENT_TOP_LEVEL_KEYS`/`CURRENT_FUEL_STOP_KEYS`.
from routing.tests import test_serializers

COMMITTED_CSV_PATH = Path(settings.BASE_DIR) / "data" / "stations_geocoded.csv"


def _candidate_with_price_source(name, opis_id, price, distance, price_source):
    return Candidate(
        name=name,
        opis_id=opis_id,
        price_per_gallon=Decimal(price),
        distance_from_start_mi=Decimal(distance),
        price_source=price_source,
    )


def _make_station(opis_id, **overrides):
    fields = dict(
        opis_id=opis_id,
        name="Test Station",
        address="I-00, EXIT 1 & US-1",
        city="Anytown",
        state="OK",
        rack_id="100",
        retail_price=Decimal("3.259"),
        observation_count=1,
        price_min=Decimal("3.259"),
        price_max=Decimal("3.259"),
    )
    fields.update(overrides)
    return Station.objects.create(**fields)


class StationPriceSourceModelHopTests(TestCase):
    """Hop 1: the model layer. A `Station` created with no explicit
    provenance resolves to the recorded-price wire value; an explicit
    estimate value round-trips through `refresh_from_db()`; and the choice
    set is exactly the two wire values PROV-01 names, in order."""

    def test_hop1_default_price_source_is_opis_indexed(self):
        station = _make_station(opis_id=101)

        self.assertEqual(station.price_source, PriceSource.OPIS_INDEXED)

    def test_explicit_estimate_value_round_trips_through_refresh(self):
        station = _make_station(
            opis_id=102, price_source=PriceSource.EIA_REGIONAL_ESTIMATE
        )

        station.refresh_from_db()

        self.assertEqual(station.price_source, PriceSource.EIA_REGIONAL_ESTIMATE)

    def test_price_source_values_is_exactly_the_two_wire_values_in_order(self):
        self.assertEqual(
            PriceSource.values, ["opis_indexed", "eia_regional_estimate"]
        )


class SeedStationsPriceSourceReplayHopTests(TestCase):
    """Hop-1-adjacent: replaying the real committed CSV through
    `seed_stations` leaves every row at the recorded-price value, since the
    committed dataset carries no estimate-sourced row in this phase."""

    def test_replaying_committed_csv_leaves_every_row_opis_indexed(self):
        call_command("seed_stations", str(COMMITTED_CSV_PATH), stdout=io.StringIO())

        total = Station.objects.count()
        opis_indexed_count = Station.objects.filter(
            price_source=PriceSource.OPIS_INDEXED
        ).count()

        self.assertEqual(total, 6738)
        self.assertEqual(opis_indexed_count, 6738)


class SeedStationsMissingColumnGuardHopTests(TestCase):
    """Hop-1-adjacent: a derived CSV missing a required column must fail
    loudly with `CommandError` naming it, never silently skip every row
    (T-20-06)."""

    def test_missing_price_source_column_raises_command_error(self):
        header_without_price_source = [
            "opis_id",
            "name",
            "address",
            "city",
            "state",
            "rack_id",
            "retail_price",
            "observation_count",
            "price_min",
            "price_max",
            "latitude",
            "longitude",
            "geocode_precision",
            "geocode_status",
        ]
        row = {
            "opis_id": "9001",
            "name": "No Provenance Stop",
            "address": "1 Test Rd",
            "city": "Testville",
            "state": "TX",
            "rack_id": "100",
            "retail_price": "3.10000000",
            "observation_count": "1",
            "price_min": "3.10000000",
            "price_max": "3.10000000",
            "latitude": "32.00000000",
            "longitude": "-97.00000000",
            "geocode_precision": "city",
            "geocode_status": "ok",
        }

        tmp = tempfile.NamedTemporaryFile(
            mode="w", newline="", suffix=".csv", delete=False, encoding="utf-8"
        )
        try:
            writer = csv.DictWriter(tmp, fieldnames=header_without_price_source)
            writer.writeheader()
            writer.writerow(row)
            tmp.close()

            with self.assertRaises(CommandError):
                call_command("seed_stations", tmp.name, stdout=io.StringIO())

            # Not thousands of rows silently skipped -- nothing was written.
            self.assertEqual(Station.objects.count(), 0)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


class VerifyStationsUnrecognizedPriceSourceHopTests(TestCase):
    """Hop-1-adjacent: `verify_stations` refuses to pass any row holding a
    `price_source` value outside `PriceSource.values` (D-10)."""

    def test_unrecognized_price_source_value_raises_command_error(self):
        station = _make_station(opis_id=103)
        Station.objects.filter(pk=station.pk).update(price_source="typo_value")

        with self.assertRaises(CommandError):
            call_command("verify_stations", stdout=io.StringIO())


class PipelineSchemaHopTests(TestCase):
    """Hop-1-adjacent: `EXPORT_HEADER` and `STRAIGHT_FIELDS` both carry the
    column, so a future column reorder is caught by a simple membership
    check rather than discovered at run time."""

    def test_export_header_contains_price_source(self):
        self.assertIn("price_source", EXPORT_HEADER)

    def test_straight_fields_contains_price_source(self):
        self.assertIn("price_source", STRAIGHT_FIELDS)


class CorridorSingleLegPriceSourceHopTests(TestCase):
    """Hop 2 (single-leg path): a `Candidate` built by
    `_candidates_single_leg` carries the source station's provenance --
    two stations with DIFFERING values distinguish "carried" from
    "defaulted"."""

    ROUTE_COORDS = [(-97.00, 30.00), (-97.00, 40.00)]
    TOTAL_ROUTE_MI = Decimal("700")

    def setUp(self):
        super().setUp()
        reset_index()

    def _route(self):
        return Route(
            total_route_mi=self.TOTAL_ROUTE_MI,
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
        )

    def test_hop2_candidate_price_source_matches_its_own_station(self):
        recorded = _make_station(
            opis_id=9101,
            latitude=Decimal("32.50"),
            longitude=Decimal("-97.00"),
            geocode_status=GeocodeStatus.OK,
            geocode_precision=GeocodePrecision.ROOFTOP,
            price_source=PriceSource.OPIS_INDEXED,
        )
        estimated = _make_station(
            opis_id=9102,
            latitude=Decimal("35.00"),
            longitude=Decimal("-97.00"),
            geocode_status=GeocodeStatus.OK,
            geocode_precision=GeocodePrecision.ROOFTOP,
            price_source=PriceSource.EIA_REGIONAL_ESTIMATE,
        )

        result = candidates(self._route())
        by_opis_id = {c.opis_id: c for c in result}

        self.assertEqual(
            by_opis_id[recorded.opis_id].price_source, PriceSource.OPIS_INDEXED
        )
        self.assertEqual(
            by_opis_id[estimated.opis_id].price_source,
            PriceSource.EIA_REGIONAL_ESTIMATE,
        )


class CorridorMultiLegPriceSourceHopTests(TestCase):
    """Hop 2 (multi-leg path): a `Candidate` built by
    `_candidates_multi_leg` carries the source station's provenance too,
    including after the nearest-perpendicular dedup collapses a station
    seen from both adjacent legs to its single `opis_id` entry."""

    def setUp(self):
        super().setUp()
        reset_index()

    def _route(self):
        # Two straight north-south legs sharing a boundary waypoint at
        # lat 35.00 -- mirrors test_multi_leg.py's DetourCorridorTestCase
        # shape, simplified to straight legs since no simplification
        # hazard is under test here.
        leg0 = [(-97.00, 30.00), (-97.00, 35.00)]
        leg1 = [(-97.00, 35.00), (-97.00, 40.00)]
        combined = leg0 + leg1[1:]
        return Route(
            total_route_mi=Decimal("700"),
            geometry=LineString(combined),
            raw_coordinates=combined,
            leg_distances_mi=[Decimal("350"), Decimal("350")],
            leg_annotation_lengths=[1, 1],
        )

    def test_boundary_station_dedup_keeps_its_own_provenance(self):
        # Sits exactly on the shared boundary waypoint, so BOTH legs'
        # corridor queries pick it up -- best_by_opis_id must collapse
        # the two per-leg Candidate builds to one entry, and that entry
        # must still carry this station's own provenance.
        station = _make_station(
            opis_id=9201,
            latitude=Decimal("35.00"),
            longitude=Decimal("-97.00"),
            geocode_status=GeocodeStatus.OK,
            geocode_precision=GeocodePrecision.CITY,
            price_source=PriceSource.EIA_REGIONAL_ESTIMATE,
        )

        result = candidates(self._route())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].opis_id, station.opis_id)
        self.assertEqual(result[0].price_source, PriceSource.EIA_REGIONAL_ESTIMATE)

    def test_two_stations_one_per_leg_both_carry_their_own_provenance(self):
        leg0_station = _make_station(
            opis_id=9202,
            latitude=Decimal("32.50"),
            longitude=Decimal("-97.00"),
            geocode_status=GeocodeStatus.OK,
            geocode_precision=GeocodePrecision.ROOFTOP,
            price_source=PriceSource.OPIS_INDEXED,
        )
        leg1_station = _make_station(
            opis_id=9203,
            latitude=Decimal("37.50"),
            longitude=Decimal("-97.00"),
            geocode_status=GeocodeStatus.OK,
            geocode_precision=GeocodePrecision.ROOFTOP,
            price_source=PriceSource.EIA_REGIONAL_ESTIMATE,
        )

        result = candidates(self._route())
        by_opis_id = {c.opis_id: c for c in result}

        self.assertEqual(
            by_opis_id[leg0_station.opis_id].price_source, PriceSource.OPIS_INDEXED
        )
        self.assertEqual(
            by_opis_id[leg1_station.opis_id].price_source,
            PriceSource.EIA_REGIONAL_ESTIMATE,
        )


class SolverCandidateDefaultPriceSourceHopTests(SimpleTestCase):
    """Hop 3: `Candidate` constructed with only the original four
    positional arguments still compiles and defaults its provenance to
    the recorded-price wire value -- the AST-gated pure solver boundary
    sees a plain `str`, never the Django-side `PriceSource` enum."""

    def test_hop3_four_argument_candidate_defaults_to_opis_indexed(self):
        candidate = Candidate(
            name="Test Stop",
            opis_id=1,
            price_per_gallon=Decimal("3.259"),
            distance_from_start_mi=Decimal("100"),
        )

        self.assertEqual(candidate.price_source, "opis_indexed")


class PriceSourceCountsHopTests(TestCase):
    """Hop 3: `corridor.price_source_counts()` over the seeded, committed
    dataset returns exactly one key at 6290 -- the committed CSV carries
    no estimate-sourced row in this phase -- and, once the lazy index is
    already warm, costs zero additional queries to call again (D-03)."""

    def setUp(self):
        super().setUp()
        reset_index()

    def test_seeded_dataset_composition_is_all_recorded_price(self):
        call_command("seed_stations", str(COMMITTED_CSV_PATH), stdout=io.StringIO())
        reset_index()

        counts = price_source_counts()

        self.assertEqual(counts, {"opis_indexed": 6290})

    def test_calling_twice_after_warm_costs_zero_queries(self):
        call_command("seed_stations", str(COMMITTED_CSV_PATH), stdout=io.StringIO())
        reset_index()
        price_source_counts()  # warm the lazy index once, outside the assertion.

        with self.assertNumQueries(0):
            price_source_counts()
            price_source_counts()


class DpExactArmPriceSourceHopTests(SimpleTestCase):
    """Hop 4a: the exact-DP arm (`dp.solve_fixed_charge`) copies
    provenance onto the winning edge and carries it onto every stop it
    emits, for both stops of a multi-stop plan -- not only the first.
    Reuses `test_dp.py`'s own
    `test_reaches_cheaper_stop_buying_only_enough_to_get_there` fixture
    shape (forces exactly two stops, `[1, 2]`), with the two candidates
    given DIFFERING provenance values so the test is positioned to fail
    if either stop carried the wrong -- or a defaulted -- value."""

    def _mixed_candidates(self):
        return [
            _candidate_with_price_source(
                "A", 1, "4.00", 100, PriceSource.EIA_REGIONAL_ESTIMATE
            ),
            _candidate_with_price_source(
                "B", 2, "3.00", 300, PriceSource.OPIS_INDEXED
            ),
        ]

    def _solve(self, candidates):
        return solve_fixed_charge(
            candidates,
            total_route_mi=Decimal(600),
            tank_range_mi=Decimal(400),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.25"),
            penalty=Decimal(0),
        )

    def test_hop4a_each_stop_carries_its_own_stations_provenance(self):
        plan = self._solve(self._mixed_candidates())

        self.assertEqual([s.opis_id for s in plan.stops], [1, 2])
        self.assertEqual(len(plan.stops), 2)
        self.assertEqual(
            plan.stops[0].price_source, PriceSource.EIA_REGIONAL_ESTIMATE
        )
        self.assertEqual(plan.stops[1].price_source, PriceSource.OPIS_INDEXED)
        # The provenance thread must not perturb the DP's own decision --
        # same station set/gallons this fixture already pins in test_dp.py.
        self.assertEqual(plan.stops[0].gallons, Decimal("20.00"))

    def test_all_recorded_price_candidates_return_stops_all_recorded_price(self):
        candidates = [
            _candidate_with_price_source(
                "A", 1, "4.00", 100, PriceSource.OPIS_INDEXED
            ),
            _candidate_with_price_source(
                "B", 2, "3.00", 300, PriceSource.OPIS_INDEXED
            ),
        ]

        plan = self._solve(candidates)

        self.assertTrue(
            all(s.price_source == PriceSource.OPIS_INDEXED for s in plan.stops)
        )


class HeuristicArmPriceSourceMultiStopHopTests(SimpleTestCase):
    """Hop 4b: the penalty-aware heuristic arm carries provenance at every
    transition and every purchase site, over a plan forced onto that arm
    by the pre-flight transition-count estimate alone -- the same
    over-budget shape `test_solver_dispatch.py`'s `HeavyLightDispatchTests`
    forces, but built from a synthetic corridor rather than a seeded
    database, so this hop needs no DB fixture.

    A dense cluster of 70 identically-priced ($5.00) filler candidates,
    packed into the route's first ~465 mi, inflates
    `dp.estimate_transition_count` well past `DP_TRANSITION_BUDGET`
    (50,000) without ever being cheap or reachable enough to affect which
    stations the walk actually purchases at. Five much cheaper ($2.00)
    "real" stations, spaced one tank apart along the rest of a 2,500 mi
    route, are the ones the heuristic actually stops at -- with
    DIFFERING (alternating) provenance values, so a stop after the first
    carrying the wrong -- or a defaulted -- value would be caught.
    `prune=False` keeps the dispatch estimate computed over the exact,
    hand-reasoned candidate set below (D-21's documented rollback hatch),
    rather than fighting the prune's own dominance removal.
    """

    TOTAL_ROUTE_MI = Decimal(2500)
    TANK_RANGE_MI = Decimal(500)
    FILLER_COUNT = 70
    FILLER_PRICE = Decimal("5.00")
    REAL_PRICE = Decimal("2.00")
    REAL_POSITIONS = [
        Decimal(480), Decimal(970), Decimal(1450), Decimal(1900), Decimal(2200),
    ]
    REAL_PRICE_SOURCES = [
        PriceSource.OPIS_INDEXED,
        PriceSource.EIA_REGIONAL_ESTIMATE,
        PriceSource.OPIS_INDEXED,
        PriceSource.EIA_REGIONAL_ESTIMATE,
        PriceSource.OPIS_INDEXED,
    ]

    def _candidates(self):
        span = Decimal(465)
        start = Decimal(5)
        fillers = []
        for i in range(self.FILLER_COUNT):
            frac = Decimal(i) / Decimal(self.FILLER_COUNT - 1)
            position = start + span * frac
            fillers.append(
                Candidate(
                    name=f"Filler {i}",
                    opis_id=10_000 + i,
                    price_per_gallon=self.FILLER_PRICE,
                    distance_from_start_mi=position,
                )
            )
        reals = [
            Candidate(
                name=f"Real {i}",
                opis_id=20_000 + i,
                price_per_gallon=self.REAL_PRICE,
                distance_from_start_mi=position,
                price_source=price_source,
            )
            for i, (position, price_source) in enumerate(
                zip(self.REAL_POSITIONS, self.REAL_PRICE_SOURCES)
            )
        ]
        return fillers + reals

    def _solve(self):
        return solve(
            self._candidates(),
            total_route_mi=self.TOTAL_ROUTE_MI,
            tank_range_mi=self.TANK_RANGE_MI,
            mpg=Decimal(10),
            starting_fuel=Decimal(1),
            penalty=Decimal(0),
            prune=False,
        )

    def test_fixture_estimate_exceeds_the_dp_transition_budget(self):
        """Sanity check that this fixture actually forces heuristic
        dispatch -- otherwise the assertions below would pass vacuously
        on the exact-DP arm instead of the arm this hop exists to cover."""
        search_set = sorted(
            self._candidates(),
            key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
        )
        estimate = dp_module.estimate_transition_count(
            search_set,
            total_route_mi=self.TOTAL_ROUTE_MI,
            tank_range_mi=self.TANK_RANGE_MI,
            starting_fuel=Decimal(1),
        )
        self.assertGreater(estimate, dp_module.DP_TRANSITION_BUDGET)

    def test_hop4b_at_least_three_stops_each_carry_their_own_stations_provenance(self):
        plan = self._solve()

        self.assertEqual(plan.strategy, SolverStrategy.PENALTY_AWARE_HEURISTIC)
        self.assertGreaterEqual(len(plan.stops), 3)
        self.assertEqual(
            [s.distance_from_start_mi for s in plan.stops], self.REAL_POSITIONS
        )
        self.assertEqual(
            [s.price_source for s in plan.stops], self.REAL_PRICE_SOURCES
        )


class FuelStopRebuildHopTests(SimpleTestCase):
    """Hop 5: `solver.solve()`'s own rebuild loop (the `FuelStop(...)`
    construction that copies every field off `raw_plan.stops`, including
    `price_source=raw_stop.price_source`) preserves it -- a DIFFERENT bug
    surface than hop 4a/4b, which prove the two arms' OWN internal
    bookkeeping (`dp.py`'s winning-edge reconstruction,
    `heuristic.py`'s walk) never drops it. Hop 4a's fixture calls
    `dp.solve_fixed_charge` directly, bypassing `solve()`'s rebuild
    entirely, so it cannot catch a rebuild that forgets to copy the
    field. Hop 4b's fixture already calls `solve()` (so its own passing
    is *also* evidence for this hop on the heuristic side), but only this
    class calls `solve()` on the exact-DP side and asserts
    `plan.strategy` is the exact-DP value -- the literal "exact-DP test
    asserts `plan.strategy`" half of this plan's own acceptance criteria,
    the heuristic half already being hop 4b's
    `test_hop4b_...` assertion above.

    Reuses hop 4a's `_mixed_candidates()` fixture shape (two stations,
    DIFFERING provenance, forced into exactly two stops by the same
    500mi range within a 600mi route) so a rebuild that drops or
    transposes `price_source` on either stop is caught."""

    def _mixed_candidates(self):
        return [
            _candidate_with_price_source(
                "A", 1, "4.00", 100, PriceSource.EIA_REGIONAL_ESTIMATE
            ),
            _candidate_with_price_source(
                "B", 2, "3.00", 300, PriceSource.OPIS_INDEXED
            ),
        ]

    def test_hop5_exact_dp_dispatch_through_solve_preserves_both_stops_provenance(self):
        plan = solve(
            self._mixed_candidates(),
            total_route_mi=Decimal(600),
            tank_range_mi=Decimal(400),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.25"),
            penalty=Decimal(0),
        )

        self.assertEqual(plan.strategy, SolverStrategy.EXACT_DP)
        self.assertEqual([s.opis_id for s in plan.stops], [1, 2])
        self.assertEqual(len(plan.stops), 2)
        self.assertEqual(
            plan.stops[0].price_source, PriceSource.EIA_REGIONAL_ESTIMATE
        )
        self.assertEqual(plan.stops[1].price_source, PriceSource.OPIS_INDEXED)

    def test_hop5_estimate_bypass_pair_survives_the_solve_rebuild(self):
        """Regression (plan 21-09): this same rebuild loop dropped the
        newer `bypassed_estimate_count`/`bypassed_estimate_saving_forgone`
        pair (plan 21-07) even though it never dropped `price_source`
        above. `dp.solve_fixed_charge` and
        `heuristic.solve_penalty_aware_heuristic` both populate the pair
        correctly on their OWN returned `FuelStop`s (proven directly by
        `routing.tests.test_dp.BypassedEstimateDisclosureTests`, which
        calls them without going through `solve()`), but `solve()`'s
        `FuelStop(...)` reconstruction had no line copying this specific
        pair across from `raw_stop` -- so the real production request
        path always reported `0`/`None` regardless of what either arm
        computed internally, which is what made plan 21-08's realism
        sweep (itself a `solve()` caller) come back with a literal-zero
        witness count across all 288 measured cells. Found and fixed
        while implementing this plan's own fallback witness action, which
        the plan's own text says must call `solver.solve()` -- not
        discovered by re-running or re-parameterizing that sweep, which
        this plan is explicitly forbidden from doing.

        Reuses `BypassedEstimateDisclosureTests._mixed_provenance_candidates()`'s
        exact scenario (three stations, penalty=35, B estimate-priced) but
        calls `solve()` itself, the one call this bug needed to hide
        behind."""
        candidates = [
            _candidate_with_price_source(
                "A", 1, "3.50", 250, PriceSource.OPIS_INDEXED
            ),
            _candidate_with_price_source(
                "B", 2, "3.00", 500, PriceSource.EIA_REGIONAL_ESTIMATE
            ),
            _candidate_with_price_source(
                "C", 3, "3.55", 700, PriceSource.OPIS_INDEXED
            ),
        ]

        plan = solve(
            candidates,
            total_route_mi=Decimal(1050),
            tank_range_mi=Decimal(500),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.5"),
            penalty=Decimal(35),
        )

        first_stop = plan.stops[0]
        self.assertEqual(
            first_stop.purchase_reason, PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
        )
        self.assertEqual(first_stop.bypassed_estimate_count, 1)
        self.assertEqual(
            first_stop.bypassed_estimate_saving_forgone, Decimal("12.50")
        )


class RenderedStopRowPointerHopTests(SimpleTestCase):
    """Hop 8: the rendered stop row. This Python module cannot execute
    TypeScript, so this is a POINTER, not an assertion (per this module's
    own docstring) -- it confirms
    `frontend/src/features/results/JustificationPopup.test.tsx` still
    exists and still names the two provenance-qualifier tests plan 20-05
    added (the recorded-price case and the estimate case), so a rename or
    deletion on the frontend side of D-19's cross-language join breaks a
    test on THIS side too, not just a silent frontend-only failure."""

    FRONTEND_POPUP_TEST_PATH = (
        Path(settings.BASE_DIR)
        / "frontend"
        / "src"
        / "features"
        / "results"
        / "JustificationPopup.test.tsx"
    )

    def test_hop8_frontend_popup_test_file_names_both_provenance_qualifier_tests(self):
        self.assertTrue(
            self.FRONTEND_POPUP_TEST_PATH.is_file(),
            f"{self.FRONTEND_POPUP_TEST_PATH} is missing -- hop 8's pointer target"
            " no longer exists.",
        )
        content = self.FRONTEND_POPUP_TEST_PATH.read_text(encoding="utf-8")

        self.assertIn("recorded-price", content)
        self.assertIn("regional-estimate", content)


class SerializerPriceSourceHopTests(SimpleTestCase):
    """Hop 6/7 (serializer half): `FuelStopSerializer` renders
    `price_source` as a direct top-level sibling of
    `price_per_gallon`/`cost`, never nested inside `rationale` --
    `_rationale_repr()`'s own docstring scopes it to facts explaining why
    the stop happened, a narrower contract than a ground-truth fact about
    the station itself. The fixture half of hop 6/7 -- the committed
    cross-language artefact D-19 requires (a `FixtureCrossLanguageHopTests`
    class comparing this serializer's fresh output against the committed
    `frontend/src/test/fixtures/route-response.json`) -- is added by this
    phase's next task/commit, alongside the fixture file itself."""

    def _stop(self, price_source):
        return FuelStop(
            name="Test Stop",
            opis_id=1,
            price_per_gallon=Decimal("3.259"),
            distance_from_start_mi=Decimal("100"),
            gallons=Decimal("10"),
            cost=Decimal("32.59"),
            price_source=price_source,
        )

    def test_hop6_7_serialized_stop_carries_price_source_top_level(self):
        data = FuelStopSerializer(self._stop(PriceSource.EIA_REGIONAL_ESTIMATE)).data

        self.assertEqual(data["price_source"], PriceSource.EIA_REGIONAL_ESTIMATE)

    def test_rationale_does_not_contain_price_source(self):
        data = FuelStopSerializer(self._stop(PriceSource.OPIS_INDEXED)).data

        self.assertNotIn("price_source", data["rationale"])

    def test_stop_with_no_provenance_serializes_price_source_as_null(self):
        stop = FuelStop(
            name="Legacy Stop",
            opis_id=2,
            price_per_gallon=Decimal("3.10"),
            distance_from_start_mi=Decimal("50"),
            gallons=Decimal("5"),
            cost=Decimal("15.50"),
        )

        data = FuelStopSerializer(stop).data

        self.assertIn("price_source", data)
        self.assertIsNone(data["price_source"])


class FixtureCrossLanguageHopTests(SimpleTestCase):
    """Hop 6/7 (fixture half): the committed cross-language artefact D-19
    requires. `frontend/src/test/fixtures/route-response.json` is asserted
    equal to a FRESHLY serialized response built from the same hand-built
    instance shape below -- load-and-compare, never write-and-overwrite,
    so this test can actually fail rather than being self-fulfilling.

    This is a serializer-level artefact assembled from hand-constructed
    `FuelStop`/`Leg`/`Savings`/`Route` instances -- it is NOT a station
    dataset, and its `eia_regional_estimate`-sourced stop is NOT an
    Overture row. No such row exists in `data/stations_geocoded.csv` or
    the `Station` table in this phase
    (`test_no_estimate_sourced_row_in_committed_station_csv` below proves
    it with the same `grep`-equivalent check the plan's own acceptance
    criteria name).

    Regeneration procedure (run deliberately by a human when the response
    shape legitimately changes -- never automated into this test): build
    `instance`/`context` exactly as `_fully_populated_two_stop_instance()`
    does below, serialize with `RouteResponseSerializer`, round-trip
    through `json.dumps`/`json.loads` to drop `Decimal`/`OrderedDict`
    artefacts, then
    ``Path(...).write_text(json.dumps(fresh, indent=2) + "\\n", encoding="utf-8")``
    to `frontend/src/test/fixtures/route-response.json`.

    PROV-03 (plan 21-09, D-19/D-21) -- the real instance behind
    `bypassed_estimate_count=1`/`bypassed_estimate_saving_forgone=Decimal("12.50")`
    on the recorded-price stop below. Plan 21-08's realism sweep found no
    qualifying corridor across all 288 measured cells (its own SUMMARY,
    confirmed against `21-TRUST-MARGIN-SWEEP.txt` directly), so this plan
    took the fallback branch its own action text authorizes: run
    `solver.solve()` over a hand-built fixed-provenance witness. The two
    `TrustMarginAnchorTests` witnesses named for that fallback
    (`routing/tests/test_solver_fixed_charge_optimality.py`, plan 21-04)
    were tried first, at every `MARGIN_LADDER` rung including the adopted
    `TRUST_MARGIN_USD=5.47` -- both change WHICH station gets bought, but
    neither ever populates `bypassed_estimate_count` at any rung, because
    that pair is only computed inside `dp.py`'s
    `truly_bypassed and penalty > saving_total` branch (a full-tank fill
    that flies PAST a reachable-but-not-worth-a-stop cheaper station), a
    structurally different decision than either witness's same-position
    swap or optional-stop drop. Rather than invent numbers or re-run the
    zero-result sweep (both forbidden), this plan fell back one step
    further to a second real, already-committed hand-built witness that
    DOES exercise that exact branch:
    `routing.tests.test_dp.BypassedEstimateDisclosureTests._mixed_provenance_candidates()`
    (landed by plan 21-07 to prove this very disclosure pair), run through
    `solver.solve()` -- three stations, `total_route_mi=1050mi`,
    `tank_range_mi=500mi`, `mpg=10`, `starting_fuel=0.5`, `penalty=$35`:
    station A (opis_id=1, $3.50/gal, 250mi, recorded price) is kept,
    buying 50.0 gallons with `purchase_reason=bypass_cheaper_not_worth_stop`,
    flying past station B (opis_id=2, $3.00/gal, 500mi, regional
    estimate) on its way to station C (opis_id=3, $3.55/gal, 700mi,
    reach_finish) -- `bypassed_estimate_count=1`,
    `bypassed_estimate_saving_forgone=Decimal("12.500")` (quantizes to
    `"12.50"`), reproduced identically at `trust_margin=Decimal(0)` and
    at `trust_margin=Decimal("5.47")` (the adopted value): this
    disclosure pair is gated on `penalty`, not on `trust_margin`, an
    honest structural finding recorded here rather than smoothed over.

    Running this exact scenario through `solver.solve()` is also what
    surfaced a genuine, unrelated production bug this plan had to fix
    (Rule 1) to make its own fallback action possible at all: `solve()`'s
    own post-processing rebuild loop (`routing/services/solver.py`) never
    copied `bypassed_estimate_count`/`bypassed_estimate_saving_forgone`
    from `raw_stop` onto the `FuelStop`s it returns, even though both
    solver arms already compute the pair correctly internally -- see
    `FuelStopRebuildHopTests.test_hop5_estimate_bypass_pair_survives_the_solve_rebuild`
    above and 21-09-SUMMARY.md for the full account. This is almost
    certainly why plan 21-08's realism sweep (itself a `solver.solve()`
    caller) came back with a literal zero across all 288 cells.
    """

    FIXTURE_PATH = (
        Path(settings.BASE_DIR)
        / "frontend"
        / "src"
        / "test"
        / "fixtures"
        / "route-response.json"
    )

    def _fully_populated_two_stop_instance(self):
        raw_coords = [[-97.7431, 30.2672], [-95.3698, 29.7604]]
        route = Route(
            total_route_mi=Decimal("200"),
            geometry=LineString(raw_coords),
            raw_coordinates=raw_coords,
            duration_s=Decimal("12000"),
        )
        stops = [
            FuelStop(
                name="Recorded Fuel Stop",
                opis_id=501,
                price_per_gallon=Decimal("3.89"),
                distance_from_start_mi=Decimal("80"),
                gallons=Decimal("41.20"),
                cost=Decimal("160.27"),
                purchase_reason=PurchaseReason.FILL_TO_CONTINUE,
                reason_target_opis_id=502,
                reason_target_name="Regional Estimate Fuel Stop",
                skipped_count=1,
                skipped_avg_price=Decimal("4.10"),
                price_percentile=Decimal("0.30"),
                corridor_avg_price=Decimal("3.95"),
                price_source=PriceSource.OPIS_INDEXED,
                # PROV-03 (plan 21-09, D-19/D-21): the recorded-price stop
                # is the ONLY one D-19's sentence can ever fire on -- set
                # here, never on the estimate-priced stop below, which
                # stays at its class defaults (0/None). See this class's
                # own docstring, below, for the real instance these two
                # numbers were transcribed from.
                bypassed_estimate_count=1,
                bypassed_estimate_saving_forgone=Decimal("12.50"),
            ),
            FuelStop(
                name="Regional Estimate Fuel Stop",
                opis_id=502,
                price_per_gallon=Decimal("3.72"),
                distance_from_start_mi=Decimal("180"),
                gallons=Decimal("35.00"),
                cost=Decimal("130.20"),
                purchase_reason=PurchaseReason.REACH_CHEAPER_STOP,
                skipped_count=0,
                price_percentile=Decimal("0.10"),
                corridor_avg_price=Decimal("3.95"),
                price_source=PriceSource.EIA_REGIONAL_ESTIMATE,
            ),
        ]
        plan = FuelPlan(
            stops=stops,
            total_cost=Decimal("290.47"),
            total_gallons=Decimal("76.20"),
            strategy=SolverStrategy.EXACT_DP,
        )
        instance = {
            "route": route,
            "plan": plan,
            "map_url": "https://example.test/map",
            "vehicle": {
                "mpg": Decimal("6.5"),
                "tank_range_mi": Decimal("1050"),
                "starting_fuel": Decimal("1"),
            },
            "legs": [
                Leg(
                    from_name="START",
                    to_name="Recorded Fuel Stop",
                    distance_mi=Decimal("80"),
                    duration_s=Decimal("4800"),
                    gallons=Decimal("0"),
                    cost=Decimal("0"),
                ),
                Leg(
                    from_name="Recorded Fuel Stop",
                    to_name="Regional Estimate Fuel Stop",
                    distance_mi=Decimal("100"),
                    duration_s=Decimal("6000"),
                    gallons=Decimal("41.20"),
                    cost=Decimal("160.27"),
                ),
                Leg(
                    from_name="Regional Estimate Fuel Stop",
                    to_name="FINISH",
                    distance_mi=Decimal("20"),
                    duration_s=Decimal("1200"),
                    gallons=Decimal("35.00"),
                    cost=Decimal("130.20"),
                ),
            ],
            "savings": Savings(
                amount=Decimal("15.20"),
                percent=Decimal("0.0497"),
                naive_total_cost=Decimal("305.67"),
                naive_total_gallons=Decimal("78.00"),
                naive_stop_count=3,
            ),
            "alternatives": [
                {
                    "total_route_mi": Decimal("200"),
                    "duration_s": Decimal("12000"),
                    "total_cost": Decimal("290.47"),
                    "chosen": True,
                    "feasible": True,
                },
            ],
            "price_index_status": "current",
            "eia_week": "2026-07-20",
            "trend_region": "PADD 3",
            # An IntegerField on the wire (routing/views.py's OpenAPI
            # schema) -- a raw Decimal here would raise on the
            # json.dumps round-trip below, unlike every money/gallon/mile
            # value elsewhere in this fixture.
            "trend_delta_cents": -2,
        }
        context = {
            "stop_coords": {
                501: {"latitude": Decimal("30.10"), "longitude": Decimal("-97.50")},
                502: {"latitude": Decimal("29.90"), "longitude": Decimal("-95.80")},
            },
            "start_coords": {
                "latitude": Decimal("30.2672"), "longitude": Decimal("-97.7431")
            },
            "finish_coords": {
                "latitude": Decimal("29.7604"), "longitude": Decimal("-95.3698")
            },
            # The two-value composition form -- the sentence Phase 22
            # will make live and the one no other test exercises against a
            # full payload.
            "price_source_counts": {"opis_indexed": 6290, "eia_regional_estimate": 412},
        }
        return instance, context

    def _fresh_response_dict(self):
        instance, context = self._fully_populated_two_stop_instance()
        data = RouteResponseSerializer(instance, context=context).data
        # Round-tripped through json.dumps/json.loads so Decimal/
        # OrderedDict artefacts do not make the comparison spuriously
        # fail against the committed file's plain dicts/floats/strings.
        return json.loads(json.dumps(data))

    def test_hop6_7_committed_fixture_matches_a_freshly_serialized_response(self):
        fresh = self._fresh_response_dict()
        committed = json.loads(self.FIXTURE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(committed, fresh)

    def test_fixture_top_level_keys_are_exactly_the_live_contract(self):
        committed = json.loads(self.FIXTURE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(
            set(committed.keys()), test_serializers.ResponseContractTests.CURRENT_TOP_LEVEL_KEYS
        )

    def test_fixture_fuel_stop_keys_are_exactly_the_live_contract(self):
        committed = json.loads(self.FIXTURE_PATH.read_text(encoding="utf-8"))

        for stop in committed["fuel_stops"]:
            self.assertEqual(
                set(stop.keys()),
                test_serializers.ResponseContractTests.CURRENT_FUEL_STOP_KEYS,
            )

    def test_fixture_has_at_least_two_fuel_stops_with_differing_price_source(self):
        committed = json.loads(self.FIXTURE_PATH.read_text(encoding="utf-8"))

        self.assertGreaterEqual(len(committed["fuel_stops"]), 2)
        self.assertEqual(
            {stop["price_source"] for stop in committed["fuel_stops"]},
            {PriceSource.OPIS_INDEXED, PriceSource.EIA_REGIONAL_ESTIMATE},
        )

    def test_fixture_station_data_note_matches_the_two_value_composition_form(self):
        committed = json.loads(self.FIXTURE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(
            committed["station_data_note"],
            "6,702 stations — 6,290 recorded, 412 regional estimates.",
        )

    def test_no_estimate_sourced_row_in_committed_station_csv(self):
        """T-20-28: this fixture's estimate-sourced stop must not be
        mistaken for dataset contamination -- the committed OPIS CSV
        carries no `eia_regional_estimate` row in this phase."""
        content = COMMITTED_CSV_PATH.read_text(encoding="utf-8")

        self.assertEqual(content.count("eia_regional_estimate"), 0)


class StationDataNoteHopTests(SimpleTestCase):
    """Hop-adjacent, not one of the eight numbered hops: `station_data_note()`
    is PROV-04's dataset-COMPOSITION disclosure (derived from the whole
    seeded table), a different feature from the per-stop `price_source`
    chain the eight hops trace. Pinned here anyway since it reads the same
    `PriceSource` wire values. Five composition branches against the exact
    approved copy (byte-compared, em dash included)."""

    def test_all_recorded_price(self):
        self.assertEqual(
            station_data_note({"opis_indexed": 6290}),
            "6,290 stations — all with recorded prices.",
        )

    def test_recorded_plus_estimates(self):
        self.assertEqual(
            station_data_note(
                {"opis_indexed": 6290, "eia_regional_estimate": 412}
            ),
            "6,702 stations — 6,290 recorded, 412 regional estimates.",
        )

    def test_empty_counts_returns_empty_string(self):
        self.assertEqual(station_data_note({}), "")

    def test_singular_nouns_with_one_of_each(self):
        self.assertEqual(
            station_data_note(
                {"opis_indexed": 1, "eia_regional_estimate": 1}
            ),
            "2 stations — 1 recorded, 1 regional estimate.",
        )

    def test_unrecognized_value_degrades_to_bare_total(self):
        self.assertEqual(
            station_data_note({"opis_indexed": 5, "some_unknown_value": 5}),
            "10 stations.",
        )


class HopCoverageGuardHopTests(SimpleTestCase):
    """The anti-vacuity guard D-18 asks for: pins the eight-hop set this
    module must cover as a literal, hardcoded tuple, and introspects this
    module's own test methods (never a hand-maintained list of what SHOULD
    be here) to confirm every hop identifier is named by at least one
    `test_hop{N}_...` method. A future refactor that drops, say, the
    heuristic-arm case has to delete `"hop4b"` from `HOPS` too -- a visible
    act, not a silent coverage loss.

    Deliberately checks the bare method name, not a class-qualified one:
    the mutation check this guard's own non-vacuity proof uses (see
    20-06-SUMMARY.md) is renaming one tagged method to drop its tag, which
    only fails this guard if the check is scoped to the method name itself.

    Non-vacuity, proven and reverted (recorded verbatim in
    20-06-SUMMARY.md): temporarily renamed
    `HeuristicArmPriceSourceMultiStopHopTests.test_hop4b_at_least_three_stops_each_carry_their_own_stations_provenance`
    to drop its `hop4b` tag -- this guard failed, naming `"hop4b"` as
    missing -- then the rename was reverted and the guard passed again."""

    HOPS = ("hop1", "hop2", "hop3", "hop4a", "hop4b", "hop5", "hop6_7", "hop8")

    @staticmethod
    def _all_test_method_names():
        module = sys.modules[__name__]
        names = []
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if isinstance(obj, type) and issubclass(obj, (TestCase, SimpleTestCase)):
                names.extend(
                    member for member in dir(obj) if member.startswith("test_")
                )
        return names

    def test_every_hop_identifier_is_named_by_at_least_one_test_method(self):
        method_names = self._all_test_method_names()

        missing = [
            hop for hop in self.HOPS if not any(hop in name for name in method_names)
        ]

        self.assertEqual(missing, [], f"Missing hop coverage for: {missing}")

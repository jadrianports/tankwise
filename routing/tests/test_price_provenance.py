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

"""The single place the price-source provenance chain is asserted.

`price_source` travels model -> CSV -> pipeline commands -> corridor
candidate -> solver -> serializer -> UI, an eight-hop chain (D-18). Each hop
gets one named assertion class here, with both solver arms (exact DP and
penalty-aware heuristic) covered explicitly where the hop touches the
solver. This module starts with hop 1 (the model layer); later plans in
this phase add hops 2 through 8.
"""

import csv
import io
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
from routing.services.corridor import candidates, price_source_counts, reset_index
from routing.services.dp import solve_fixed_charge
from routing.services.mapbox import Route
from routing.services.solver import Candidate

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

    def test_default_price_source_is_opis_indexed(self):
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

    def test_candidate_price_source_matches_its_own_station(self):
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

    def test_four_argument_candidate_defaults_to_opis_indexed(self):
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

    def test_each_stop_carries_its_own_stations_provenance(self):
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

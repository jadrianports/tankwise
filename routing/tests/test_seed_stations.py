import csv
import io
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from routing.models import GeocodeStatus, Station, StationSource
from routing.services.station_csv_paths import CANONICAL_STATION_CSV_PATHS

FIXTURE_HEADER = [
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
    "price_source",
    "source",
    "gers_id",
]

# One ok+city row, one ok+rooftop row, one failed row (blank coords), one
# out_of_scope row (blank coords) -- covers every geocode_status/precision
# combination seed_stations must handle.
FIXTURE_ROWS = [
    {
        "opis_id": "1001",
        "name": "Test Travel Center",
        "address": "123 Main St",
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
        "price_source": "opis_indexed",
        "source": "opis",
        "gers_id": "",
    },
    {
        "opis_id": "1002",
        "name": "Rooftop Fuel Stop",
        "address": "456 Elm St",
        "city": "Precisetown",
        "state": "OK",
        "rack_id": "200",
        "retail_price": "3.20000000",
        "observation_count": "1",
        "price_min": "3.20000000",
        "price_max": "3.20000000",
        "latitude": "35.00000000",
        "longitude": "-97.50000000",
        "geocode_precision": "rooftop",
        "geocode_status": "ok",
        "price_source": "opis_indexed",
        "source": "opis",
        "gers_id": "",
    },
    {
        "opis_id": "1003",
        "name": "Unresolved Stop",
        "address": "I-40, EXIT 1",
        "city": "Nowhere",
        "state": "NM",
        "rack_id": "300",
        "retail_price": "3.30000000",
        "observation_count": "1",
        "price_min": "3.30000000",
        "price_max": "3.30000000",
        "latitude": "",
        "longitude": "",
        "geocode_precision": "",
        "geocode_status": "failed",
        "price_source": "opis_indexed",
        "source": "opis",
        "gers_id": "",
    },
    {
        "opis_id": "1004",
        "name": "Canadian Stop",
        "address": "789 Rue Main",
        "city": "Montreal",
        "state": "QC",
        "rack_id": "400",
        "retail_price": "3.40000000",
        "observation_count": "1",
        "price_min": "3.40000000",
        "price_max": "3.40000000",
        "latitude": "",
        "longitude": "",
        "geocode_precision": "",
        "geocode_status": "out_of_scope",
        "price_source": "opis_indexed",
        "source": "opis",
        "gers_id": "",
    },
]


def _write_fixture_csv(rows):
    tmp = tempfile.NamedTemporaryFile(
        mode="w", newline="", suffix=".csv", delete=False, encoding="utf-8"
    )
    writer = csv.DictWriter(tmp, fieldnames=FIXTURE_HEADER)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    tmp.close()
    return tmp.name


class SeedStationsTests(TestCase):
    def setUp(self):
        self.csv_path = _write_fixture_csv(FIXTURE_ROWS)

    def tearDown(self):
        Path(self.csv_path).unlink(missing_ok=True)

    def test_seed_creates_all_fixture_rows_on_empty_db(self):
        out = io.StringIO()
        call_command("seed_stations", self.csv_path, stdout=out)

        self.assertEqual(Station.objects.count(), len(FIXTURE_ROWS))
        self.assertIn("4 created", out.getvalue())

    def test_second_run_is_a_no_op(self):
        call_command("seed_stations", self.csv_path, stdout=io.StringIO())

        out = io.StringIO()
        call_command("seed_stations", self.csv_path, stdout=out)

        self.assertEqual(Station.objects.count(), len(FIXTURE_ROWS))
        self.assertIn("0 created, 0 updated, 4 unchanged", out.getvalue())

    def test_ok_rows_routable_failed_and_out_of_scope_are_not(self):
        call_command("seed_stations", self.csv_path, stdout=io.StringIO())

        ok_city = Station.objects.get(opis_id=1001)
        ok_rooftop = Station.objects.get(opis_id=1002)
        failed = Station.objects.get(opis_id=1003)
        out_of_scope = Station.objects.get(opis_id=1004)

        routable_ids = set(Station.objects.routable().values_list("opis_id", flat=True))
        self.assertIn(1001, routable_ids)
        self.assertIn(1002, routable_ids)
        self.assertNotIn(1003, routable_ids)
        self.assertNotIn(1004, routable_ids)

        self.assertEqual(ok_city.geocode_status, GeocodeStatus.OK)
        self.assertIsNotNone(ok_city.latitude)
        self.assertEqual(ok_rooftop.geocode_precision, "rooftop")
        self.assertIsNotNone(ok_rooftop.latitude)

        self.assertEqual(failed.geocode_status, GeocodeStatus.FAILED)
        self.assertIsNone(failed.latitude)
        self.assertIsNone(failed.longitude)

        self.assertEqual(out_of_scope.geocode_status, GeocodeStatus.OUT_OF_SCOPE)
        self.assertIsNone(out_of_scope.latitude)
        self.assertIsNone(out_of_scope.longitude)

    def test_source_and_gers_id_land_on_the_model(self):
        call_command("seed_stations", self.csv_path, stdout=io.StringIO())

        for opis_id in (1001, 1002, 1003, 1004):
            station = Station.objects.get(opis_id=opis_id)
            self.assertEqual(station.source, StationSource.OPIS)
            self.assertIsNone(station.gers_id)

    def test_drifted_row_reconverges_to_csv_on_reseed(self):
        call_command("seed_stations", self.csv_path, stdout=io.StringIO())

        # Simulate drift: mutate a row directly in the DB.
        station = Station.objects.get(opis_id=1001)
        station.retail_price = Decimal("99.99000000")
        station.name = "Drifted Name"
        station.save()

        out = io.StringIO()
        call_command("seed_stations", self.csv_path, stdout=out)

        station.refresh_from_db()
        self.assertEqual(station.retail_price, Decimal("3.10000000"))
        self.assertEqual(station.name, "Test Travel Center")
        # Upsert (not skip-if-populated): the drifted row is reported as
        # updated, not left unchanged.
        self.assertIn("0 created, 1 updated, 3 unchanged", out.getvalue())

    def test_query_count_does_not_scale_with_row_count(self):
        # Guards against the pre-batching behavior: one write round trip per
        # row would make the 40-row query count roughly ten times the 4-row
        # count instead of staying flat.
        with CaptureQueriesContext(connection) as ctx_small:
            call_command("seed_stations", self.csv_path, stdout=io.StringIO())
        small_count = len(ctx_small)

        Station.objects.all().delete()

        large_rows = []
        for replica in range(10):
            for row in FIXTURE_ROWS:
                large_row = dict(row)
                large_row["opis_id"] = str(int(row["opis_id"]) + replica * 1000)
                large_rows.append(large_row)
        large_csv_path = _write_fixture_csv(large_rows)
        try:
            with CaptureQueriesContext(connection) as ctx_large:
                call_command("seed_stations", large_csv_path, stdout=io.StringIO())
            large_count = len(ctx_large)
        finally:
            Path(large_csv_path).unlink(missing_ok=True)

        self.assertLessEqual(large_count, small_count + 2)


class SeedStationsSourceGersIdColumnTests(TestCase):
    """source is required (STRAIGHT_FIELDS); gers_id is optional and
    blank-tolerant -- that asymmetry is deliberate (see seed_stations.py)."""

    def tearDown(self):
        Path(self.csv_path).unlink(missing_ok=True)

    def test_csv_missing_source_column_raises_command_error(self):
        header_without_source = [c for c in FIXTURE_HEADER if c != "source"]
        rows_without_source = []
        for row in FIXTURE_ROWS:
            trimmed = dict(row)
            del trimmed["source"]
            rows_without_source.append(trimmed)

        tmp = tempfile.NamedTemporaryFile(
            mode="w", newline="", suffix=".csv", delete=False, encoding="utf-8"
        )
        writer = csv.DictWriter(tmp, fieldnames=header_without_source)
        writer.writeheader()
        for row in rows_without_source:
            writer.writerow(row)
        tmp.close()
        self.csv_path = tmp.name

        with self.assertRaises(CommandError) as ctx:
            call_command("seed_stations", self.csv_path, stdout=io.StringIO())

        self.assertIn("source", str(ctx.exception))
        self.assertEqual(Station.objects.count(), 0)

    def test_csv_missing_gers_id_column_does_not_raise(self):
        # gers_id is optional -- a CSV that never had the column at all
        # (e.g. the pre-Phase-22 file shape) must still seed cleanly.
        header_without_gers_id = [c for c in FIXTURE_HEADER if c != "gers_id"]
        rows_without_gers_id = []
        for row in FIXTURE_ROWS:
            trimmed = dict(row)
            del trimmed["gers_id"]
            rows_without_gers_id.append(trimmed)

        tmp = tempfile.NamedTemporaryFile(
            mode="w", newline="", suffix=".csv", delete=False, encoding="utf-8"
        )
        writer = csv.DictWriter(tmp, fieldnames=header_without_gers_id)
        writer.writeheader()
        for row in rows_without_gers_id:
            writer.writerow(row)
        tmp.close()
        self.csv_path = tmp.name

        out = io.StringIO()
        call_command("seed_stations", self.csv_path, stdout=out)

        self.assertEqual(Station.objects.count(), len(FIXTURE_ROWS))
        self.assertIsNone(Station.objects.get(opis_id=1001).gers_id)

    def test_populated_gers_id_round_trips_onto_the_model(self):
        uuid_string = "08f2a1c4-9b3d-4e5f-8a6b-1c2d3e4f5a6b"
        overture_row = dict(FIXTURE_ROWS[0])
        overture_row["opis_id"] = "2001"
        overture_row["source"] = "overture"
        overture_row["gers_id"] = uuid_string

        self.csv_path = _write_fixture_csv([overture_row])

        call_command("seed_stations", self.csv_path, stdout=io.StringIO())

        station = Station.objects.get(opis_id=2001)
        self.assertEqual(station.source, StationSource.OVERTURE)
        self.assertEqual(station.gers_id, uuid_string)


class SeedStationsMultiPathTests(TestCase):
    """`seed_stations` accepts N paths (`nargs="*"`), replays every file
    inside one transaction against one shared `existing_by_opis_id`
    snapshot, and resolves a shared opis_id to the LATER file's values
    (D-26)."""

    def setUp(self):
        self.csv_path_a = _write_fixture_csv(FIXTURE_ROWS[:2])
        self.csv_path_b = _write_fixture_csv(FIXTURE_ROWS[2:])

    def tearDown(self):
        Path(self.csv_path_a).unlink(missing_ok=True)
        Path(self.csv_path_b).unlink(missing_ok=True)

    def test_two_files_seed_the_union_of_rows(self):
        out = io.StringIO()
        call_command("seed_stations", self.csv_path_a, self.csv_path_b, stdout=out)

        self.assertEqual(Station.objects.count(), len(FIXTURE_ROWS))
        self.assertIn("2 file(s)", out.getvalue())
        self.assertIn("4 created", out.getvalue())

    def test_shared_opis_id_resolves_to_the_later_files_values(self):
        row_first_file = dict(FIXTURE_ROWS[0])
        row_second_file = dict(FIXTURE_ROWS[0])
        row_second_file["name"] = "Later File Name"
        row_second_file["retail_price"] = "9.99000000"

        path_first = _write_fixture_csv([row_first_file])
        path_second = _write_fixture_csv([row_second_file])
        try:
            call_command("seed_stations", path_first, path_second, stdout=io.StringIO())

            station = Station.objects.get(opis_id=int(row_first_file["opis_id"]))
            self.assertEqual(station.name, "Later File Name")
            self.assertEqual(station.retail_price, Decimal("9.99000000"))
        finally:
            Path(path_first).unlink(missing_ok=True)
            Path(path_second).unlink(missing_ok=True)

    def test_no_argument_invocation_seeds_every_canonical_path(self):
        out = io.StringIO()
        call_command("seed_stations", stdout=out)

        for expected_path in (str(p) for p in CANONICAL_STATION_CSV_PATHS):
            self.assertIn(expected_path, out.getvalue())
        # data/stations_geocoded.csv carries thousands of rows -- this only
        # proves the canonical (not a fixture) file was actually replayed.
        self.assertGreater(Station.objects.count(), 1000)

    def test_second_file_missing_source_raises_command_error_naming_that_file(self):
        header_without_source = [c for c in FIXTURE_HEADER if c != "source"]
        rows_without_source = []
        for row in FIXTURE_ROWS[2:]:
            trimmed = dict(row)
            del trimmed["source"]
            rows_without_source.append(trimmed)

        tmp = tempfile.NamedTemporaryFile(
            mode="w", newline="", suffix=".csv", delete=False, encoding="utf-8"
        )
        writer = csv.DictWriter(tmp, fieldnames=header_without_source)
        writer.writeheader()
        for row in rows_without_source:
            writer.writerow(row)
        tmp.close()
        bad_path = tmp.name
        try:
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "seed_stations", self.csv_path_a, bad_path, stdout=io.StringIO()
                )
            message = str(ctx.exception)
            self.assertIn(Path(bad_path).name, message)
            self.assertIn("source", message)
            self.assertEqual(Station.objects.count(), 0)
        finally:
            Path(bad_path).unlink(missing_ok=True)

    def test_query_count_does_not_grow_linearly_with_file_count(self):
        with CaptureQueriesContext(connection) as ctx_one:
            call_command("seed_stations", self.csv_path_a, stdout=io.StringIO())
        one_file_count = len(ctx_one)

        Station.objects.all().delete()

        with CaptureQueriesContext(connection) as ctx_two:
            call_command(
                "seed_stations", self.csv_path_a, self.csv_path_b, stdout=io.StringIO()
            )
        two_file_count = len(ctx_two)

        # One shared snapshot query plus two batched write calls for the
        # whole run -- not per file -- so two files should not roughly
        # double the query count.
        self.assertLessEqual(two_file_count, one_file_count + 2)


class SeedStationsDatasetVintageTokenResetTests(TestCase):
    """Plan 22-12's own added behavior: `seed_stations` resets the
    process-level dataset-vintage token memo after every reseed, exactly as
    it already resets the corridor STRtree via `reset_index()` -- a reseed
    inside a long-lived process must not leave `routing.cache`'s memo
    describing the dataset the process started with rather than the one it
    just replayed."""

    def setUp(self):
        self.csv_path = _write_fixture_csv(FIXTURE_ROWS)

    def tearDown(self):
        Path(self.csv_path).unlink(missing_ok=True)

    def test_reset_dataset_vintage_token_is_called_once_per_run(self):
        with mock.patch(
            "routing.management.commands.seed_stations.reset_dataset_vintage_token"
        ) as mock_reset:
            call_command("seed_stations", self.csv_path, stdout=io.StringIO())

        mock_reset.assert_called_once()

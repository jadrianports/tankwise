import csv
import io
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from routing.models import GeocodeStatus, Station

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
]

# One ok+city row, one ok+rooftop row, one failed row (blank coords), one
# out_of_scope row (blank coords) -- covers every geocode_status/precision
# combination seed_stations must handle (D-18/D-27).
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

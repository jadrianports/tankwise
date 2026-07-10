"""Tests for the geocode_stations management command (D-27 extension):
status transitions, resume-selects-pending-only semantics, the bbox
persistence gate, and the derived-CSV export schema.

All tests stub `census_client.submit_chunk` -- no live network call is ever
performed -- and point the Gazetteer join at a tiny in-memory fixture lookup
instead of the real 31,539-row committed file, so results are deterministic.
"""

import csv
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from routing.models import GeocodePrecision, GeocodeStatus, Station
from routing.pipeline import gazetteer as gazetteer_module

# Tiny fixture lookup keyed exactly like gazetteer.load_gazetteer()'s real
# return shape: (normalize(name), state) -> (lat, lng).
FIXTURE_LOOKUP = {
    (gazetteer_module.normalize("Springfield"), "MO"): (37.2153, -93.2982),
}


def _no_match_stub(rows):
    """Stub for census_client.submit_chunk: every row comes back No_Match.
    Pure in-memory function -- no live network request anywhere."""
    return [
        {"id": row[0], "input_address": row[1], "match_status": "No_Match"}
        for row in rows
    ]


class GeocodeStationsCommandTests(TestCase):
    def setUp(self):
        # Ensure the Gazetteer module's lazy singleton cache never leaks the
        # real committed 31,539-row file into these tests, and always starts
        # from a clean slate for the patched loader below.
        gazetteer_module._lookup_cache = None
        self.addCleanup(self._reset_gazetteer_cache)

        load_patcher = mock.patch.object(
            gazetteer_module, "load_gazetteer", return_value=dict(FIXTURE_LOOKUP)
        )
        load_patcher.start()
        self.addCleanup(load_patcher.stop)

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.export_path = Path(self.tmpdir.name) / "stations_geocoded.csv"
        self.report_path = Path(self.tmpdir.name) / "geocode-report.md"

        self.matched_city_station = Station.objects.create(
            opis_id=1001,
            name="Truck Stop A",
            address="I-44, EXIT 1",
            city="Springfield",
            state="MO",
            rack_id="R1",
            retail_price=Decimal("3.25900000"),
            observation_count=1,
            price_min=Decimal("3.25900000"),
            price_max=Decimal("3.25900000"),
            geocode_status=GeocodeStatus.PENDING,
        )
        self.unmatched_city_station = Station.objects.create(
            opis_id=1002,
            name="Truck Stop B",
            address="Rural Route 5",
            city="Nowheresville",
            state="ZZ",
            rack_id="R2",
            retail_price=Decimal("3.45900000"),
            observation_count=1,
            price_min=Decimal("3.45900000"),
            price_max=Decimal("3.45900000"),
            geocode_status=GeocodeStatus.PENDING,
        )
        self.transposed_target_station = Station.objects.create(
            opis_id=1004,
            name="Truck Stop D",
            address="Highway 9",
            city="Nowhereville2",
            state="ZZ",
            rack_id="R4",
            retail_price=Decimal("3.65900000"),
            observation_count=1,
            price_min=Decimal("3.65900000"),
            price_max=Decimal("3.65900000"),
            geocode_status=GeocodeStatus.PENDING,
        )
        self.out_of_scope_station = Station.objects.create(
            opis_id=1003,
            name="Truck Stop C",
            address="Highway 1",
            city="Toronto",
            state="ON",
            rack_id="R3",
            retail_price=Decimal("3.55900000"),
            observation_count=1,
            price_min=Decimal("3.55900000"),
            price_max=Decimal("3.55900000"),
            geocode_status=GeocodeStatus.OUT_OF_SCOPE,
        )

    def _reset_gazetteer_cache(self):
        gazetteer_module._lookup_cache = None

    def _call(self, *args):
        call_command(
            "geocode_stations",
            *args,
            f"--export-path={self.export_path}",
            f"--report-path={self.report_path}",
        )

    # --- status transitions -------------------------------------------------

    def test_matched_city_row_ends_ok_with_city_precision(self):
        with mock.patch(
            "routing.pipeline.census_client.submit_chunk", side_effect=_no_match_stub
        ):
            self._call()

        self.matched_city_station.refresh_from_db()
        self.assertEqual(self.matched_city_station.geocode_status, GeocodeStatus.OK)
        self.assertEqual(
            self.matched_city_station.geocode_precision, GeocodePrecision.CITY
        )
        self.assertIsNotNone(self.matched_city_station.latitude)
        self.assertIsNotNone(self.matched_city_station.longitude)
        self.assertIn(self.matched_city_station, Station.objects.routable())

    def test_unmatched_city_row_ends_failed_with_null_coordinates(self):
        with mock.patch(
            "routing.pipeline.census_client.submit_chunk", side_effect=_no_match_stub
        ):
            self._call()

        self.unmatched_city_station.refresh_from_db()
        self.assertEqual(self.unmatched_city_station.geocode_status, GeocodeStatus.FAILED)
        self.assertIsNone(self.unmatched_city_station.latitude)
        self.assertIsNone(self.unmatched_city_station.longitude)
        self.assertNotIn(self.unmatched_city_station, Station.objects.routable())

    # --- out_of_scope is never touched (D-17) --------------------------------

    def test_out_of_scope_row_untouched_by_default_run(self):
        with mock.patch(
            "routing.pipeline.census_client.submit_chunk", side_effect=_no_match_stub
        ):
            self._call()

        self.out_of_scope_station.refresh_from_db()
        self.assertEqual(self.out_of_scope_station.geocode_status, GeocodeStatus.OUT_OF_SCOPE)
        self.assertIsNone(self.out_of_scope_station.latitude)
        self.assertIsNone(self.out_of_scope_station.longitude)

    def test_out_of_scope_row_untouched_by_retry_failed_run(self):
        with mock.patch(
            "routing.pipeline.census_client.submit_chunk", side_effect=_no_match_stub
        ):
            self._call("--retry-failed")

        self.out_of_scope_station.refresh_from_db()
        self.assertEqual(self.out_of_scope_station.geocode_status, GeocodeStatus.OUT_OF_SCOPE)
        self.assertIsNone(self.out_of_scope_station.latitude)

    # --- bbox persistence gate (D-05) ----------------------------------------

    def test_transposed_census_match_rejected_by_bbox_gate(self):
        target_id = str(self.transposed_target_station.opis_id)

        def _transposed_match_stub(rows):
            records = []
            for row in rows:
                opis_id = row[0]
                if opis_id == target_id:
                    # Deliberately invalid/transposed: neither ordering of
                    # (40.7, -73.9) is what matters here -- the coordinate
                    # pair below is simply out of the continental-US bbox.
                    records.append(
                        {
                            "id": opis_id,
                            "input_address": row[1],
                            "match_status": "Match",
                            "match_type": "Exact",
                            "matched_address": row[1],
                            "longitude": 40.7,
                            "latitude": -73.9,
                            "tigerlineid": "1",
                            "side": "L",
                        }
                    )
                else:
                    records.append(
                        {"id": opis_id, "input_address": row[1], "match_status": "No_Match"}
                    )
            return records

        with mock.patch(
            "routing.pipeline.census_client.submit_chunk",
            side_effect=_transposed_match_stub,
        ):
            self._call()

        self.transposed_target_station.refresh_from_db()
        # Rejected at the bbox gate -- never persisted as ok/rooftop. It has
        # no Gazetteer match either (city="Nowhereville2" is not in the
        # fixture lookup), so it correctly falls through to failed/null.
        self.assertNotEqual(self.transposed_target_station.geocode_status, GeocodeStatus.OK)
        self.assertNotEqual(
            self.transposed_target_station.geocode_precision, GeocodePrecision.ROOFTOP
        )
        self.assertIsNone(self.transposed_target_station.latitude)
        self.assertIsNone(self.transposed_target_station.longitude)

    # --- resume semantics (D-06) ---------------------------------------------

    def test_rerun_processes_zero_pending_rows(self):
        with mock.patch(
            "routing.pipeline.census_client.submit_chunk", side_effect=_no_match_stub
        ):
            self._call()

        self.assertEqual(
            Station.objects.filter(geocode_status=GeocodeStatus.PENDING).count(), 0
        )

        with mock.patch(
            "routing.pipeline.census_client.submit_chunk", side_effect=_no_match_stub
        ) as mock_submit:
            self._call()

        # Nothing left pending -> the Census pass has an empty working set ->
        # submit_chunk is never called on the second run.
        mock_submit.assert_not_called()

    # --- export schema (D-12) -------------------------------------------------

    def test_export_csv_header_matches_derived_schema(self):
        with mock.patch(
            "routing.pipeline.census_client.submit_chunk", side_effect=_no_match_stub
        ):
            self._call()

        with open(self.export_path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))

        self.assertEqual(
            header,
            [
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
            ],
        )

    def test_report_file_written_with_all_four_buckets(self):
        with mock.patch(
            "routing.pipeline.census_client.submit_chunk", side_effect=_no_match_stub
        ):
            self._call()

        report_text = self.report_path.read_text(encoding="utf-8")
        self.assertIn("Rooftop", report_text)
        self.assertIn("City centroid", report_text)
        self.assertIn("Failed", report_text)
        self.assertIn("Out of scope", report_text)

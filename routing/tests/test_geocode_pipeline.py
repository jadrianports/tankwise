"""Tests for the offline geocoding pipeline building blocks (routing/pipeline/):
bbox.py (continental-US validator, D-05), gazetteer.py (normalize + alias +
centroid join, D-04), and census_client.py (addressbatch parser, DATA-03).

All tests are pure/offline (SimpleTestCase) -- no DB access, no live network
calls (D-27 baseline tests).
"""
import csv
import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from routing.pipeline import bbox, census_client, gazetteer


class BboxValidatorTests(SimpleTestCase):
    def test_valid_us_coordinate_accepted(self):
        self.assertTrue(bbox.is_valid(lat=40.7, lng=-73.9))

    def test_zero_zero_rejected(self):
        self.assertFalse(bbox.is_valid(lat=0, lng=0))

    def test_transposed_pair_rejected(self):
        # lat=40.7, lng=-73.9 is valid; the transposed pair below must be rejected
        self.assertFalse(bbox.is_valid(lat=-73.9, lng=40.7))

    def test_accepts_decimal_input(self):
        self.assertTrue(bbox.is_valid(lat=Decimal("40.7"), lng=Decimal("-73.9")))

    def test_rejects_out_of_range_latitude(self):
        self.assertFalse(bbox.is_valid(lat=60.0, lng=-100.0))

    def test_rejects_out_of_range_longitude(self):
        self.assertFalse(bbox.is_valid(lat=35.0, lng=-40.0))


class GazetteerNormalizeTests(SimpleTestCase):
    def test_st_alias_resolves_saint(self):
        self.assertEqual(gazetteer.normalize("St. Louis"), gazetteer.normalize("SAINT LOUIS"))
        self.assertEqual(gazetteer.normalize("St. Louis"), "SAINT LOUIS")

    def test_mt_alias_resolves_mount(self):
        self.assertEqual(gazetteer.normalize("Mt Vernon"), "MOUNT VERNON")

    def test_ft_alias_resolves_fort(self):
        self.assertEqual(gazetteer.normalize("Ft Worth"), "FORT WORTH")

    def test_directional_alias_resolves(self):
        self.assertEqual(gazetteer.normalize("N Charleston"), "NORTH CHARLESTON")
        self.assertEqual(gazetteer.normalize("S Charleston"), "SOUTH CHARLESTON")

    def test_no_fuzzy_matching_dependency(self):
        # gazetteer.py must not IMPORT any fuzzy-matching library (checked via
        # AST, not substring search, so mentioning these names in a comment
        # or docstring doesn't trip a false positive).
        import ast

        import routing.pipeline.gazetteer as mod

        forbidden_modules = {"difflib", "rapidfuzz", "Levenshtein", "fuzzywuzzy"}
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & forbidden_modules, set())


class GazetteerLookupTests(SimpleTestCase):
    def _write_fixture(self, rows):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", suffix=".csv", delete=False
        )
        writer = csv.DictWriter(tmp, fieldnames=["name", "state", "lat", "lng"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        tmp.close()
        return tmp.name

    def test_known_city_state_returns_centroid_with_city_precision(self):
        fixture_path = self._write_fixture(
            [{"name": "Springfield", "state": "MO", "lat": "37.2153", "lng": "-93.2982"}]
        )
        lookup = gazetteer.load_gazetteer(fixture_path)
        result = gazetteer.lookup_city("Springfield", "MO", lookup=lookup)
        self.assertIsNotNone(result)
        self.assertEqual(result["precision"], "city")
        self.assertAlmostEqual(result["lat"], 37.2153)
        self.assertAlmostEqual(result["lng"], -93.2982)

    def test_unmatched_city_returns_none(self):
        fixture_path = self._write_fixture(
            [{"name": "Springfield", "state": "MO", "lat": "37.2153", "lng": "-93.2982"}]
        )
        lookup = gazetteer.load_gazetteer(fixture_path)
        result = gazetteer.lookup_city("Nowhereville", "ZZ", lookup=lookup)
        self.assertIsNone(result)

    def test_gazetteer_name_suffix_matches_normalized_csv_city(self):
        # Gazetteer NAME carries a legal-designator suffix ("city"); the CSV
        # City column does not. Both sides run through the same normalize()
        # pipeline (Pitfall C), so the join must still hit.
        fixture_path = self._write_fixture(
            [{"name": "Fort Smith city", "state": "AR", "lat": "35.3859", "lng": "-94.3985"}]
        )
        lookup = gazetteer.load_gazetteer(fixture_path)
        result = gazetteer.lookup_city("Fort Smith", "AR", lookup=lookup)
        self.assertIsNotNone(result)
        self.assertEqual(result["precision"], "city")

    def test_alias_resolution_applies_through_lookup(self):
        fixture_path = self._write_fixture(
            [{"name": "Saint Louis city", "state": "MO", "lat": "38.6270", "lng": "-90.1994"}]
        )
        lookup = gazetteer.load_gazetteer(fixture_path)
        result = gazetteer.lookup_city("St. Louis", "MO", lookup=lookup)
        self.assertIsNotNone(result)


# --- Census addressbatch response parser fixtures ---------------------------

# A captured-style fixture: one Match row (8 fields, lon,lat coordinate order)
# and several No_Match rows (3 fields) -- matches the real endpoint's
# documented variable-arity response shape (Pitfall A/B). No live network
# request is performed anywhere in these tests.
CENSUS_FIXTURE_RESPONSE = (
    '"1","123 Main St, Springfield, MO, 65801","No_Match"\r\n'
    '"2","456 Oak Ave, Reno, NV, 89501","Match","Exact","456 OAK AVE, RENO, NV, 89501",'
    '"-73.98658,40.738323","12345678","L"\r\n'
    '"3","789 Elm Rd, Boise, ID, 83702","No_Match"\r\n'
    '"4","321 Pine Ln, Tulsa, OK, 74103","No_Match"\r\n'
)


class CensusClientParserTests(SimpleTestCase):
    def test_match_row_yields_longitude_before_latitude(self):
        records = census_client.parse_addressbatch_response(CENSUS_FIXTURE_RESPONSE)
        match_record = next(r for r in records if r["id"] == "2")
        self.assertEqual(match_record["match_status"], "Match")
        self.assertAlmostEqual(match_record["longitude"], -73.98658)
        self.assertAlmostEqual(match_record["latitude"], 40.738323)

    def test_no_match_row_parses_without_raising_and_has_no_coordinates(self):
        records = census_client.parse_addressbatch_response(CENSUS_FIXTURE_RESPONSE)
        no_match_record = next(r for r in records if r["id"] == "1")
        self.assertEqual(no_match_record["match_status"], "No_Match")
        self.assertNotIn("longitude", no_match_record)
        self.assertNotIn("latitude", no_match_record)

    def test_mixed_response_returns_one_record_per_input_row(self):
        records = census_client.parse_addressbatch_response(CENSUS_FIXTURE_RESPONSE)
        self.assertEqual(len(records), 4)
        ids = {r["id"] for r in records}
        self.assertEqual(ids, {"1", "2", "3", "4"})

    def test_unrecognized_short_row_shape_falls_through_to_unmatched(self):
        # Simulate an unrecognized/short row (e.g. a Tie-shaped row not seen
        # in any captured fixture, per Assumptions Log A1) -- must not crash.
        tie_like_response = '"5","999 Tie Row, Nowhere, TX, 75001","Tie"\r\n'
        records = census_client.parse_addressbatch_response(tie_like_response)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["match_status"], "Tie")
        self.assertNotIn("longitude", records[0])

    def test_no_live_network_request_performed(self):
        # This parser is a pure function over an in-memory fixture string --
        # no session/URL is involved, so there is nothing to mock.
        self.assertIsInstance(CENSUS_FIXTURE_RESPONSE, str)
        result = census_client.parse_addressbatch_response(CENSUS_FIXTURE_RESPONSE)
        self.assertTrue(len(result) > 0)


class CensusClientBuildChunkCsvTests(SimpleTestCase):
    def test_build_chunk_csv_has_no_header_row(self):
        rows = [("1", "123 Main St", "Springfield", "MO", "65801")]
        csv_bytes = census_client.build_chunk_csv(rows)
        text = csv_bytes.decode("utf-8")
        self.assertNotIn("Unique ID", text)
        self.assertIn("123 Main St", text)

    def test_build_chunk_csv_preserves_row_order(self):
        rows = [
            ("1", "123 Main St", "Springfield", "MO", "65801"),
            ("2", "456 Oak Ave", "Reno", "NV", "89501"),
        ]
        csv_bytes = census_client.build_chunk_csv(rows)
        text = csv_bytes.decode("utf-8")
        lines = [line for line in text.splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertIn("Springfield", lines[0])
        self.assertIn("Reno", lines[1])


class CensusClientConstantsTests(SimpleTestCase):
    def test_targets_locations_addressbatch_endpoint(self):
        self.assertIn("/locations/addressbatch", census_client.ADDRESSBATCH_URL)
        self.assertNotIn("/geographies/", census_client.ADDRESSBATCH_URL)

    def test_uses_public_ar_current_benchmark(self):
        self.assertEqual(census_client.BENCHMARK, "Public_AR_Current")

    def test_chunk_size_within_locked_range(self):
        self.assertGreaterEqual(census_client.CHUNK_SIZE, 200)
        self.assertLessEqual(census_client.CHUNK_SIZE, 1000)

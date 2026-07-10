"""Tests for the offline geocoding pipeline building blocks (routing/pipeline/):
bbox.py (continental-US validator, D-05) and gazetteer.py (normalize + alias +
centroid join, D-04).

All tests are pure/offline (SimpleTestCase) -- no DB access, no live network
calls (D-27 baseline tests).
"""
import csv
import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from routing.pipeline import bbox, gazetteer


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
        # gazetteer.py must not import any fuzzy-matching library
        import routing.pipeline.gazetteer as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        for forbidden in ("difflib", "rapidfuzz", "Levenshtein", "fuzzywuzzy"):
            self.assertNotIn(forbidden, source)


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

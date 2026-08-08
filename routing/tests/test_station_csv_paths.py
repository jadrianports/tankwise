from django.test import SimpleTestCase

from routing.services.station_csv_paths import CANONICAL_STATION_CSV_PATHS


class StationCsvPathsTests(SimpleTestCase):
    def test_canonical_paths_is_a_tuple(self):
        self.assertIsInstance(CANONICAL_STATION_CSV_PATHS, tuple)

    def test_every_member_exists_on_disk(self):
        for path in CANONICAL_STATION_CSV_PATHS:
            self.assertTrue(path.exists(), f"{path} does not exist on disk")

    def test_no_member_is_the_raw_extract(self):
        names = [p.name for p in CANONICAL_STATION_CSV_PATHS]
        self.assertNotIn("overture_raw_extract.csv", names)

    def test_no_member_is_the_gazetteer_file(self):
        names = [p.name for p in CANONICAL_STATION_CSV_PATHS]
        self.assertNotIn("gazetteer_places_trimmed.csv", names)

    def test_tuple_currently_has_exactly_one_member(self):
        self.assertEqual(len(CANONICAL_STATION_CSV_PATHS), 1)

"""Tests for the pure Overture transform (`routing.pipeline.overture`).

`ImportOvertureStationsCommandTests` and `ApplyHygieneUnitTests` cover
hygiene, brand, price, identity, precision and provenance behaviour against
the hand-authored fixture extract. `OvertureIdCollisionRaiseTests` proves
the collision guard's raise branch is actually reachable via the pinned
witness pair -- a natural collision is unlikely at fixture (or even real
import) scale, so only the witness pair exercises that path.

`OvertureTransformDeterminismTests`, covering the thin `import_overture_
stations` command's determinism and dry-run behaviour, is added in the
next task once that command exists.
"""
import csv
import io
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from routing.pipeline import overture, overture_scope
from routing.services import regions

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "overture" / "raw_extract_sample.csv"
)


def _read_fixture_rows():
    with open(FIXTURE_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return overture.transform(reader)


class ParseExtractRowsHeaderTests(SimpleTestCase):
    def test_missing_required_column_raises_loudly(self):
        reader = csv.DictReader(io.StringIO("gers_id,name\n1,Test\n"))
        with self.assertRaises(overture.MalformedExtractHeaderError):
            overture.parse_extract_rows(reader)


class ApplyHygieneUnitTests(SimpleTestCase):
    """Direct unit coverage of `apply_hygiene`, independent of price
    assignment or identity minting -- the level the four-bucket invariant
    and the empty/closed operating-status behaviour are stated at."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(FIXTURE_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cls.parsed_rows, cls.malformed_count = overture.parse_extract_rows(reader)
        cls.kept, cls.counts = overture.apply_hygiene(cls.parsed_rows)
        cls.kept_by_gers_id = {row.gers_id: row for row in cls.kept}

    def test_four_hygiene_bucket_counts_plus_kept_equal_parsed_row_count(self):
        total = sum(self.counts.values()) + len(self.kept)
        self.assertEqual(total, len(self.parsed_rows))

    def test_only_the_four_hygiene_buckets_are_present(self):
        self.assertEqual(
            set(self.counts.keys()),
            {"mojibake", "alt_fuel_only", "closed_status", "below_confidence_floor"},
        )

    def test_empty_operating_status_row_survives_apply_hygiene(self):
        # The row that catches the negation bug: a NULL/blank
        # operating_status cell means unknown, not closed, and must be
        # retained.
        self.assertIn("11111111-1111-4111-8111-111111111107", self.kept_by_gers_id)

    def test_permanently_closed_row_does_not_survive_apply_hygiene(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111106", self.kept_by_gers_id)


class ImportOvertureStationsCommandTests(SimpleTestCase):
    """Covers `transform()` end to end -- hygiene, brand, price, identity,
    precision and provenance -- against the hand-authored fixture extract."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.station_rows, cls.report = _read_fixture_rows()
        cls.by_gers_id = {row.gers_id: row for row in cls.station_rows}

    def test_mojibake_row_dropped_and_counted(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111104", self.by_gers_id)
        self.assertEqual(self.report.bucket_counts["mojibake"], 1)

    def test_alt_fuel_row_dropped_and_counted(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111105", self.by_gers_id)
        self.assertEqual(self.report.bucket_counts["alt_fuel_only"], 1)

    def test_closed_status_row_dropped_and_counted(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111106", self.by_gers_id)
        self.assertEqual(self.report.bucket_counts["closed_status"], 1)

    def test_empty_operating_status_row_survives(self):
        self.assertIn("11111111-1111-4111-8111-111111111107", self.by_gers_id)

    def test_below_confidence_floor_row_dropped_and_counted(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111108", self.by_gers_id)
        self.assertEqual(self.report.bucket_counts["below_confidence_floor"], 1)

    def test_blank_state_row_routed_to_malformed_row(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111109", self.by_gers_id)

    def test_bad_confidence_row_dropped_at_parse_time(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111110", self.by_gers_id)

    def test_malformed_row_bucket_counts_blank_state_and_bad_parse(self):
        # Two malformed rows: one bad confidence cell (parse-time), one
        # blank state cell (price-assignment-time, unresolvable region).
        self.assertEqual(self.report.bucket_counts["malformed_row"], 2)

    def test_hygiene_bucket_counts_plus_kept_count_equal_input_row_count(self):
        total = sum(self.report.bucket_counts.values()) + self.report.kept_count
        self.assertEqual(total, self.report.input_row_count)

    def test_chain_alias_brand_matched_row_kept(self):
        self.assertIn("11111111-1111-4111-8111-111111111102", self.by_gers_id)

    def test_no_alias_plain_fuel_row_kept(self):
        self.assertIn("11111111-1111-4111-8111-111111111103", self.by_gers_id)

    def test_truck_category_row_kept(self):
        self.assertIn("11111111-1111-4111-8111-111111111101", self.by_gers_id)

    def test_every_kept_row_price_equals_region_baseline_with_zero_observations(self):
        for row in self.station_rows:
            region = regions.region_for_state(row.state)
            expected = regions.BASELINE_VALUES[region]
            self.assertEqual(row.retail_price, expected)
            self.assertEqual(row.price_min, expected)
            self.assertEqual(row.price_max, expected)
            self.assertEqual(row.observation_count, 0)

    def test_every_kept_row_carries_correct_provenance(self):
        for row in self.station_rows:
            self.assertEqual(row.geocode_precision, "rooftop")
            self.assertEqual(row.geocode_status, "ok")
            self.assertEqual(row.price_source, "eia_regional_estimate")
            self.assertEqual(row.source, "overture")

    def test_every_minted_opis_id_is_an_overture_id(self):
        for row in self.station_rows:
            self.assertTrue(overture_scope.is_overture_id(row.opis_id))

    def test_kept_rows_span_at_least_three_regions_including_california(self):
        regions_seen = {regions.region_for_state(row.state) for row in self.station_rows}
        self.assertGreaterEqual(len(regions_seen), 3)
        self.assertIn("CALIFORNIA", regions_seen)

    def test_output_row_order_is_sorted_by_opis_id(self):
        ids = [row.opis_id for row in self.station_rows]
        self.assertEqual(ids, sorted(ids))


class OvertureIdCollisionRaiseTests(SimpleTestCase):
    """Feeds `overture_scope.COLLISION_WITNESS_PAIR` through
    `mint_identities` and asserts it raises, naming both GERS ids."""

    def _witness_row(self, gers_id, address_freeform, state="TX"):
        return overture.OvertureRow(
            gers_id=gers_id,
            name="Witness Station",
            brand_name="",
            address_freeform=address_freeform,
            address_locality="Witness City",
            address_region=state,
            address_postcode="75201",
            category="gas_station",
            confidence=0.9,
            operating_status="open",
            longitude=Decimal("-96.797"),
            latitude=Decimal("32.7767"),
        )

    def test_collision_witness_pair_raises_naming_both_gers_ids(self):
        gers_a, gers_b = overture_scope.COLLISION_WITNESS_PAIR
        row_a = self._witness_row(gers_a, "1 Witness Rd")
        row_b = self._witness_row(gers_b, "2 Witness Rd")

        with self.assertRaises(overture.OvertureIdCollisionError) as ctx:
            overture.mint_identities([row_a, row_b])

        message = str(ctx.exception)
        self.assertIn(gers_a, message)
        self.assertIn(gers_b, message)
        self.assertIn(str(overture_scope.COLLISION_WITNESS_MINTED_ID), message)

"""Tests for the pinned Overture gap-fill scope, filter, hygiene and ID
minting constants -- routing/pipeline/overture_scope.py.

Pure-module test -- no DB needed, mirrors test_regions.py's SimpleTestCase
style since overture_scope.py is itself dependency-free.
"""
import re
import uuid
from pathlib import Path

from django.test import SimpleTestCase

from routing.pipeline import overture_scope

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NOTICE_PATH = REPO_ROOT / "NOTICE"


class OvertureScopeConstantTests(SimpleTestCase):
    def test_release_string_is_the_pinned_literal(self):
        self.assertEqual(overture_scope.OVERTURE_RELEASE, "2026-07-22.0")

    def test_release_string_matches_the_release_shape(self):
        self.assertRegex(overture_scope.OVERTURE_RELEASE, r"^\d{4}-\d{2}-\d{2}\.\d+$")

    def test_gap_fill_boxes_has_exactly_two_entries(self):
        self.assertEqual(len(overture_scope.GAP_FILL_BOXES), 2)

    def test_category_filter(self):
        self.assertEqual(
            overture_scope.CATEGORY_FILTER, ("gas_station", "truck_gas_station")
        )

    def test_confidence_floor(self):
        self.assertEqual(overture_scope.CONFIDENCE_FLOOR, 0.5)

    def test_tight_tier_threshold(self):
        self.assertEqual(overture_scope.TIGHT_TIER_THRESHOLD_MI, 0.25)

    def test_tight_tier_sensitivity_neighbours(self):
        self.assertEqual(overture_scope.TIGHT_TIER_SENSITIVITY_MI, (0.15, 0.40))

    def test_spot_check_cluster_count(self):
        self.assertEqual(overture_scope.SPOT_CHECK_CLUSTER_COUNT, 8)

    def test_extract_size_finding_threshold(self):
        self.assertEqual(
            overture_scope.EXTRACT_SIZE_FINDING_THRESHOLD_BYTES, 3_000_000
        )

    def test_no_row_cap_or_truncation_name_exists_on_the_module(self):
        pattern = re.compile(r"(max_rows|row_cap|limit|top_n)", re.IGNORECASE)
        offending = [name for name in dir(overture_scope) if pattern.search(name)]
        self.assertEqual(offending, [])

    def test_no_planning_decision_codes_in_shipped_source(self):
        source = open(overture_scope.__file__, encoding="utf-8").read()
        self.assertEqual(re.findall(r"D-\d{2}", source), [])


class GapFillBoxContainsTests(SimpleTestCase):
    def test_seattle_is_outside_the_multi_box(self):
        # Seattle sits north of lat 44 -- the route enters the boxes on its
        # way south, the endpoint itself need not be inside them.
        self.assertFalse(overture_scope.contains(47.6062, -122.3321))

    def test_san_diego_is_inside_the_socal_extension(self):
        self.assertTrue(overture_scope.contains(32.7157, -117.1611))

    def test_los_angeles_is_inside_the_multi_box(self):
        self.assertTrue(overture_scope.contains(34.0522, -118.2437))

    def test_sacramento_is_inside_the_northern_band(self):
        self.assertTrue(overture_scope.contains(38.5816, -121.4944))

    def test_bbox_predicate_sql_ors_both_boxes(self):
        sql = overture_scope.bbox_predicate_sql()
        self.assertEqual(sql.count(" OR "), 1)
        self.assertIn("bbox.xmin", sql)
        self.assertIn("bbox.ymin", sql)


class ClosedStatusTests(SimpleTestCase):
    def test_permanently_closed_is_closed(self):
        self.assertTrue(overture_scope.is_closed_status("permanently_closed"))

    def test_temporarily_closed_is_closed(self):
        self.assertTrue(overture_scope.is_closed_status("temporarily_closed"))

    def test_none_is_not_closed(self):
        self.assertFalse(overture_scope.is_closed_status(None))

    def test_empty_string_is_not_closed(self):
        self.assertFalse(overture_scope.is_closed_status(""))

    def test_open_is_not_closed(self):
        self.assertFalse(overture_scope.is_closed_status("open"))

    def test_open_uppercase_is_not_closed(self):
        self.assertFalse(overture_scope.is_closed_status("OPEN"))


class AltFuelOnlyTests(SimpleTestCase):
    def test_cng_alternative_energy_entry_is_alt_fuel_only(self):
        self.assertTrue(
            overture_scope.is_alt_fuel_only("cng-love's alternative energy")
        )

    def test_conventional_loves_travel_stop_is_not_alt_fuel_only(self):
        self.assertFalse(overture_scope.is_alt_fuel_only("Love's Travel Stop #123"))


class MojibakeTests(SimpleTestCase):
    def test_replacement_character_is_mojibake(self):
        self.assertTrue(overture_scope.has_mojibake("pilot � dunnigan, ca"))

    def test_clean_name_is_not_mojibake(self):
        self.assertFalse(overture_scope.has_mojibake("Pilot Travel Center"))


class ChainAliasSubstringTrapTests(SimpleTestCase):
    def test_atlanta_fuel_mart_does_not_match_ta(self):
        self.assertIsNone(overture_scope.chain_alias_for("ATLANTA FUEL MART"))

    def test_atlanta_alone_does_not_match_ta(self):
        self.assertIsNone(overture_scope.chain_alias_for("ATLANTA"))

    def test_ta_hash_number_matches_ta(self):
        self.assertEqual(overture_scope.chain_alias_for("TA #234"), "TA")

    def test_ta_petro_stopping_center_matches_ta(self):
        self.assertEqual(
            overture_scope.chain_alias_for("TA PETRO STOPPING CENTER"), "TA"
        )

    def test_loves_apostrophe_travel_stop_matches_loves(self):
        self.assertEqual(
            overture_scope.chain_alias_for("LOVE'S TRAVEL STOP #451"), "LOVES"
        )

    def test_pilot_travel_center_matches_pilot(self):
        self.assertEqual(
            overture_scope.chain_alias_for("Pilot Travel Center 0621"), "PILOT"
        )


class OvertureIdMintingTests(SimpleTestCase):
    def test_base_and_span_literals(self):
        self.assertEqual(overture_scope.OVERTURE_ID_BASE, 1_000_000_000)
        self.assertEqual(overture_scope.OVERTURE_ID_SPAN, 1_000_000_000)

    def test_max_span_value_fits_signed_int32(self):
        self.assertLess(
            overture_scope.OVERTURE_ID_BASE + overture_scope.OVERTURE_ID_SPAN - 1,
            2**31 - 1,
        )

    def test_live_sample_gers_ids_mint_deterministically(self):
        for gers_id in (
            "cfcdde8c-d160-44d4-b6a6-7f579ec12fb6",
            "832865e7-be88-4b6a-838f-b95a65f00f0f",
        ):
            with self.subTest(gers_id=gers_id):
                first = overture_scope.mint_opis_id(gers_id)
                second = overture_scope.mint_opis_id(gers_id)
                self.assertEqual(first, second)
                self.assertTrue(overture_scope.is_overture_id(first))

    def test_ten_thousand_generated_uuids_stay_in_range(self):
        rng = __import__("random").Random(20260808)
        for _ in range(10_000):
            gers_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            value = overture_scope.mint_opis_id(gers_id)
            self.assertGreaterEqual(value, overture_scope.OVERTURE_ID_BASE)
            self.assertLess(
                value,
                overture_scope.OVERTURE_ID_BASE + overture_scope.OVERTURE_ID_SPAN,
            )

    def test_is_overture_id_disjoint_from_real_opis_ids(self):
        self.assertFalse(overture_scope.is_overture_id(73_131))
        self.assertTrue(overture_scope.is_overture_id(1_999_999_999))


class OvertureIdCollisionWitnessTests(SimpleTestCase):
    def test_witness_pair_mints_to_the_same_id(self):
        gers_a, gers_b = overture_scope.COLLISION_WITNESS_PAIR
        self.assertNotEqual(gers_a, gers_b)
        self.assertEqual(
            overture_scope.mint_opis_id(gers_a),
            overture_scope.COLLISION_WITNESS_MINTED_ID,
        )
        self.assertEqual(
            overture_scope.mint_opis_id(gers_b),
            overture_scope.COLLISION_WITNESS_MINTED_ID,
        )


class NoticeFileBindingTests(SimpleTestCase):
    """NOTICE must never drift from the pinned release/licence constants --
    a future release bump that forgets to update NOTICE fails loudly here
    rather than shipping a licence file describing the wrong release."""

    def test_notice_exists(self):
        self.assertTrue(NOTICE_PATH.is_file())

    def test_notice_contains_the_pinned_release_verbatim(self):
        text = NOTICE_PATH.read_text(encoding="utf-8")
        self.assertIn(overture_scope.OVERTURE_RELEASE, text)

    def test_notice_contains_the_pinned_licence_verbatim(self):
        text = NOTICE_PATH.read_text(encoding="utf-8")
        self.assertIn(overture_scope.OVERTURE_LICENCE, text)

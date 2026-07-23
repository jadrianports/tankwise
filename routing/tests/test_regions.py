"""Tests for the state -> EIA sub-PADD region mapping, baseline/clamp band,
and dominant-region plurality helper.

Pure-module test -- no DB needed, uses SimpleTestCase mirroring
test_corridor.py's TestCase style but without the DB dependency, since
regions.py itself is dependency-free.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from routing.services import regions

# Transcribed verbatim from 12-RESEARCH.md's VERIFIED "State -> PADD Region
# Mapping" table -- the authoritative expected mapping for all 51 USPS
# jurisdictions (50 states + DC).
EXPECTED_STATE_REGION = {
    # PADD1A -- New England
    "CT": "PADD1A", "ME": "PADD1A", "MA": "PADD1A",
    "NH": "PADD1A", "RI": "PADD1A", "VT": "PADD1A",
    # PADD1B -- Central Atlantic
    "DE": "PADD1B", "DC": "PADD1B", "MD": "PADD1B",
    "NJ": "PADD1B", "NY": "PADD1B", "PA": "PADD1B",
    # PADD1C -- Lower Atlantic
    "FL": "PADD1C", "GA": "PADD1C", "NC": "PADD1C",
    "SC": "PADD1C", "VA": "PADD1C", "WV": "PADD1C",
    # PADD2 -- Midwest
    "IL": "PADD2", "IN": "PADD2", "IA": "PADD2", "KS": "PADD2",
    "KY": "PADD2", "MI": "PADD2", "MN": "PADD2", "MO": "PADD2",
    "NE": "PADD2", "ND": "PADD2", "SD": "PADD2", "OH": "PADD2",
    "OK": "PADD2", "TN": "PADD2", "WI": "PADD2",
    # PADD3 -- Gulf Coast
    "AL": "PADD3", "AR": "PADD3", "LA": "PADD3",
    "MS": "PADD3", "NM": "PADD3", "TX": "PADD3",
    # PADD4 -- Rocky Mountain
    "CO": "PADD4", "ID": "PADD4", "MT": "PADD4", "UT": "PADD4", "WY": "PADD4",
    # PADD5_EX_CA -- West Coast (excl. CA)
    "AK": "PADD5_EX_CA", "AZ": "PADD5_EX_CA", "HI": "PADD5_EX_CA",
    "NV": "PADD5_EX_CA", "OR": "PADD5_EX_CA", "WA": "PADD5_EX_CA",
    # CALIFORNIA -- carve-out from PADD 5
    "CA": "CALIFORNIA",
}


class ExhaustiveStateRegionMappingTests(SimpleTestCase):
    """D-01's "exhaustively unit-tested" guarantee: every one of the 50
    states + DC maps to exactly one region, matching the researched table."""

    def test_expected_table_has_51_entries(self):
        self.assertEqual(len(EXPECTED_STATE_REGION), 51)

    def test_every_jurisdiction_maps_to_expected_region(self):
        for state, expected_region in EXPECTED_STATE_REGION.items():
            with self.subTest(state=state):
                self.assertEqual(regions.region_for_state(state), expected_region)

    def test_all_51_jurisdictions_partition_across_8_regions_no_overlap(self):
        seen = set()
        for region, states in regions.REGION_STATES.items():
            with self.subTest(region=region):
                overlap = seen & states
                self.assertFalse(
                    overlap, f"states {overlap} appear in more than one region"
                )
                seen |= states
        self.assertEqual(seen, set(EXPECTED_STATE_REGION.keys()))

    def test_every_region_code_is_one_of_the_8_known_codes(self):
        self.assertEqual(set(regions.REGION_STATES.keys()), set(regions.ALL_REGION_CODES))
        self.assertEqual(len(regions.ALL_REGION_CODES), 8)


class NeutralFallbackTests(SimpleTestCase):
    """D-03: unknown/foreign/blank state codes degrade to None (caller
    applies the neutral factor 1.0), never raise."""

    def test_unknown_code_returns_none(self):
        self.assertIsNone(regions.region_for_state("ZZ"))

    def test_foreign_code_returns_none(self):
        self.assertIsNone(regions.region_for_state("ON"))  # Ontario, Canada

    def test_empty_string_returns_none(self):
        self.assertIsNone(regions.region_for_state(""))

    def test_none_input_returns_none(self):
        self.assertIsNone(regions.region_for_state(None))

    def test_lowercased_valid_code_resolves_correctly(self):
        self.assertEqual(regions.region_for_state("ca"), "CALIFORNIA")
        self.assertEqual(regions.region_for_state("il"), "PADD2")

    def test_whitespace_padded_valid_code_resolves_correctly(self):
        self.assertEqual(regions.region_for_state(" CA "), "CALIFORNIA")


class ClampFactorTests(SimpleTestCase):
    """Claude's Discretion: sanity clamp band [0.5, 2.0]. Out-of-band
    factors return None so the caller treats them as corrupt."""

    def test_factor_of_one_passes_through(self):
        self.assertEqual(regions.clamp_factor(Decimal("1.0")), Decimal("1.0"))

    def test_below_band_returns_none(self):
        self.assertIsNone(regions.clamp_factor(Decimal("0.4")))

    def test_above_band_returns_none(self):
        self.assertIsNone(regions.clamp_factor(Decimal("2.5")))

    def test_lower_band_edge_passes_through(self):
        self.assertEqual(regions.clamp_factor(Decimal("0.5")), Decimal("0.5"))

    def test_upper_band_edge_passes_through(self):
        self.assertEqual(regions.clamp_factor(Decimal("2.0")), Decimal("2.0"))


class DominantRegionTests(SimpleTestCase):
    """Pure core of D-06's route-dominant-region selection: plurality
    wins; ties resolve toward the earliest-occurring region; empty -> None."""

    def test_plurality_wins(self):
        self.assertEqual(
            regions.dominant_region(["PADD2", "PADD2", "PADD3"]), "PADD2"
        )

    def test_tie_resolves_to_earliest_occurring(self):
        self.assertEqual(
            regions.dominant_region(["PADD3", "PADD2", "PADD3", "PADD2"]), "PADD3"
        )

    def test_empty_list_returns_none(self):
        self.assertIsNone(regions.dominant_region([]))

    def test_single_element_returns_that_region(self):
        self.assertEqual(regions.dominant_region(["CALIFORNIA"]), "CALIFORNIA")


class BaselineValuesTests(SimpleTestCase):
    """D-05: baseline denominators are committed Decimal constants with
    provenance, one per region, never placeholders."""

    def test_every_region_has_a_decimal_baseline(self):
        for region in regions.ALL_REGION_CODES:
            with self.subTest(region=region):
                self.assertIn(region, regions.BASELINE_VALUES)
                self.assertIsInstance(regions.BASELINE_VALUES[region], Decimal)
                self.assertGreater(regions.BASELINE_VALUES[region], Decimal("0"))

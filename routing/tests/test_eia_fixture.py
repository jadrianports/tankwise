"""Structural guard for the committed EIA v2 response fixture.

The fixture at `fixtures/eia_response.json` is a real, unedited capture
from `api.eia.gov/v2/petroleum/pri/gnd/data/` -- not a hand-invented
payload. This test asserts its shape so a later accidental edit, or a
wrong-shaped re-capture, fails loudly instead of silently mis-seeding
every downstream test (routing.services.eia._parse_eia_response, Plan
12-03, relies on this exact shape).
"""
import json
from pathlib import Path

from django.test import SimpleTestCase

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "eia_response.json"
)
with open(FIXTURE_PATH, encoding="utf-8") as f:
    FIXTURE = json.load(f)

EXPECTED_REGIONS = {"R1X", "R1Y", "R1Z", "R20", "R30", "R40", "R5XCA", "SCA"}
REQUIRED_ROW_KEYS = {"period", "duoarea", "product", "value"}


class EiaFixtureShapeTests(SimpleTestCase):
    """The captured payload has the response.data[] shape
    routing.services.eia._parse_eia_response will read."""

    def test_response_data_is_non_empty_list(self):
        self.assertIn("response", FIXTURE)
        self.assertIn("data", FIXTURE["response"])
        data = FIXTURE["response"]["data"]
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)

    def test_every_row_has_required_keys(self):
        for row in FIXTURE["response"]["data"]:
            self.assertIsInstance(row, dict)
            self.assertTrue(REQUIRED_ROW_KEYS.issubset(row.keys()))

    def test_all_eight_regions_present(self):
        seen_regions = {row["duoarea"] for row in FIXTURE["response"]["data"]}
        self.assertEqual(seen_regions, EXPECTED_REGIONS)

    def test_padd4_region_carries_epd2dxl0_product(self):
        """PADD 4 (R40) only publishes a ULSD series -- RESEARCH.md
        Pitfall 1. The captured fixture's R40 rows must include the
        EPD2DXL0 product code (it may also appear under EPD2D, per the
        live-call finding that PADD 4 is duplicated across both codes
        in the latest week -- that duplication is fine; EPD2DXL0 simply
        must not be absent)."""
        r40_products = {
            row["product"]
            for row in FIXTURE["response"]["data"]
            if row["duoarea"] == "R40"
        }
        self.assertIn("EPD2DXL0", r40_products)

    def test_no_raw_api_key_present(self):
        """The fixture must never carry live key material -- only the
        redacted placeholder is acceptable in the echoed request block."""
        request_block = FIXTURE.get("request", {})
        api_key = request_block.get("params", {}).get("api_key")
        if api_key is not None:
            self.assertEqual(api_key, "REDACTED")

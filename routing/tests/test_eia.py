"""Tests for the EIA regional diesel factor client.

The transport boundary (`routing.services.eia._SESSION.get`) is always
mocked -- no live network call is ever performed. The happy-path parser
is exercised against a recorded-shape fixture reproducing a real,
sanitized EIA v2 `petroleum/pri/gnd` response (Plan 12-01). Every
fallback branch (cooldown, timeout, never-fetched, missing-key,
clamp-reject) is exercised against synthetic payloads built inline,
since the committed fixture captures only a single week.
"""
import copy
import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

import requests
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from routing.services import eia, regions

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "eia_response.json"
with open(FIXTURE_PATH, encoding="utf-8") as f:
    FIXTURE = json.load(f)


class _StubResponse:
    """Minimal stand-in for a `requests.Response`."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else FIXTURE

    def json(self):
        return self._payload


def _row(period, duoarea, product, value):
    return {
        "period": period,
        "duoarea": duoarea,
        "product": product,
        "value": value,
    }


def _two_week_payload():
    """A synthetic two-period payload covering every region under its
    confirmed (duoarea, product) facet pair, so week-over-week delta
    math can be exercised without depending on the single-week fixture.
    Current week is a flat +2 cents over the prior week for every
    region."""
    rows = []
    for region in regions.ALL_REGION_CODES:
        duoarea = regions.REGION_DUOAREA[region]
        product = regions.REGION_PRODUCT[region]
        baseline = regions.BASELINE_VALUES[region]
        prior_value = baseline  # factor 1.0 at the prior week
        current_value = baseline + Decimal("0.02")
        rows.append(_row("2026-07-13", duoarea, product, str(prior_value)))
        rows.append(_row("2026-07-20", duoarea, product, str(current_value)))
    return {"response": {"data": rows}}


class EiaTestCase(SimpleTestCase):
    """Base class clearing the Django cache in setUp so the two-key
    cache-aside state never leaks across test cases."""

    def setUp(self):
        super().setUp()
        cache.clear()


@override_settings(EIA_API_KEY="test-key")
class HappyPathTests(EiaTestCase):
    """get_factor_table() resolves a "current" table in exactly one call."""

    def test_returns_current_status_with_exactly_one_call(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            table, status = eia.get_factor_table()

        mock_get.assert_called_once()
        self.assertEqual(status, "current")

    def test_week_matches_fixture_most_recent_period(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            table, _status = eia.get_factor_table()

        fixture_periods = {
            row["period"] for row in FIXTURE["response"]["data"]
        }
        self.assertEqual(table["week"], max(fixture_periods))

    def test_factors_are_decimal_for_every_region(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            table, _status = eia.get_factor_table()

        self.assertEqual(len(table["factors"]), len(regions.ALL_REGION_CODES))
        for factor in table["factors"].values():
            self.assertIsInstance(factor, Decimal)

    def test_params_carry_key_never_interpolated_into_url(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            eia.get_factor_table()

        args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["api_key"], "test-key")
        self.assertNotIn("test-key", args[0])


@override_settings(EIA_API_KEY="test-key")
class NoPerRequestCallTests(EiaTestCase):
    """A second get_factor_table() call reads the `current` cache and
    never re-issues the EIA HTTP call (EIA-01)."""

    def test_second_call_does_not_call_transport_again(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            eia.get_factor_table()
            eia.get_factor_table()

        mock_get.assert_called_once()

    def test_second_call_returns_current_status(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            eia.get_factor_table()
            _table, status = eia.get_factor_table()

        self.assertEqual(status, "current")


@override_settings(EIA_API_KEY="test-key")
class CooldownSkipTests(EiaTestCase):
    """D-18: a pre-set cooldown marker skips the transport call
    entirely, returning stale (if last-known exists) or frozen."""

    def test_cooldown_with_last_known_skips_transport_and_returns_stale(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            seeded_table, _ = eia.get_factor_table()
        cache.delete(eia.CURRENT_KEY)
        cache.set(eia.COOLDOWN_KEY, True, timeout=eia.COOLDOWN_TTL)

        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            table, status = eia.get_factor_table()

        mock_get.assert_not_called()
        self.assertEqual(status, "stale")
        self.assertEqual(table, seeded_table)

    def test_cooldown_with_no_last_known_returns_frozen(self):
        cache.set(eia.COOLDOWN_KEY, True, timeout=eia.COOLDOWN_TTL)

        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            table, status = eia.get_factor_table()

        mock_get.assert_not_called()
        self.assertEqual(status, "frozen")
        self.assertEqual(table["factors"], {})


@override_settings(EIA_API_KEY="test-key")
class TimeoutFallbackTests(EiaTestCase):
    """D-13/D-15: a transport failure after a prior success degrades to
    last-known ("stale") and sets the cooldown marker."""

    def test_transport_failure_after_success_returns_stale_from_last_known(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            seeded_table, _ = eia.get_factor_table()
        cache.delete(eia.CURRENT_KEY)  # force the next call past the TTL'd read

        with mock.patch(
            "routing.services.eia._SESSION.get",
            side_effect=requests.RequestException("timed out"),
        ):
            table, status = eia.get_factor_table()

        self.assertEqual(status, "stale")
        self.assertEqual(table, seeded_table)

    def test_transport_failure_sets_cooldown_marker(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            eia.get_factor_table()
        cache.delete(eia.CURRENT_KEY)

        with mock.patch(
            "routing.services.eia._SESSION.get",
            side_effect=requests.RequestException("timed out"),
        ):
            eia.get_factor_table()

        self.assertTrue(cache.get(eia.COOLDOWN_KEY))

    def test_non_200_status_also_degrades_to_stale(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            seeded_table, _ = eia.get_factor_table()
        cache.delete(eia.CURRENT_KEY)

        with mock.patch(
            "routing.services.eia._SESSION.get",
            return_value=_StubResponse(status_code=500),
        ):
            table, status = eia.get_factor_table()

        self.assertEqual(status, "stale")
        self.assertEqual(table, seeded_table)


@override_settings(EIA_API_KEY="test-key")
class NeverFetchedFrozenTests(EiaTestCase):
    """D-15: an empty cache plus a failing transport degrades to the
    frozen 1.0 table -- never raises."""

    def test_never_fetched_and_transport_fails_returns_frozen(self):
        with mock.patch(
            "routing.services.eia._SESSION.get",
            side_effect=requests.RequestException("down"),
        ) as mock_get:
            table, status = eia.get_factor_table()

        mock_get.assert_called_once()
        self.assertEqual(status, "frozen")
        self.assertEqual(table["factors"], {})
        self.assertEqual(table["deltas_cents"], {})

    def test_frozen_table_lookup_yields_neutral_factor_for_every_region(self):
        with mock.patch(
            "routing.services.eia._SESSION.get",
            side_effect=requests.RequestException("down"),
        ):
            table, _status = eia.get_factor_table()

        factor_for = eia.make_factor_lookup(table)
        for states in regions.REGION_STATES.values():
            for state in states:
                self.assertEqual(factor_for(state), Decimal("1"))


class MissingKeyFrozenTests(EiaTestCase):
    """D-20: an unset EIA_API_KEY degrades to frozen and never raises
    ImproperlyConfigured to the caller."""

    @override_settings(EIA_API_KEY=None)
    def test_missing_key_returns_frozen_without_raising(self):
        with mock.patch("routing.services.eia._SESSION.get") as mock_get:
            table, status = eia.get_factor_table()

        mock_get.assert_not_called()
        self.assertEqual(status, "frozen")
        self.assertEqual(table["factors"], {})

    @override_settings(EIA_API_KEY=None)
    def test_fetch_current_week_itself_still_raises_improperly_configured(self):
        with self.assertRaises(ImproperlyConfigured):
            eia.fetch_current_week()


@override_settings(EIA_API_KEY="test-key")
class ClampRejectTests(EiaTestCase):
    """Claude's Discretion: a region whose current/baseline ratio falls
    outside the sanity-clamp band is dropped from `factors` (and
    `deltas_cents`) entirely -- neutral 1.0 applies via
    make_factor_lookup -- while in-band regions are unaffected."""

    def _payload_with_out_of_band_region(self, region):
        payload = copy.deepcopy(FIXTURE)
        duoarea = regions.REGION_DUOAREA[region]
        product = regions.REGION_PRODUCT[region]
        for row in payload["response"]["data"]:
            if row["duoarea"] == duoarea and row["product"] == product:
                # 10x the baseline is far outside the [0.5, 2.0] clamp band.
                row["value"] = str(regions.BASELINE_VALUES[region] * 10)
        return payload

    def test_out_of_band_region_absent_from_factors(self):
        payload = self._payload_with_out_of_band_region("PADD3")
        with mock.patch(
            "routing.services.eia._SESSION.get",
            return_value=_StubResponse(payload=payload),
        ):
            table, status = eia.get_factor_table()

        self.assertEqual(status, "current")
        self.assertNotIn("PADD3", table["factors"])
        self.assertNotIn("PADD3", table["deltas_cents"])

    def test_in_band_regions_unaffected_by_a_corrupt_sibling_region(self):
        payload = self._payload_with_out_of_band_region("PADD3")
        with mock.patch(
            "routing.services.eia._SESSION.get",
            return_value=_StubResponse(payload=payload),
        ):
            table, _status = eia.get_factor_table()

        for region in regions.ALL_REGION_CODES:
            if region == "PADD3":
                continue
            self.assertIn(region, table["factors"])

    def test_out_of_band_region_falls_back_to_neutral_factor(self):
        payload = self._payload_with_out_of_band_region("PADD3")
        with mock.patch(
            "routing.services.eia._SESSION.get",
            return_value=_StubResponse(payload=payload),
        ):
            table, _status = eia.get_factor_table()

        factor_for = eia.make_factor_lookup(table)
        self.assertEqual(factor_for("TX"), Decimal("1"))  # TX is PADD3


@override_settings(EIA_API_KEY="test-key")
class DecimalExactnessTests(EiaTestCase):
    """D-04: a parsed factor multiplied by a sample retail price stays
    an exact Decimal -- no binary-float noise."""

    def test_factor_times_price_stays_exact_decimal(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            table, _status = eia.get_factor_table()

        factor_for = eia.make_factor_lookup(table)
        sample_price = Decimal("3.459")
        result = sample_price * factor_for("CA")

        self.assertIsInstance(result, Decimal)
        expected = sample_price * table["factors"]["CALIFORNIA"]
        self.assertEqual(result, expected)


@override_settings(EIA_API_KEY="test-key")
class WeekOverWeekDeltaTests(EiaTestCase):
    """The parser computes a signed week-over-week delta (in cents) from
    the two most recent periods per region, and degrades gracefully to
    delta 0 when only one period is observed (the committed fixture's
    single-week case is covered by HappyPathTests)."""

    def test_two_week_payload_yields_two_cent_delta_for_every_region(self):
        with mock.patch(
            "routing.services.eia._SESSION.get",
            return_value=_StubResponse(payload=_two_week_payload()),
        ):
            table, _status = eia.get_factor_table()

        for region in regions.ALL_REGION_CODES:
            self.assertEqual(table["deltas_cents"][region], 2)

    def test_single_period_region_reports_zero_delta(self):
        with mock.patch(
            "routing.services.eia._SESSION.get", return_value=_StubResponse()
        ):
            table, _status = eia.get_factor_table()

        for region in regions.ALL_REGION_CODES:
            self.assertEqual(table["deltas_cents"][region], 0)

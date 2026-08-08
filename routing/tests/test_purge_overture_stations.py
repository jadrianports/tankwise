"""Tests for the purge_overture_stations rollback command (plan 22-16, D-40).

`PurgeOvertureStationsTests` covers every behaviour in the plan's <behavior>
block: dry-run-by-default, --confirm actually deleting, the OPIS row count
staying unchanged, a no-op on a table with no Overture rows, the corridor
index / dataset-vintage token reset, and -- the assertion that proves the
rolled-back state is a VALID one, not merely a smaller one -- that
verify_stations still exits 0 against the OPIS-only remainder.
"""

import io
from decimal import Decimal
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from routing.models import GeocodeStatus, Station, StationSource


def _make_station(opis_id, source, **overrides):
    defaults = dict(
        opis_id=opis_id,
        name=f"Station {opis_id}",
        address="123 Main St",
        city="Springfield",
        state="MO",
        rack_id="R1",
        retail_price=Decimal("3.25900000"),
        observation_count=1,
        price_min=Decimal("3.25900000"),
        price_max=Decimal("3.25900000"),
        geocode_status=GeocodeStatus.OK,
        latitude=Decimal("37.21530000"),
        longitude=Decimal("-93.29820000"),
        geocode_precision="rooftop",
        source=source,
    )
    defaults.update(overrides)
    return Station.objects.create(**defaults)


class PurgeOvertureStationsTests(TestCase):
    def setUp(self):
        self.opis_station = _make_station(1001, StationSource.OPIS)
        self.overture_station_a = _make_station(
            1_000_000_001,
            StationSource.OVERTURE,
            gers_id="08f2828badea6a7203c3d3cc402d1234",
        )
        self.overture_station_b = _make_station(
            1_000_000_002,
            StationSource.OVERTURE,
            gers_id="08f2828badea6a7203c3d3cc402d5678",
        )

    def test_dry_run_without_confirm_deletes_nothing_and_exits_0(self):
        out = io.StringIO()
        call_command("purge_overture_stations", stdout=out)

        self.assertEqual(Station.objects.count(), 3)
        self.assertEqual(
            Station.objects.filter(source=StationSource.OVERTURE).count(), 2
        )
        self.assertIn("Dry run", out.getvalue())
        self.assertIn("2", out.getvalue())

    def test_confirm_deletes_exactly_the_overture_rows_and_reports_count(self):
        out = io.StringIO()
        call_command("purge_overture_stations", "--confirm", stdout=out)

        self.assertEqual(
            Station.objects.filter(source=StationSource.OVERTURE).count(), 0
        )
        self.assertIn("Deleted 2 Overture-sourced row(s)", out.getvalue())

    def test_opis_row_count_is_unchanged_after_a_purge(self):
        opis_before = Station.objects.filter(source=StationSource.OPIS).count()

        call_command("purge_overture_stations", "--confirm", stdout=io.StringIO())

        opis_after = Station.objects.filter(source=StationSource.OPIS).count()
        self.assertEqual(opis_before, opis_after)
        self.assertTrue(Station.objects.filter(pk=self.opis_station.pk).exists())

    def test_purge_on_table_with_no_overture_rows_reports_zero_and_exits_0(self):
        Station.objects.filter(source=StationSource.OVERTURE).delete()

        out = io.StringIO()
        call_command("purge_overture_stations", "--confirm", stdout=out)

        self.assertIn("Deleted 0 Overture-sourced row(s)", out.getvalue())
        self.assertIn("No Overture-sourced rows found", out.getvalue())
        # OPIS row must survive a no-op purge untouched.
        self.assertTrue(Station.objects.filter(pk=self.opis_station.pk).exists())

    def test_confirmed_purge_resets_corridor_index_and_dataset_vintage_token(self):
        with (
            mock.patch(
                "routing.management.commands.purge_overture_stations.reset_index"
            ) as mock_reset_index,
            mock.patch(
                "routing.management.commands.purge_overture_stations."
                "reset_dataset_vintage_token"
            ) as mock_reset_token,
        ):
            call_command("purge_overture_stations", "--confirm", stdout=io.StringIO())

        mock_reset_index.assert_called_once()
        mock_reset_token.assert_called_once()

    def test_dry_run_does_not_reset_corridor_index_or_dataset_vintage_token(self):
        with (
            mock.patch(
                "routing.management.commands.purge_overture_stations.reset_index"
            ) as mock_reset_index,
            mock.patch(
                "routing.management.commands.purge_overture_stations."
                "reset_dataset_vintage_token"
            ) as mock_reset_token,
        ):
            call_command("purge_overture_stations", stdout=io.StringIO())

        mock_reset_index.assert_not_called()
        mock_reset_token.assert_not_called()

    def test_verify_stations_still_exits_0_after_a_purge(self):
        call_command("purge_overture_stations", "--confirm", stdout=io.StringIO())

        # A CommandError means non-zero exit; call_command re-raises it, so
        # "no exception" is exactly "exit 0" for this command.
        try:
            call_command("verify_stations", stdout=io.StringIO())
        except CommandError as exc:  # pragma: no cover - failure path
            self.fail(f"verify_stations raised CommandError after purge: {exc}")

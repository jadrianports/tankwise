"""The single place the price-source provenance chain is asserted.

`price_source` travels model -> CSV -> pipeline commands -> corridor
candidate -> solver -> serializer -> UI, an eight-hop chain (D-18). Each hop
gets one named assertion class here, with both solver arms (exact DP and
penalty-aware heuristic) covered explicitly where the hop touches the
solver. This module starts with hop 1 (the model layer); later plans in
this phase add hops 2 through 8.
"""

import csv
import io
import tempfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase

from routing.management.commands.geocode_stations import EXPORT_HEADER
from routing.management.commands.seed_stations import STRAIGHT_FIELDS
from routing.models import PriceSource, Station

COMMITTED_CSV_PATH = Path(settings.BASE_DIR) / "data" / "stations_geocoded.csv"


def _make_station(opis_id, **overrides):
    fields = dict(
        opis_id=opis_id,
        name="Test Station",
        address="I-00, EXIT 1 & US-1",
        city="Anytown",
        state="OK",
        rack_id="100",
        retail_price=Decimal("3.259"),
        observation_count=1,
        price_min=Decimal("3.259"),
        price_max=Decimal("3.259"),
    )
    fields.update(overrides)
    return Station.objects.create(**fields)


class StationPriceSourceModelHopTests(TestCase):
    """Hop 1: the model layer. A `Station` created with no explicit
    provenance resolves to the recorded-price wire value; an explicit
    estimate value round-trips through `refresh_from_db()`; and the choice
    set is exactly the two wire values PROV-01 names, in order."""

    def test_default_price_source_is_opis_indexed(self):
        station = _make_station(opis_id=101)

        self.assertEqual(station.price_source, PriceSource.OPIS_INDEXED)

    def test_explicit_estimate_value_round_trips_through_refresh(self):
        station = _make_station(
            opis_id=102, price_source=PriceSource.EIA_REGIONAL_ESTIMATE
        )

        station.refresh_from_db()

        self.assertEqual(station.price_source, PriceSource.EIA_REGIONAL_ESTIMATE)

    def test_price_source_values_is_exactly_the_two_wire_values_in_order(self):
        self.assertEqual(
            PriceSource.values, ["opis_indexed", "eia_regional_estimate"]
        )


class SeedStationsPriceSourceReplayHopTests(TestCase):
    """Hop-1-adjacent: replaying the real committed CSV through
    `seed_stations` leaves every row at the recorded-price value, since the
    committed dataset carries no estimate-sourced row in this phase."""

    def test_replaying_committed_csv_leaves_every_row_opis_indexed(self):
        call_command("seed_stations", str(COMMITTED_CSV_PATH), stdout=io.StringIO())

        total = Station.objects.count()
        opis_indexed_count = Station.objects.filter(
            price_source=PriceSource.OPIS_INDEXED
        ).count()

        self.assertEqual(total, 6738)
        self.assertEqual(opis_indexed_count, 6738)


class SeedStationsMissingColumnGuardHopTests(TestCase):
    """Hop-1-adjacent: a derived CSV missing a required column must fail
    loudly with `CommandError` naming it, never silently skip every row
    (T-20-06)."""

    def test_missing_price_source_column_raises_command_error(self):
        header_without_price_source = [
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
        ]
        row = {
            "opis_id": "9001",
            "name": "No Provenance Stop",
            "address": "1 Test Rd",
            "city": "Testville",
            "state": "TX",
            "rack_id": "100",
            "retail_price": "3.10000000",
            "observation_count": "1",
            "price_min": "3.10000000",
            "price_max": "3.10000000",
            "latitude": "32.00000000",
            "longitude": "-97.00000000",
            "geocode_precision": "city",
            "geocode_status": "ok",
        }

        tmp = tempfile.NamedTemporaryFile(
            mode="w", newline="", suffix=".csv", delete=False, encoding="utf-8"
        )
        try:
            writer = csv.DictWriter(tmp, fieldnames=header_without_price_source)
            writer.writeheader()
            writer.writerow(row)
            tmp.close()

            with self.assertRaises(CommandError):
                call_command("seed_stations", tmp.name, stdout=io.StringIO())

            # Not thousands of rows silently skipped -- nothing was written.
            self.assertEqual(Station.objects.count(), 0)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


class VerifyStationsUnrecognizedPriceSourceHopTests(TestCase):
    """Hop-1-adjacent: `verify_stations` refuses to pass any row holding a
    `price_source` value outside `PriceSource.values` (D-10)."""

    def test_unrecognized_price_source_value_raises_command_error(self):
        station = _make_station(opis_id=103)
        Station.objects.filter(pk=station.pk).update(price_source="typo_value")

        with self.assertRaises(CommandError):
            call_command("verify_stations", stdout=io.StringIO())


class PipelineSchemaHopTests(TestCase):
    """Hop-1-adjacent: `EXPORT_HEADER` and `STRAIGHT_FIELDS` both carry the
    column, so a future column reorder is caught by a simple membership
    check rather than discovered at run time."""

    def test_export_header_contains_price_source(self):
        self.assertIn("price_source", EXPORT_HEADER)

    def test_straight_fields_contains_price_source(self):
        self.assertIn("price_source", STRAIGHT_FIELDS)

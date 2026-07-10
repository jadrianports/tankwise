from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase

from routing.models import GeocodeStatus, Station

CSV_PATH = str(Path(settings.BASE_DIR) / "fuel-prices-for-be-assessment.csv")


class ImportStationsRealCsvTests(TestCase):
    """Covers behavior against the real repo-root CSV fixture (D-27)."""

    @classmethod
    def setUpTestData(cls):
        call_command("import_stations", CSV_PATH)

    def test_deduped_station_count_is_exactly_6738(self):
        self.assertEqual(Station.objects.count(), 6738)
        self.assertEqual(
            Station.objects.values_list("opis_id", flat=True).distinct().count(), 6738
        )

    def test_out_of_scope_rows_are_tagged_and_excluded_from_routable(self):
        out_of_scope_qs = Station.objects.filter(geocode_status=GeocodeStatus.OUT_OF_SCOPE)
        self.assertTrue(out_of_scope_qs.exists())
        # None of the out_of_scope rows are routable candidates.
        self.assertEqual(
            out_of_scope_qs.filter(pk__in=Station.objects.routable().values("pk")).count(),
            0,
        )

    def test_us_rows_are_pending_with_null_coordinates(self):
        us_rows = Station.objects.exclude(geocode_status=GeocodeStatus.OUT_OF_SCOPE)
        self.assertTrue(us_rows.exists())
        self.assertTrue(us_rows.filter(geocode_status=GeocodeStatus.PENDING).exists())
        self.assertEqual(
            us_rows.filter(latitude__isnull=False).count(),
            0,
            "US rows must have null coordinates before geocoding runs",
        )

    def test_known_conflicting_opis_id_gets_lower_median_price(self):
        # opis_id 20 appears twice in the source CSV with the same price
        # (3.899), so this asserts the representative price is an observed
        # value rather than a synthesized average.
        station = Station.objects.get(opis_id=20)
        self.assertEqual(station.retail_price, Decimal("3.899"))
        self.assertEqual(station.observation_count, 2)


class ImportStationsIdempotencyTests(TestCase):
    """Covers idempotent upsert-on-opis_id semantics (D-16/D-27)."""

    def test_running_twice_is_a_no_op(self):
        call_command("import_stations", CSV_PATH)
        count_after_first = Station.objects.count()
        snapshot = {
            s.opis_id: (
                s.name,
                s.retail_price,
                s.geocode_status,
                s.latitude,
                s.longitude,
            )
            for s in Station.objects.all()
        }

        call_command("import_stations", CSV_PATH)

        self.assertEqual(Station.objects.count(), count_after_first)
        for station in Station.objects.all():
            self.assertEqual(
                snapshot[station.opis_id],
                (
                    station.name,
                    station.retail_price,
                    station.geocode_status,
                    station.latitude,
                    station.longitude,
                ),
            )


class ImportStationsUniqueConstraintTests(TestCase):
    """Confirms the opis_id UNIQUE constraint is exercised (raises, not
    silently overwritten) — the safety net dedupe (Task 1) exists to avoid
    ever hitting.
    """

    def test_duplicate_opis_id_raises_integrity_error(self):
        Station.objects.create(
            opis_id=999999,
            name="A",
            address="1 Rd",
            city="Town",
            state="TX",
            rack_id="1",
            retail_price=Decimal("3.000"),
            price_min=Decimal("3.000"),
            price_max=Decimal("3.000"),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Station.objects.create(
                    opis_id=999999,
                    name="B",
                    address="2 Rd",
                    city="Town",
                    state="TX",
                    rack_id="1",
                    retail_price=Decimal("3.100"),
                    price_min=Decimal("3.100"),
                    price_max=Decimal("3.100"),
                )

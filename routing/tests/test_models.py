from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase

from routing.models import GeocodeStatus, Station


def _make_station(opis_id, geocode_status, latitude=None, longitude=None):
    return Station.objects.create(
        opis_id=opis_id,
        name="Test Station",
        address="I-00, EXIT 1 & US-1",
        city="Anytown",
        state="OK",
        rack_id="100",
        retail_price=Decimal("3.259"),
        geocode_status=geocode_status,
        latitude=latitude,
        longitude=longitude,
        observation_count=1,
        price_min=Decimal("3.259"),
        price_max=Decimal("3.259"),
    )


class StationRoutableQuerySetTests(TestCase):
    def test_routable_includes_ok_station_with_coords(self):
        station = _make_station(
            opis_id=1,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("36.1234"),
            longitude=Decimal("-95.1234"),
        )

        self.assertEqual(list(Station.objects.routable()), [station])

    def test_routable_excludes_failed_station(self):
        _make_station(opis_id=2, geocode_status=GeocodeStatus.FAILED)

        self.assertEqual(Station.objects.routable().count(), 0)

    def test_routable_excludes_out_of_scope_station(self):
        _make_station(opis_id=3, geocode_status=GeocodeStatus.OUT_OF_SCOPE)

        self.assertEqual(Station.objects.routable().count(), 0)

    def test_routable_excludes_pending_station(self):
        _make_station(opis_id=4, geocode_status=GeocodeStatus.PENDING)

        self.assertEqual(Station.objects.routable().count(), 0)


class StationOpisIdUniqueTests(TestCase):
    def test_duplicate_opis_id_raises_integrity_error(self):
        _make_station(opis_id=5, geocode_status=GeocodeStatus.PENDING)

        with self.assertRaises(IntegrityError):
            _make_station(opis_id=5, geocode_status=GeocodeStatus.PENDING)

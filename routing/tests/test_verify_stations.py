import io

from django.core.management import CommandError, call_command
from django.test import TestCase

from routing.models import GeocodeStatus, Station


def _make_station(opis_id, status, precision=None, lat=None, lng=None):
    return Station.objects.create(
        opis_id=opis_id,
        name=f"Station {opis_id}",
        address="1 Test Rd",
        city="Testville",
        state="TX",
        rack_id="100",
        retail_price="3.100",
        price_min="3.100",
        price_max="3.100",
        geocode_status=status,
        geocode_precision=precision,
        latitude=lat,
        longitude=lng,
    )


class VerifyStationsTests(TestCase):
    """Fixture: 3 in-scope stations (2 routable/ok, 1 failed) + 1
    out_of_scope station -- coverage over in-scope rows is 2/3 (~0.667),
    which must NOT be diluted to 2/4 by counting the out_of_scope row
    (D-19)."""

    def setUp(self):
        _make_station(1, GeocodeStatus.OK, "city", "32.0", "-97.0")
        _make_station(2, GeocodeStatus.OK, "rooftop", "35.0", "-97.5")
        _make_station(3, GeocodeStatus.FAILED)
        _make_station(4, GeocodeStatus.OUT_OF_SCOPE)

    def test_no_flag_reports_and_exits_zero(self):
        out = io.StringIO()
        call_command("verify_stations", stdout=out)
        output = out.getvalue()
        self.assertIn("2/3", output)
        self.assertIn("rooftop=1 city=1 failed=1 out_of_scope=1", output)

    def test_min_coverage_below_actual_exits_zero(self):
        out = io.StringIO()
        # actual coverage is 2/3 (~0.667); 0.5 is below actual -> passes
        call_command("verify_stations", "--min-coverage", "0.5", stdout=out)
        self.assertIn("meets --min-coverage", out.getvalue())

    def test_min_coverage_above_actual_raises_command_error(self):
        # actual coverage is 2/3 (~0.667); 0.99 is above actual -> fails
        with self.assertRaises(CommandError):
            call_command("verify_stations", "--min-coverage", "0.99", stdout=io.StringIO())

    def test_coverage_denominator_excludes_out_of_scope(self):
        # With 1 out_of_scope + 3 in-scope (2 ok, 1 failed), denominator
        # must be 3, not 4.
        out = io.StringIO()
        call_command("verify_stations", stdout=out)
        output = out.getvalue()
        self.assertIn("2/3", output)
        self.assertNotIn("2/4", output)

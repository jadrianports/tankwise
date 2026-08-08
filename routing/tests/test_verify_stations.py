import io
from decimal import Decimal

from django.core.management import CommandError, call_command
from django.test import TestCase

from routing.models import GeocodeStatus, PriceSource, Station, StationSource
from routing.pipeline.overture_scope import OVERTURE_ID_RANGE
from routing.services import regions


def _make_station(
    opis_id,
    status,
    precision=None,
    lat=None,
    lng=None,
    *,
    source=StationSource.OPIS,
    gers_id=None,
    price_source=PriceSource.OPIS_INDEXED,
    retail_price="3.100",
    state="TX",
):
    return Station.objects.create(
        opis_id=opis_id,
        name=f"Station {opis_id}",
        address="1 Test Rd",
        city="Testville",
        state=state,
        rack_id="100",
        retail_price=retail_price,
        price_min=retail_price,
        price_max=retail_price,
        geocode_status=status,
        geocode_precision=precision,
        latitude=lat,
        longitude=lng,
        source=source,
        gers_id=gers_id,
        price_source=price_source,
    )


class VerifyStationsTests(TestCase):
    """Fixture: 3 in-scope stations (2 routable/ok, 1 failed) + 1
    out_of_scope station -- coverage over in-scope rows is 2/3 (~0.667),
    which must NOT be diluted to 2/4 by counting the out_of_scope row."""

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


# A valid Overture opis_id and a valid OPIS opis_id, reused across the D-42
# invariant tests below.
_VALID_OVERTURE_OPIS_ID = OVERTURE_ID_RANGE[0] + 12345
_VALID_OPIS_OPIS_ID = 60001

# TX -> PADD3, whose committed EIA baseline is Decimal("3.196")
# (routing/services/regions.py). Used to build both a correctly-priced
# estimate row (invariant 4's all-pass case) and a deliberately mismatched
# one (invariant 4's violating case).
_TX_REGION = regions.region_for_state("TX")
_TX_BASELINE = regions.BASELINE_VALUES[_TX_REGION]


class VerifyStationsDatasetInvariantTests(TestCase):
    """D-42 (plan 22-13): the four dataset invariants over
    `source`/`gers_id`/`opis_id`/`retail_price`, each of which must run
    UNCONDITIONALLY -- with no `--min-coverage` argument supplied, exactly
    like the pre-existing `price_source` gate above. Every violating-case
    test below deliberately omits `--min-coverage` for that reason; the
    dedicated `test_each_invariant_fires_with_no_min_coverage_flag` method
    makes that discipline explicit as its own guard, so a future refactor
    that accidentally gates one of these four behind the flag is caught
    even if a violating fixture happens to also pass `--min-coverage`.
    """

    def test_unknown_source_value_raises_naming_the_count(self):
        _make_station(101, GeocodeStatus.OK, "city", "32.0", "-97.0", source="bogus")

        with self.assertRaises(CommandError) as ctx:
            call_command("verify_stations", stdout=io.StringIO())
        self.assertIn("1", str(ctx.exception))
        self.assertIn("source", str(ctx.exception))

    def test_overture_row_with_blank_gers_id_raises(self):
        _make_station(
            _VALID_OVERTURE_OPIS_ID,
            GeocodeStatus.OK,
            "rooftop",
            "32.0",
            "-97.0",
            source=StationSource.OVERTURE,
            gers_id=None,
        )

        with self.assertRaises(CommandError) as ctx:
            call_command("verify_stations", stdout=io.StringIO())
        self.assertIn("1", str(ctx.exception))
        self.assertIn("gers_id", str(ctx.exception))

    def test_overture_row_with_id_outside_reserved_span_raises(self):
        _make_station(
            _VALID_OPIS_OPIS_ID,  # a normal, low, OPIS-range id
            GeocodeStatus.OK,
            "rooftop",
            "32.0",
            "-97.0",
            source=StationSource.OVERTURE,
            gers_id="a73d1c9a-91d9-4121-a260-62d9b44284d4",
        )

        with self.assertRaises(CommandError) as ctx:
            call_command("verify_stations", stdout=io.StringIO())
        self.assertIn("1", str(ctx.exception))
        self.assertIn("opis_id", str(ctx.exception))

    def test_opis_row_with_id_inside_reserved_span_raises(self):
        _make_station(
            _VALID_OVERTURE_OPIS_ID,  # an id inside OVERTURE_ID_RANGE
            GeocodeStatus.OK,
            "city",
            "32.0",
            "-97.0",
            source=StationSource.OPIS,
        )

        with self.assertRaises(CommandError) as ctx:
            call_command("verify_stations", stdout=io.StringIO())
        self.assertIn("1", str(ctx.exception))
        self.assertIn("opis_id", str(ctx.exception))

    def test_estimate_priced_row_mismatched_baseline_raises(self):
        # A well-formed OPIS row (so invariants 1-3 all pass cleanly) whose
        # price_source claims EIA_REGIONAL_ESTIMATE but whose retail_price
        # does not exactly equal its state's region baseline -- invariant
        # 4's own violating case, isolated from the other three.
        _make_station(
            _VALID_OPIS_OPIS_ID,
            GeocodeStatus.OK,
            "city",
            "32.0",
            "-97.0",
            source=StationSource.OPIS,
            price_source=PriceSource.EIA_REGIONAL_ESTIMATE,
            retail_price=str(_TX_BASELINE + Decimal("0.01")),
            state="TX",
        )

        with self.assertRaises(CommandError) as ctx:
            call_command("verify_stations", stdout=io.StringIO())
        self.assertIn("1", str(ctx.exception))
        self.assertIn("retail_price", str(ctx.exception))

    def test_all_four_invariants_pass_against_a_correctly_seeded_table(self):
        # A well-formed OPIS row, a well-formed Overture row (valid
        # gers_id, id inside the reserved span), and a well-formed
        # estimate-priced row (retail_price exactly equal to its state's
        # region baseline) -- every invariant's positive path exercised at
        # once, not just the trivially-empty "zero rows of that kind"
        # case.
        _make_station(
            _VALID_OPIS_OPIS_ID,
            GeocodeStatus.OK,
            "city",
            "32.0",
            "-97.0",
            source=StationSource.OPIS,
        )
        _make_station(
            _VALID_OVERTURE_OPIS_ID,
            GeocodeStatus.OK,
            "rooftop",
            "33.0",
            "-97.5",
            source=StationSource.OVERTURE,
            gers_id="a73d1c9a-91d9-4121-a260-62d9b44284d4",
        )
        _make_station(
            OVERTURE_ID_RANGE[0] + 54321,
            GeocodeStatus.OK,
            "rooftop",
            "34.0",
            "-98.0",
            source=StationSource.OVERTURE,
            gers_id="2df04625-917d-4309-9b98-c903ececdc32",
            price_source=PriceSource.EIA_REGIONAL_ESTIMATE,
            retail_price=str(_TX_BASELINE),
            state="TX",
        )

        out = io.StringIO()
        call_command("verify_stations", stdout=out)
        output = out.getvalue()

        self.assertIn("Source breakdown:", output)
        self.assertIn("Overture row check:", output)
        self.assertIn("OPIS id-range check:", output)
        self.assertIn("Price-basis check (D-11): checked=1 mismatched=0", output)

    def test_each_invariant_fires_with_no_min_coverage_flag(self):
        """The unconditional-discipline guard (D-42): each violating
        fixture below is checked with NO `--min-coverage` argument at
        all -- proving these four invariants cannot be silently gated
        behind the coverage flag the way a future refactor might
        accidentally introduce."""
        cases = {
            "unknown_source": lambda: _make_station(
                201, GeocodeStatus.OK, "city", "32.0", "-97.0", source="bogus"
            ),
            "overture_blank_gers_id": lambda: _make_station(
                OVERTURE_ID_RANGE[0] + 1,
                GeocodeStatus.OK,
                "rooftop",
                "32.0",
                "-97.0",
                source=StationSource.OVERTURE,
                gers_id=None,
            ),
            "overture_id_out_of_range": lambda: _make_station(
                202,
                GeocodeStatus.OK,
                "rooftop",
                "32.0",
                "-97.0",
                source=StationSource.OVERTURE,
                gers_id="a73d1c9a-91d9-4121-a260-62d9b44284d4",
            ),
            "opis_id_in_reserved_span": lambda: _make_station(
                OVERTURE_ID_RANGE[0] + 2,
                GeocodeStatus.OK,
                "city",
                "32.0",
                "-97.0",
                source=StationSource.OPIS,
            ),
            "estimate_price_mismatched": lambda: _make_station(
                203,
                GeocodeStatus.OK,
                "city",
                "32.0",
                "-97.0",
                source=StationSource.OPIS,
                price_source=PriceSource.EIA_REGIONAL_ESTIMATE,
                retail_price=str(_TX_BASELINE + Decimal("0.02")),
                state="TX",
            ),
        }

        for label, build_violation in cases.items():
            with self.subTest(invariant=label):
                station = build_violation()
                try:
                    with self.assertRaises(CommandError):
                        # No --min-coverage argument supplied anywhere in
                        # this call.
                        call_command("verify_stations", stdout=io.StringIO())
                finally:
                    station.delete()

"""The single place the price-source provenance chain is asserted.

`price_source` travels model -> CSV -> pipeline commands -> corridor
candidate -> solver -> serializer -> UI, an eight-hop chain (D-18). Each hop
gets one named assertion class here, with both solver arms (exact DP and
penalty-aware heuristic) covered explicitly where the hop touches the
solver. This module starts with hop 1 (the model layer); later plans in
this phase add hops 2 through 8.
"""

from decimal import Decimal

from django.test import TestCase

from routing.models import PriceSource, Station


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

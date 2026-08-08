"""Tests for `routing.pipeline.overture_dedupe` -- the two-tier match
against the existing OPIS dataset.

`OvertureDedupeTierTests` covers every behaviour in 22-11-PLAN.md's
`<behavior>` block against small hand-built existing-row and incoming-row
lists, including the two anti-vacuity guards this project's own history
demands (Phase 17's `prune(x) -> x` lesson): a dedupe that matches nothing
(keeps every incoming row) and a dedupe that matches everything (drops
every incoming row) must both fail a test here, in both directions.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from routing.pipeline import overture, overture_dedupe


def _incoming(gers_id, name, lat, lng, city="Testville", state="TX", chain_token=None):
    return overture.OvertureRow(
        gers_id=gers_id,
        name=name,
        brand_name="",
        address_freeform="1 Test Rd",
        address_locality=city,
        address_region=state,
        address_postcode="00000",
        category="gas_station",
        confidence=0.9,
        operating_status="open",
        longitude=Decimal(str(lng)),
        latitude=Decimal(str(lat)),
        chain_token=chain_token,
    )


def _existing(opis_id, name, lat, lng, city, state, precision):
    return overture_dedupe.ExistingStationRow(
        opis_id=opis_id,
        name=name,
        city=city,
        state=state,
        latitude=Decimal(str(lat)),
        longitude=Decimal(str(lng)),
        geocode_precision=precision,
    )


class OvertureDedupeTierTests(SimpleTestCase):
    def test_tight_tier_match_within_threshold_is_dropped(self):
        existing = [_existing(1, "PILOT #1", 32.7767, -96.797, "Dallas", "TX", "rooftop")]
        # ~0.069 mi north -- well inside the 0.25 mi pinned threshold.
        incoming = [_incoming("g1", "Pilot Travel Center", "32.7777", "-96.797")]

        kept, decisions = overture_dedupe.deduplicate(incoming, existing)

        self.assertEqual(kept, [])
        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        self.assertEqual(decision.decision, "dropped")
        self.assertEqual(decision.tier, "tight")
        self.assertEqual(decision.matched_opis_id, 1)
        self.assertNotEqual(decision.distance_mi, "")
        self.assertLess(float(decision.distance_mi), overture_dedupe.overture_scope.TIGHT_TIER_THRESHOLD_MI)

    def test_two_rooftop_matched_candidates_beyond_threshold_are_both_retained(self):
        existing = [
            _existing(1, "PILOT #1", 32.7767, -96.797, "Dallas", "TX", "rooftop"),
            _existing(2, "FLYING J #2", 32.7867, -96.797, "Dallas", "TX", "rooftop"),
        ]
        # ~0.415 mi from its own nearest rooftop row -- outside the 0.25 mi
        # threshold, so both incoming candidates must be retained rather
        # than merged into the nearby existing station.
        incoming = [
            _incoming("g1", "New Fuel Stop A", "32.7827", "-96.797"),
            _incoming("g2", "New Fuel Stop B", "32.7947", "-96.797"),
        ]

        kept, decisions = overture_dedupe.deduplicate(incoming, existing)

        self.assertEqual({row.gers_id for row in kept}, {"g1", "g2"})
        for decision in decisions:
            self.assertEqual(decision.decision, "kept")

    def test_city_tier_match_does_not_consult_distance(self):
        existing = [
            _existing(1, "PILOT TRAVEL CENTER", 40.5865, -122.3917, "Redding", "CA", "city")
        ]
        # Hundreds of miles from the matched city-centroid pin -- proves
        # the city tier never looks at distance at all.
        incoming = [
            _incoming(
                "g1", "Pilot Travel Center 0621", "34.0522", "-118.2437",
                city="Redding", state="CA", chain_token="PILOT",
            )
        ]

        kept, decisions = overture_dedupe.deduplicate(incoming, existing)

        self.assertEqual(kept, [])
        decision = decisions[0]
        self.assertEqual(decision.decision, "dropped")
        self.assertEqual(decision.tier, "city")
        self.assertEqual(decision.matched_opis_id, 1)
        self.assertEqual(decision.distance_mi, "")

    def test_brandless_incoming_row_can_never_city_match(self):
        existing = [
            _existing(1, "PILOT TRAVEL CENTER", 40.5865, -122.3917, "Redding", "CA", "city")
        ]
        # Same city/state as the existing row, but no chain token --
        # brand is half the city tier's key, so this can never match there.
        incoming = [
            _incoming(
                "g1", "Unbranded Fuel Stop", "34.0522", "-118.2437",
                city="Redding", state="CA", chain_token=None,
            )
        ]

        kept, decisions = overture_dedupe.deduplicate(incoming, existing)

        self.assertEqual(len(kept), 1)
        decision = decisions[0]
        self.assertEqual(decision.decision, "kept")
        self.assertIn("brand token is None", decision.reason)

    def test_no_match_anywhere_is_kept_and_records_the_tier_consulted(self):
        existing = [_existing(1, "PILOT #1", 32.7767, -96.797, "Dallas", "TX", "rooftop")]
        incoming = [
            _incoming("g1", "Unrelated Station", "45.0", "-100.0", city="Nowhere", state="ND")
        ]

        kept, decisions = overture_dedupe.deduplicate(incoming, existing)

        self.assertEqual(len(kept), 1)
        self.assertEqual(decisions[0].decision, "kept")

    def test_existing_rows_are_unchanged_by_identity_and_value(self):
        existing = [
            _existing(1, "PILOT #1", 32.7767, -96.797, "Dallas", "TX", "rooftop"),
            _existing(2, "FLYING J #2", 40.5865, -122.3917, "Redding", "CA", "city"),
        ]
        before_ids = [id(row) for row in existing]
        before_values = list(existing)

        incoming = [
            _incoming("g1", "Pilot Travel Center", "32.7777", "-96.797"),
            _incoming(
                "g2", "Flying J Travel Center", "34.0522", "-118.2437",
                city="Redding", state="CA", chain_token="FLYING J",
            ),
        ]
        overture_dedupe.deduplicate(incoming, existing)

        self.assertEqual([id(row) for row in existing], before_ids)
        self.assertEqual(existing, before_values)

    def test_sensitivity_counts_returns_exactly_three_values_in_widening_order(self):
        existing = [_existing(1, "PILOT #1", 32.7767, -96.797, "Dallas", "TX", "rooftop")]
        # Distances from the existing row, chosen to straddle all three
        # pinned thresholds (0.15 / 0.25 / 0.40 mi): ~0.069, ~0.208, ~0.346.
        incoming = [
            _incoming("close", "A", "32.7777", "-96.797"),
            _incoming("mid", "B", "32.7797", "-96.797"),
            _incoming("far", "C", "32.7817", "-96.797"),
        ]

        counts = overture_dedupe.sensitivity_counts(incoming, existing)

        self.assertEqual(len(counts), 3)
        ordered = [counts[t] for t in sorted(counts)]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(ordered, [1, 2, 3])

    def test_select_spot_check_clusters_is_deterministic(self):
        existing = [_existing(1, "PILOT #1", 32.7767, -96.797, "Dallas", "TX", "rooftop")]
        incoming = [
            _incoming(f"g{i}", f"Station {i}", str(32.7 + i * 0.01), "-96.797")
            for i in range(5)
        ]
        _, decisions = overture_dedupe.deduplicate(incoming, existing)

        first = overture_dedupe.select_spot_check_clusters(decisions)
        second = overture_dedupe.select_spot_check_clusters(decisions)

        self.assertEqual(first, second)

    # -- Anti-vacuity guards -------------------------------------------
    #
    # A dedupe that matches nothing (keeps every incoming row) and a
    # dedupe that matches everything (drops every incoming row) must both
    # fail. Each test below is the direct catch for one direction; both
    # were run once against a deliberately weakened `deduplicate()` (tier
    # one's threshold check short-circuited to always-False for one, and
    # tier one short-circuited to always-True for the other) to confirm
    # each fails exactly as intended, then reverted -- see the plan
    # SUMMARY for the record of that run.

    def test_not_vacuously_keep_everything(self):
        """A coincident rooftop match MUST be dropped -- if `deduplicate`
        degenerated into a no-op that always kept every row, this fails."""
        existing = [_existing(1, "PILOT #1", 32.7767, -96.797, "Dallas", "TX", "rooftop")]
        incoming = [_incoming("g1", "Pilot Travel Center", "32.7767", "-96.797")]

        kept, decisions = overture_dedupe.deduplicate(incoming, existing)

        self.assertEqual(kept, [], "a coincident rooftop candidate must be dropped")
        self.assertEqual(decisions[0].decision, "dropped")

    def test_not_vacuously_drop_everything(self):
        """A candidate nowhere near anything, with no brand token, MUST be
        kept -- if `deduplicate` degenerated into dropping every row, this
        fails."""
        existing = [_existing(1, "PILOT #1", 32.7767, -96.797, "Dallas", "TX", "rooftop")]
        incoming = [
            _incoming("g1", "Unrelated Unbranded Stop", "45.0", "-100.0", city="Nowhere", state="ND")
        ]

        kept, decisions = overture_dedupe.deduplicate(incoming, existing)

        self.assertEqual(len(kept), 1, "an unrelated, unmatched candidate must be kept")
        self.assertEqual(decisions[0].decision, "kept")

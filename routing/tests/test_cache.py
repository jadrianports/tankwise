"""Tests for the cache-key normalizer. No DB needed --
pure string-formatting behavior."""
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from routing.cache import (
    _dataset_vintage_token,
    build_cache_key,
    reset_dataset_vintage_token,
)


def coord(lat, lng):
    return {"kind": "coordinate", "lat": Decimal(str(lat)), "lng": Decimal(str(lng))}


def address(value):
    return {"kind": "address", "value": value}


def vehicle(mpg=None, tank_range_mi=None, starting_fuel=None):
    """Build a validated-data-shaped vehicle dict, Decimal-valued like
    RouteRequestSerializer.validated_data["vehicle"], with any
    unspecified key simply omitted (letting _vehicle_token supply its
    own default for that key)."""
    result = {}
    if mpg is not None:
        result["mpg"] = Decimal(str(mpg))
    if tank_range_mi is not None:
        result["tank_range_mi"] = Decimal(str(tank_range_mi))
    if starting_fuel is not None:
        result["starting_fuel"] = Decimal(str(starting_fuel))
    return result


class CoordinatePrecisionCollapseTests(SimpleTestCase):
    """Two coordinate requests differing only past the 5th decimal place
    produce the same key."""

    def test_sixth_decimal_difference_collapses_to_same_key(self):
        key1 = build_cache_key(
            {
                "start": coord("41.878100", "-87.629800"),
                "finish": coord("38.627000", "-90.199400"),
            }
        )
        key2 = build_cache_key(
            {
                "start": coord("41.8781004", "-87.6298001"),
                "finish": coord("38.6270003", "-90.1994002"),
            }
        )

        self.assertEqual(key1, key2)


class AddressNormalizationTests(SimpleTestCase):
    """Two addresses differing only in case/whitespace produce identical
    keys."""

    def test_case_and_whitespace_variants_collapse_to_same_key(self):
        key1 = build_cache_key(
            {"start": address("123 Main St"), "finish": address("456 Oak Ave")}
        )
        key2 = build_cache_key(
            {"start": address("  123   MAIN st "), "finish": address("456 OAK ave")}
        )

        self.assertEqual(key1, key2)


class NamespaceCollisionTests(SimpleTestCase):
    """A coordinate token and an address token with the same underlying
    characters must never collide (explicit c:/a: prefixes)."""

    def test_coordinate_and_lookalike_address_string_produce_distinct_keys(self):
        coord_key = build_cache_key(
            {
                "start": coord("41.87810", "-87.62980"),
                "finish": coord("38.62700", "-90.19940"),
            }
        )
        # An address whose text happens to equal the coordinate token's
        # post-normalization body would collide without the prefix.
        addr_key = build_cache_key(
            {
                "start": address("41.8781,-87.6298"),
                "finish": address("38.627,-90.1994"),
            }
        )

        self.assertNotEqual(coord_key, addr_key)


class MixedRequestStabilityTests(SimpleTestCase):
    """A mixed coord+address request produces a stable, distinct key."""

    def test_mixed_request_is_stable_and_distinct(self):
        key1 = build_cache_key(
            {"start": coord("41.8781", "-87.6298"), "finish": address("St Louis, MO")}
        )
        key2 = build_cache_key(
            {"start": coord("41.8781", "-87.6298"), "finish": address("St Louis, MO")}
        )
        coord_only_key = build_cache_key(
            {
                "start": coord("41.8781", "-87.6298"),
                "finish": coord("38.6270", "-90.1994"),
            }
        )

        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, coord_only_key)


class KeyFormatTests(SimpleTestCase):
    """Every produced key starts with route:v11: (OVRT-03/PIPE-01, plan
    22-12) and contains exactly six | separators
    (stops-chain|vehicle|eia|dispatch|penalty|trust_margin|dataset)."""

    def test_key_starts_with_prefix_and_has_two_separators(self):
        key = build_cache_key(
            {
                "start": coord("41.8781", "-87.6298"),
                "finish": address("St Louis, MO"),
            }
        )

        self.assertTrue(key.startswith("route:v11:"))
        self.assertEqual(key.count("|"), 6)


class VehicleCacheKeyTests(SimpleTestCase):
    """Cache keys are vehicle-aware: two profiles never collide, a
    near-identical profile still hits, and a missing vehicle key
    resolves identically to the explicit defaults."""

    def _payload(self, vehicle=None):
        payload = {
            "start": coord("41.8781", "-87.6298"),
            "finish": coord("38.6270", "-90.1994"),
        }
        if vehicle is not None:
            payload["vehicle"] = vehicle
        return payload

    def test_different_mpg_produces_different_key(self):
        key_a = build_cache_key(self._payload(vehicle(mpg="6")))
        key_b = build_cache_key(self._payload(vehicle(mpg="10")))

        self.assertNotEqual(key_a, key_b)

    def test_mpg_precision_collapses_to_same_key(self):
        key_a = build_cache_key(self._payload(vehicle(mpg="6")))
        key_b = build_cache_key(self._payload(vehicle(mpg="6.00")))

        self.assertEqual(key_a, key_b)

    def test_absent_vehicle_matches_explicit_defaults(self):
        key_absent = build_cache_key(self._payload())
        key_explicit = build_cache_key(
            self._payload(vehicle(mpg="10", tank_range_mi="500", starting_fuel="1.0"))
        )

        self.assertEqual(key_absent, key_explicit)

    def test_every_key_starts_with_current_prefix(self):
        for payload in (
            self._payload(),
            self._payload(vehicle(mpg="6")),
            self._payload(vehicle(tank_range_mi="1800")),
        ):
            self.assertTrue(build_cache_key(payload).startswith("route:v11:"))

    def test_no_generated_key_contains_v1_substring(self):
        profiles = [
            None,
            vehicle(mpg="6"),
            vehicle(mpg="32", tank_range_mi="400"),
            vehicle(starting_fuel="0.25"),
            vehicle(mpg="100", tank_range_mi="2000", starting_fuel="0.0"),
        ]
        for profile in profiles:
            self.assertNotIn("route:v1:", build_cache_key(self._payload(profile)))

    def test_spread_of_distinct_profiles_yields_no_key_collisions(self):
        profiles = [
            vehicle(mpg="6", tank_range_mi="1800", starting_fuel="1.0"),
            vehicle(mpg="10", tank_range_mi="500", starting_fuel="1.0"),
            vehicle(mpg="32", tank_range_mi="400", starting_fuel="1.0"),
            vehicle(mpg="10", tank_range_mi="500", starting_fuel="0.5"),
            vehicle(mpg="10", tank_range_mi="500", starting_fuel="0.0"),
            vehicle(mpg="1", tank_range_mi="20", starting_fuel="0.0"),
            vehicle(mpg="100", tank_range_mi="2000", starting_fuel="1.0"),
        ]
        keys = [build_cache_key(self._payload(profile)) for profile in profiles]

        self.assertEqual(len(keys), len(set(keys)))


class PenaltyCacheKeyTests(SimpleTestCase):
    """The `p:` penalty token (INTG-03, D-32) ties a cached payload to the
    flat per-stop penalty it was priced under: two different penalties
    never collide, while an omitted penalty (a legacy or test caller)
    still produces a stable key."""

    def _payload(self):
        return {
            "start": coord("41.8781", "-87.6298"),
            "finish": coord("38.6270", "-90.1994"),
        }

    def test_different_penalty_produces_different_key(self):
        key_a = build_cache_key(self._payload(), penalty=Decimal("35"))
        key_b = build_cache_key(self._payload(), penalty=Decimal("20"))

        self.assertNotEqual(key_a, key_b)

    def test_omitted_penalty_is_stable_across_calls(self):
        key_a = build_cache_key(self._payload())
        key_b = build_cache_key(self._payload())

        self.assertEqual(key_a, key_b)


class TrustMarginCacheKeyTests(SimpleTestCase):
    """The `t:` trust-margin token (PROV-03, D-17, plan 21-06) ties a
    cached payload to the flat per-purchase trust margin it was priced
    under -- a sibling of `PenaltyCacheKeyTests` above, for the identical
    reason: two different margins never collide, while an omitted margin
    (a legacy or test caller) still produces a stable key."""

    def _payload(self):
        return {
            "start": coord("41.8781", "-87.6298"),
            "finish": coord("38.6270", "-90.1994"),
        }

    def test_different_trust_margin_produces_different_key(self):
        key_a = build_cache_key(self._payload(), trust_margin=Decimal("10.93"))
        key_b = build_cache_key(self._payload(), trust_margin=Decimal("0"))

        self.assertNotEqual(key_a, key_b)

    def test_omitted_trust_margin_is_stable_across_calls(self):
        key_a = build_cache_key(self._payload())
        key_b = build_cache_key(self._payload())

        self.assertEqual(key_a, key_b)

    def test_trust_margin_token_is_the_second_to_last_segment(self):
        # No longer the final segment as of plan 22-12 -- the `s:`
        # dataset-vintage token now follows it (see
        # DatasetVintageCacheKeyPlacementTests below).
        key = build_cache_key(self._payload(), trust_margin=Decimal("5.47"))
        self.assertTrue(key.split("|")[-2].startswith("t:"))
        self.assertIn("5.47", key.split("|")[-2])


class EiaVintageCacheKeyTests(SimpleTestCase):
    """The EIA-week vintage token (EIA-01) ties a cached payload to the
    EIA week it was priced under: a week rollover produces a distinct
    key so a stale plan is never served under a fresh disclaimer, while
    a repeat request under the same vintage still hits the cache."""

    def _payload(self):
        return {
            "start": coord("41.8781", "-87.6298"),
            "finish": coord("38.6270", "-90.1994"),
        }

    def test_different_eia_vintage_produces_different_key(self):
        key_a = build_cache_key(self._payload(), eia_vintage="2026-07-13")
        key_b = build_cache_key(self._payload(), eia_vintage="2026-07-20")

        self.assertNotEqual(key_a, key_b)

    def test_identical_eia_vintage_reproduces_same_key(self):
        key_a = build_cache_key(self._payload(), eia_vintage="2026-07-20")
        key_b = build_cache_key(self._payload(), eia_vintage="2026-07-20")

        self.assertEqual(key_a, key_b)

    def test_frozen_vintage_never_collides_with_a_dated_vintage(self):
        frozen_key = build_cache_key(self._payload(), eia_vintage="frozen")
        current_key = build_cache_key(self._payload(), eia_vintage="2026-07-20")

        self.assertNotEqual(frozen_key, current_key)

    def test_omitted_eia_vintage_is_stable_across_calls(self):
        key_a = build_cache_key(self._payload())
        key_b = build_cache_key(self._payload())

        self.assertEqual(key_a, key_b)


class WaypointOrderingCacheKeyTests(SimpleTestCase):
    """The ordered `start -> *waypoints -> finish` token chain
    (Pitfall 13): visit order is part of the key, so a same-stop-set
    trip in a different order never collides with a different trip's
    cache entry."""

    def _payload(self, waypoints=None):
        payload = {
            "start": coord("34.0522", "-118.2437"),  # LA
            "finish": coord("41.8781", "-87.6298"),  # Chicago
        }
        if waypoints is not None:
            payload["waypoints"] = waypoints
        return payload

    def test_permuted_waypoints_produce_different_keys(self):
        denver = coord("39.7392", "-104.9903")
        st_louis = address("St Louis, MO")

        key_denver_then_st_louis = build_cache_key(
            self._payload([denver, st_louis])
        )
        key_st_louis_then_denver = build_cache_key(
            self._payload([st_louis, denver])
        )

        self.assertNotEqual(key_denver_then_st_louis, key_st_louis_then_denver)

    def test_no_waypoints_key_is_stable(self):
        key_a = build_cache_key(self._payload())
        key_b = build_cache_key(self._payload(waypoints=[]))

        self.assertEqual(key_a, key_b)

    def test_waypoints_present_differs_from_no_waypoints(self):
        denver = coord("39.7392", "-104.9903")

        key_no_waypoints = build_cache_key(self._payload())
        key_with_waypoint = build_cache_key(self._payload([denver]))

        self.assertNotEqual(key_no_waypoints, key_with_waypoint)


class DispatchPolicyCacheKeyTests(SimpleTestCase):
    """The `d:` dispatch-policy token (plan 18-12, INTG-03) ties a cached
    payload to the dispatch policy (predictor identity + `DP_TRANSITION_BUDGET`)
    that decided `solver_strategy` for it -- closing the coupling
    `18-VERIFICATION.md` found unguarded: `route:v6:` keyed on the penalty
    but NOT on the dispatch policy.

    Extended by plan 18.1-05 (D-01/D-07): the dispatch policy grew a
    second layer, a wall-clock `DP_DEADLINE_SECONDS` that backstops
    `DP_TRANSITION_BUDGET`'s pre-flight estimate, so the token now derives
    from BOTH constants together. `test_dispatch_policy_token_tracks_the_policy_constants`
    above already proves the budget's influence;
    `test_dispatch_policy_token_tracks_the_deadline_constant_alone` below
    proves the SAME property for the deadline alone -- changing only
    `DP_DEADLINE_SECONDS`, with `DP_TRANSITION_BUDGET` untouched, must
    still change the resulting key. A token that tracked the budget but
    not the deadline would pass every assertion above and still leave
    half the policy unguarded, exactly the failure mode this extension
    exists to close."""

    def _payload(self):
        return {
            "start": coord("41.8781", "-87.6298"),
            "finish": coord("38.6270", "-90.1994"),
        }

    def test_new_prefix_appears_in_a_built_key(self):
        key = build_cache_key(self._payload())
        self.assertTrue(key.startswith("route:v11:"))

    def test_dispatch_policy_token_is_present_and_correctly_placed(self):
        key = build_cache_key(self._payload(), eia_vintage="2026-07-20", penalty=Decimal("35"))
        segments = key[len("route:v11:") :].split("|")
        # stops_token | vehicle_token | eia_token | dispatch_token | penalty_token | trust_margin_token | dataset_token
        self.assertEqual(len(segments), 7)
        dispatch_segment = segments[3]
        self.assertTrue(
            dispatch_segment.startswith("d:"),
            f"expected the 4th segment to be the dispatch-policy token "
            f"(d:...), got {dispatch_segment!r} in key {key!r}",
        )
        self.assertIn("estimate_transition_count", dispatch_segment)

    def test_dispatch_policy_token_tracks_the_policy_constants(self):
        """The mechanism test: a token that exists but is pinned to a
        literal would pass the two assertions above and still leave the
        coupling unguarded -- exactly the failure mode this token exists
        to close. Patching the live constant must change the resulting
        key."""
        key_before = build_cache_key(self._payload())
        with mock.patch("routing.services.dp.DP_TRANSITION_BUDGET", 12_345):
            key_after = build_cache_key(self._payload())

        self.assertNotEqual(key_before, key_after)
        self.assertIn("12345", key_after.split("|")[3])

    def test_dispatch_policy_token_tracks_the_deadline_constant_alone(self):
        """The deadline half of the mechanism test (plan 18.1-05, D-07):
        `DP_TRANSITION_BUDGET` is left untouched here -- only
        `DP_DEADLINE_SECONDS` is patched -- so this isolates the deadline's
        own contribution to the token from the budget's, already proven
        above. A token derived from the budget alone would pass every
        other assertion in this class and still fail this one."""
        key_before = build_cache_key(self._payload())
        with mock.patch("routing.services.dp.DP_DEADLINE_SECONDS", Decimal("99")):
            key_after = build_cache_key(self._payload())

        self.assertNotEqual(key_before, key_after)
        self.assertIn("99", key_after.split("|")[3])

    def test_dispatch_policy_token_is_stable_and_never_omittable(self):
        """Unlike `_eia_token`/`_penalty_token`, this token has no
        caller-supplied optional input to omit -- a dispatch policy is a
        build-time module constant, always known, never a per-request
        fact a caller might not have threaded through yet. There is
        therefore no "none" literal to produce; what this asserts instead
        is the same stability those tokens' own "omitted" tests check:
        repeated calls under the SAME policy constants always produce the
        SAME token, with no caller input able to vary it."""
        key_a = build_cache_key(self._payload())
        key_b = build_cache_key(self._payload())

        self.assertEqual(key_a, key_b)


class DatasetVintageTokenTests(SimpleTestCase):
    """The `s:` dataset-vintage token (plan 22-08, D-27/D-28/D-30) hashes
    exactly the canonical station CSVs `seed_stations` replays -- proven in
    three directions rather than two, because the third is the one a naive
    single-file hash passes the other two while failing (D-30, criterion 6).

    Every test here operates on temporary files, never on the committed
    CSVs under `data/` -- `CANONICAL_STATION_CSV_PATHS` is patched at its
    declaration site (`routing.services.station_csv_paths`), which
    `_dataset_vintage_token`'s own local import picks up on every call."""

    def setUp(self):
        reset_dataset_vintage_token()

    def tearDown(self):
        reset_dataset_vintage_token()

    def _write(self, tmp_dir, name, content: bytes) -> Path:
        path = Path(tmp_dir) / name
        path.write_bytes(content)
        return path

    def test_token_changes_when_station_data_changes(self):
        """Direction 1: the token changes when station data changes."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            first = self._write(tmp_dir, "stations_geocoded.csv", b"a,b\n1,2\n")
            second = self._write(tmp_dir, "overture_stations.csv", b"c,d\n3,4\n")
            with mock.patch(
                "routing.services.station_csv_paths.CANONICAL_STATION_CSV_PATHS",
                (first, second),
            ):
                token_before = _dataset_vintage_token()
                first.write_bytes(first.read_bytes() + b"5,6\n")
                reset_dataset_vintage_token()
                token_after = _dataset_vintage_token()

        self.assertNotEqual(token_before, token_after)

    def test_token_unchanged_across_a_non_dataset_change(self):
        """Direction 2: unchanged across a change that touches no station
        data. This repository deploys far more often than Overture
        releases land, so a build-derived token would nuke the whole route
        cache on every copy fix -- this is the direction that guards
        against that over-invalidation failure mode."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            first = self._write(tmp_dir, "stations_geocoded.csv", b"a,b\n1,2\n")
            outside = self._write(tmp_dir, "gazetteer_places_trimmed.csv", b"x,y\n7,8\n")
            with mock.patch(
                "routing.services.station_csv_paths.CANONICAL_STATION_CSV_PATHS",
                (first,),
            ):
                token_before = _dataset_vintage_token()
                outside.write_bytes(outside.read_bytes() + b"9,10\n")
                reset_dataset_vintage_token()
                token_after = _dataset_vintage_token()

        self.assertEqual(token_before, token_after)

    def test_token_changes_when_only_the_second_member_changes(self):
        """Direction 3: changes when ONLY a non-first member of the
        canonical list changes -- the shape criterion 6 names explicitly
        as the one a naive single-file hash fails. An implementation that
        hashes only the first CSV passes directions 1 and 2 above and
        still fails this one, which is exactly why it exists: that
        implementation looks correct under the other two tests."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            first = self._write(tmp_dir, "stations_geocoded.csv", b"a,b\n1,2\n")
            second = self._write(tmp_dir, "overture_stations.csv", b"c,d\n3,4\n")
            with mock.patch(
                "routing.services.station_csv_paths.CANONICAL_STATION_CSV_PATHS",
                (first, second),
            ):
                token_before = _dataset_vintage_token()
                second.write_bytes(second.read_bytes() + b"5,6\n")
                reset_dataset_vintage_token()
                token_after = _dataset_vintage_token()

        self.assertNotEqual(token_before, token_after)

    def test_memo_does_not_re_read_files_without_a_reset(self):
        """The memo is genuine: a second call with no reset does not
        re-read the files -- it is what makes the token once-per-process
        rather than per-request."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            first = self._write(tmp_dir, "stations_geocoded.csv", b"a,b\n1,2\n")
            with mock.patch(
                "routing.services.station_csv_paths.CANONICAL_STATION_CSV_PATHS",
                (first,),
            ):
                token_before = _dataset_vintage_token()
                first.write_bytes(first.read_bytes() + b"changed\n")
                token_after = _dataset_vintage_token()

        self.assertEqual(token_before, token_after)

    def test_token_is_sensitive_to_canonical_list_order(self):
        """Order sensitivity: the token is built name-then-bytes over the
        canonical tuple IN LIST ORDER, so two files swapping positions
        (with different content in each) must produce a different token --
        this is why the canonical tuple's ordering is part of the
        contract, not an implementation detail."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            a = self._write(tmp_dir, "a.csv", b"aaa\n")
            b = self._write(tmp_dir, "b.csv", b"bbb\n")

            reset_dataset_vintage_token()
            with mock.patch(
                "routing.services.station_csv_paths.CANONICAL_STATION_CSV_PATHS",
                (a, b),
            ):
                token_ab = _dataset_vintage_token()

            reset_dataset_vintage_token()
            with mock.patch(
                "routing.services.station_csv_paths.CANONICAL_STATION_CSV_PATHS",
                (b, a),
            ):
                token_ba = _dataset_vintage_token()

        self.assertNotEqual(token_ab, token_ba)

    def test_real_canonical_list_excludes_the_raw_extract_and_gazetteer(self):
        """Membership exclusion at the boundary this plan owns (D-28):
        with `CANONICAL_STATION_CSV_PATHS` pointed at the real committed
        tuple, no member's name is the raw extract or the gazetteer.
        Plan 22-05 asserts the same thing from the other side
        (`StationCsvPathsTests`); asserting it here too means neither file
        can enter the hash list without failing a test in the module that
        does the hashing."""
        from routing.services.station_csv_paths import CANONICAL_STATION_CSV_PATHS

        names = {path.name for path in CANONICAL_STATION_CSV_PATHS}

        self.assertNotIn("overture_raw_extract.csv", names)
        self.assertNotIn("gazetteer_places_trimmed.csv", names)


class DatasetVintageCacheKeyPlacementTests(SimpleTestCase):
    """`build_cache_key`'s own integration with the `s:` token (plan
    22-12): DatasetVintageTokenTests above proves `_dataset_vintage_token`
    in isolation; this class proves `build_cache_key` actually calls it and
    places it correctly -- a token that exists but is never wired in would
    pass every assertion above and still leave `route:v11:` no different
    from `route:v10:` in practice."""

    def setUp(self):
        reset_dataset_vintage_token()
        self.addCleanup(reset_dataset_vintage_token)

    def _payload(self):
        return {
            "start": coord("41.8781", "-87.6298"),
            "finish": coord("38.6270", "-90.1994"),
        }

    def test_final_segment_starts_with_s(self):
        key = build_cache_key(self._payload())
        self.assertTrue(key.startswith("route:v11:"))
        self.assertTrue(key.split("|")[-1].startswith("s:"))

    def test_key_changes_when_canonical_list_contents_change(self):
        """The mechanism test: a token that exists but is pinned to a
        literal (or never re-derived) would pass the placement assertion
        above and still leave a dataset change silently unreflected in the
        cache key -- exactly the failure mode this token exists to close."""
        import tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "stations_geocoded.csv"
            csv_path.write_bytes(b"a,b\n1,2\n")

            with mock.patch(
                "routing.services.station_csv_paths.CANONICAL_STATION_CSV_PATHS",
                (csv_path,),
            ):
                reset_dataset_vintage_token()
                key_before = build_cache_key(self._payload())

                csv_path.write_bytes(csv_path.read_bytes() + b"3,4\n")
                reset_dataset_vintage_token()
                key_after = build_cache_key(self._payload())

        self.assertNotEqual(key_before, key_after)

"""Pinned measurement corridors, tank ranges, and price bases (D-16/D-18).

This module fixes the twelve corridors, the two tank ranges, and the two
price bases BEFORE any measurement is taken (D-16) -- the set is never
adjusted after seeing a result. Ten of the twelve original v3.1-scoping
corridors are unrecoverable: only Dallas -> Seattle and Toronto -> Hillsboro
survive by name in PROJECT.md/REQUIREMENTS.md, because the harness that
produced the historical 508/121 and 157/286 figures no longer exists in git
or on disk. This module, `fetch_corridor_geometry` (the one-time capture
command), and `measure_prune_reduction` (plan 17-05's report) are the
antidote: every later reduction figure is re-derivable by any reviewer with
no Mapbox token and no network, forever, because the real route geometry is
committed once and replayed offline.

These constants are the single shared source of truth for the reduction
report and its future consumers (plan 17-05's `measure_prune_reduction`
command, and later Phase 18's latency sweep and Phase 19's demo-trip
verification) -- never copied, only imported. This follows the same
discipline `CORPUS_PARAMS` established in
`test_solver_fixed_charge_optimality.py`: pinned parameters live in one
shared module-level constant, imported by every consumer.
"""
import json
import pathlib
from dataclasses import dataclass
from decimal import Decimal

from django.test import SimpleTestCase

from routing.pipeline import overture_scope
from routing.services import eia
from routing.services.mapbox import _parse_directions_response

CORRIDOR_GEOMETRY_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "corridor_geometry"


@dataclass(frozen=True)
class Corridor:
    """One pinned measurement corridor.

    `start`/`finish` are (latitude, longitude) Decimal pairs -- the ordering
    `mapbox.get_routes()` takes. Note the Mapbox URL path itself is built
    lon,lat (the opposite order); `mapbox.py`'s own `get_routes()` already
    handles that flip, so callers here always work in (lat, lng).

    `estimated_driving_mi` carries RESEARCH.md's figure. For ten of the
    twelve corridors this is an ESTIMATE (great-circle distance x 1.2), not
    a measurement -- only Dallas -> Seattle and Toronto -> Hillsboro were
    ever actually driven-route-measured in the lost harness. No field here
    is ever adjusted to fit a later measured result (D-16).
    """

    slug: str
    label: str
    start: tuple[Decimal, Decimal]
    finish: tuple[Decimal, Decimal]
    estimated_driving_mi: int


def _pt(lat, lng):
    return (Decimal(lat), Decimal(lng))


# Twelve corridors, fixed before any measurement (D-16). Coordinates
# verified against this repo's own data/gazetteer_places_trimmed.csv.
# dallas_tx-seattle_wa and toronto_oh-hillsboro_or are mandatory -- the
# only two named in PROJECT.md/REQUIREMENTS.md and therefore the only two
# survivors of the lost twelve-corridor harness.
#
# Spread rationale: eight of the twelve sit above the ~1,400 mi threshold
# the evidence base names as where the trivial-stop problem concentrates
# (D-16's "emphasis above 1,400 mi"). sacramento_ca-salt_lake_city_ut, at
# ~635 mi, is deliberately shorter than the 1,050 mi UI-default tank range
# so the report covers D-05's "route shorter than tank range" shape on
# real geometry rather than only on synthetic unit-test fixtures.
CORRIDORS = (
    Corridor(
        slug="dallas_tx-seattle_wa",
        label="Dallas, TX -> Seattle, WA",
        start=_pt("32.7767", "-96.7970"),
        finish=_pt("47.6062", "-122.3321"),
        estimated_driving_mi=2108,
    ),
    Corridor(
        slug="toronto_oh-hillsboro_or",
        label="Toronto, OH -> Hillsboro, OR",
        start=_pt("40.457934", "-80.60963"),
        finish=_pt("45.526798", "-122.935395"),
        estimated_driving_mi=2578,
    ),
    Corridor(
        slug="san_diego_ca-jacksonville_fl",
        label="San Diego, CA -> Jacksonville, FL",
        start=_pt("32.830391", "-117.120923"),
        finish=_pt("30.336864", "-81.661603"),
        estimated_driving_mi=2501,
    ),
    Corridor(
        slug="el_paso_tx-portland_me",
        label="El Paso, TX -> Portland, ME",
        start=_pt("31.84778", "-106.431106"),
        finish=_pt("43.633157", "-70.185305"),
        estimated_driving_mi=2545,
    ),
    Corridor(
        slug="phoenix_az-minneapolis_mn",
        label="Phoenix, AZ -> Minneapolis, MN",
        start=_pt("33.572154", "-112.090132"),
        finish=_pt("44.963324", "-93.26832"),
        estimated_driving_mi=1527,
    ),
    Corridor(
        slug="miami_fl-boston_ma",
        label="Miami, FL -> Boston, MA",
        start=_pt("25.775163", "-80.208615"),
        finish=_pt("42.338551", "-71.018253"),
        estimated_driving_mi=1509,
    ),
    Corridor(
        slug="jacksonville_fl-bangor_me",
        label="Jacksonville, FL -> Bangor, ME",
        start=_pt("30.336864", "-81.661603"),
        finish=_pt("44.829625", "-68.788767"),
        estimated_driving_mi=1465,
    ),
    Corridor(
        slug="atlanta_ga-denver_co",
        label="Atlanta, GA -> Denver, CO",
        start=_pt("33.762909", "-84.422675"),
        finish=_pt("39.76185", "-104.881105"),
        estimated_driving_mi=1443,
    ),
    Corridor(
        slug="houston_tx-chicago_il",
        label="Houston, TX -> Chicago, IL",
        start=_pt("29.785743", "-95.388806"),
        finish=_pt("41.837045", "-87.684939"),
        estimated_driving_mi=1124,
    ),
    Corridor(
        slug="fargo_nd-amarillo_tx",
        label="Fargo, ND -> Amarillo, TX",
        start=_pt("46.8647", "-96.82908"),
        finish=_pt("35.199903", "-101.830194"),
        estimated_driving_mi=1016,
    ),
    Corridor(
        slug="nashville_tn-buffalo_ny",
        label="Nashville, TN -> Buffalo, NY",
        start=_pt("36.1718", "-86.785002"),
        finish=_pt("42.892492", "-78.859686"),
        estimated_driving_mi=753,
    ),
    Corridor(
        slug="sacramento_ca-salt_lake_city_ut",
        label="Sacramento, CA -> Salt Lake City, UT",
        start=_pt("38.567694", "-121.468161"),
        finish=_pt("40.776928", "-111.930991"),
        estimated_driving_mi=635,
    ),
)


# D-18: two tank ranges, both offline. 1,050 mi is the UI-default loaded
# semi the whole v3.1 evidence base uses; 500 mi is solve()'s own
# signature default and a realistic non-semi. The prune's effectiveness is
# structurally tank-dependent (a long route's tail prunes for free at a
# large tank, but not at a small one), so a single-vehicle figure would
# misrepresent it -- PITFALLS.md Pitfall 6's exact warning.
TANK_RANGES_MI = (Decimal("1050"), Decimal("500"))

# D-18: two price bases, both offline. "neutral" is a flat 1.0 factor for
# every state (eia._frozen_table() already returns exactly this table, so
# "neutral" and the codebase's own frozen-degradation mode are the same
# configuration -- no network, no cache, no get_factor_table() call
# anywhere in this module). "eia_fixture" applies the committed
# routing/tests/fixtures/eia_response.json factors via the existing
# _parse_eia_response path. Domination is entirely a price-ordering
# question, so measuring both hands Phase 18 a head start on the
# EIA-x-penalty interaction STATE.md flags as uncharacterised.
PRICE_BASIS_NEUTRAL = "neutral"
PRICE_BASIS_EIA = "eia_fixture"
PRICE_BASES = (PRICE_BASIS_NEUTRAL, PRICE_BASIS_EIA)

_EIA_FIXTURE_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "eia_response.json"


def factor_lookup_for_basis(basis):
    """Return a `factor_for(state) -> Decimal` closure for `basis`.

    "neutral" is built from `eia._frozen_table()` -- a 1.0 factor for
    every state, no network, no cache. "eia_fixture" is built by replaying
    the committed `routing/tests/fixtures/eia_response.json` through the
    existing `eia._parse_eia_response()` parser -- also no network, no
    cache. Raises `ValueError` for any other basis string.
    """
    if basis == PRICE_BASIS_NEUTRAL:
        table = eia._frozen_table()
    elif basis == PRICE_BASIS_EIA:
        with open(_EIA_FIXTURE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        table = eia._parse_eia_response(raw)
    else:
        raise ValueError(f"Unknown price basis: {basis!r}")
    return eia.make_factor_lookup(table)


def load_corridor_route(slug):
    """Load corridor `slug`'s committed Mapbox Directions fixture and
    replay it through the existing, already-tested parser -- no
    hand-rolled parsing code. Returns element 0 of the parsed list
    (Mapbox's primary alternative).

    Raises `FileNotFoundError` with a message naming
    `manage.py fetch_corridor_geometry` when the fixture is absent.
    """
    fixture_path = CORRIDOR_GEOMETRY_DIR / f"{slug}.json"
    if not fixture_path.exists():
        raise FileNotFoundError(
            f"No committed geometry fixture for corridor {slug!r} at "
            f"{fixture_path}. Run "
            f"`manage.py fetch_corridor_geometry --corridor {slug}` to "
            "capture it (one-time, online, see fetch_corridor_geometry.py)."
        )
    with open(fixture_path, encoding="utf-8") as f:
        raw = json.load(f)
    routes = _parse_directions_response(raw)
    return routes[0]


# CONUS coordinate bounds (RESEARCH.md Pitfall 3's corruption/alteration
# guard) -- catches a lat/lng swap or a truncated array. Loose enough to
# cover Maine/Florida/California/Washington without false positives.
_CONUS_LNG_RANGE = (Decimal("-125"), Decimal("-66"))
_CONUS_LAT_RANGE = (Decimal("24"), Decimal("50"))

# A genuine overview=full Mapbox response for a multi-hundred-mile
# interstate route is not a few dozen points -- the shortest committed
# corridor (sacramento_ca-salt_lake_city_ut, ~635 mi measured) still
# carries thousands. 500 is a loose floor well under every measured
# count, catching a hand-thinned or re-encoded fixture (RESEARCH.md
# Pitfall 3) without being tuned to any one corridor's exact count.
_MIN_COORDINATE_COUNT = 500

# D-05/RESEARCH.md: the pinned estimated_driving_mi is a great-circle x
# 1.2 ESTIMATE for ten of the twelve corridors (measured only for the two
# mandatory survivors historically), so this is a loose sanity band
# catching a wrong-endpoint capture, not a precision claim. The pinned
# estimates are NOT updated to match what was measured here -- the
# measured mileages are reported in this plan's SUMMARY and by plan
# 17-05's command instead.
_MILEAGE_TOLERANCE_FRACTION = Decimal("0.25")


@dataclass(frozen=True)
class DemoChip:
    """One demo-trip chip from the SPA's own `PRESET_ROUTES` (Phase 18.1
    D-13/D-14), registered here rather than appended to `CORRIDORS`.
    `CorridorFixtureTests` pins the corridor set at exactly twelve and
    asserts every corridor is single-leg -- both properties are
    load-bearing for the twelve-corridor evidence base -- and a demo chip
    is neither: it plays no part in that evidence base, and the
    multi-stop chip is deliberately NOT single-leg, so `DEMO_CHIPS` gets
    its own registry and its own guard class instead.

    `waypoints` is a tuple of (latitude, longitude) Decimal pairs, empty
    for a single-leg chip, matching `fetch_corridor_geometry --waypoints`'s
    own visit-order contract.
    """

    slug: str
    label: str
    start: tuple[Decimal, Decimal]
    waypoints: tuple
    finish: tuple[Decimal, Decimal]
    estimated_driving_mi: int


# Two demo chips (D-13/D-14). Coordinates transcribed verbatim from
# frontend/src/constants/presets.ts's PRESET_ROUTES (READ-ONLY for this
# phase) -- never independently re-derived.
DEMO_CHIPS = (
    DemoChip(
        slug="demo_la_ca-new_york_ny",
        label="Los Angeles, CA -> New York City, NY",
        start=_pt("34.0522", "-118.2437"),
        waypoints=(),
        finish=_pt("40.7128", "-74.0060"),
        estimated_driving_mi=2790,
    ),
    DemoChip(
        slug="demo_la_ca-denver_co-chicago_il",
        label="Los Angeles, CA -> Denver, CO -> Chicago, IL",
        start=_pt("34.0522", "-118.2437"),
        waypoints=(_pt("39.7392", "-104.9903"),),
        finish=_pt("41.8781", "-87.6298"),
        estimated_driving_mi=2000,
    ),
)

# D-14: DEMO_CHIP_VEHICLE is the SPA hero preset from frontend/src/
# constants/presets.ts VEHICLE_PRESETS['semi-loaded'] -- 6.5 mpg / 1050 mi
# tank / full tank (starting_fuel=1) -- because that is literally what a
# visitor clicking a demo chip actually sends. DEMO_CHIP_VEHICLE is a
# DIFFERENT vehicle from both:
#   - ADMISSION_MANIFEST_VEHICLE (routing/tests/test_solver_dispatch.py):
#     mpg=10, starting_fuel=0.5, the stale-41.7%-figure vehicle the 24
#     corridor cells are measured at for comparability with that figure.
#   - the API default (10 mpg / 500 mi tank / full tank, starting_fuel=1),
#     used by DeployedHardwareDispatchTests and the smoke gate for any
#     request that omits `vehicle`.
# Named here explicitly so the three are never conflated.
DEMO_CHIP_VEHICLE = {
    "mpg": Decimal("6.5"),
    "tank_range_mi": Decimal(1050),
    "starting_fuel": Decimal(1),
    "price_basis": PRICE_BASIS_NEUTRAL,
}


def load_demo_chip_route(slug):
    """Load demo chip `slug`'s committed Mapbox Directions fixture and
    replay it through the existing, already-tested parser -- same shape
    and same fixture directory as `load_corridor_route`.

    Raises `FileNotFoundError` naming the `fetch_corridor_geometry`
    invocation that captures it (plain `--corridor <slug>` for the
    single-leg chip; `--corridor <slug> --waypoints lat,lng` for the
    multi-stop chip).
    """
    fixture_path = CORRIDOR_GEOMETRY_DIR / f"{slug}.json"
    if not fixture_path.exists():
        raise FileNotFoundError(
            f"No committed geometry fixture for demo chip {slug!r} at "
            f"{fixture_path}. Run `manage.py fetch_corridor_geometry "
            f"--corridor {slug} [--waypoints lat,lng ...]` to capture it "
            "(one-time, online, see fetch_corridor_geometry.py)."
        )
    with open(fixture_path, encoding="utf-8") as f:
        raw = json.load(f)
    routes = _parse_directions_response(raw)
    return routes[0]


@dataclass(frozen=True)
class WestCoastProbe:
    """One pinned West Coast feasibility probe route (Phase 22 D-06/D-07),
    registered here rather than appended to `CORRIDORS`. These routes exist
    to record the feasibility residual the targeted Overture gap-fill import
    leaves behind -- what the shipped build does with each of them BEFORE
    the import exists (this plan's BEFORE verdict) and later, plan 22-13's
    AFTER comparison. They are registered separately, following the same
    precedent `DemoChip`/`DEMO_CHIPS` set (Phase 18.1 D-13/D-14), because
    `CorridorFixtureTests` pins `CORRIDORS` at exactly twelve and
    `ADMISSION_MANIFEST`'s 26 cells must stay like-for-like against the
    plan 22-01 pre-import baseline -- extending either set would break that
    bookend. `WEST_COAST_PROBES` plays no part in the twelve-corridor
    evidence base `CorridorFixtureTests` guards.

    Field shape mirrors `DemoChip` exactly: `slug`, `label`, `start`,
    `waypoints`, `finish`, `estimated_driving_mi`.
    """

    slug: str
    label: str
    start: tuple[Decimal, Decimal]
    waypoints: tuple
    finish: tuple[Decimal, Decimal]
    estimated_driving_mi: int


# Four West Coast probes (D-06), the upper end of D-06's stated 3-to-4
# range -- chosen because the fourth is the only route that crosses both
# gap-fill boxes. Pinned now, before the Overture import exists, so the
# probe set is never chosen after seeing a result (D-06).
WEST_COAST_PROBES = (
    WestCoastProbe(
        slug="seattle_wa-san_diego_ca",
        label="Seattle, WA -> San Diego, CA",
        start=_pt("47.6062", "-122.3321"),
        waypoints=(),
        finish=_pt("32.7157", "-117.1611"),
        estimated_driving_mi=1255,
    ),  # criterion 1's own named route
    WestCoastProbe(
        slug="san_francisco_ca-seattle_wa",
        label="San Francisco, CA -> Seattle, WA",
        start=_pt("37.7749", "-122.4194"),
        waypoints=(),
        finish=_pt("47.6062", "-122.3321"),
        estimated_driving_mi=810,
    ),  # README's demo walkthrough item 5 already documents this as a
    # live, reproducible 422 -- the probe whose BEFORE verdict is
    # independently corroborated by shipped documentation
    WestCoastProbe(
        slug="portland_or-sacramento_ca",
        label="Portland, OR -> Sacramento, CA",
        start=_pt("45.5152", "-122.6784"),
        waypoints=(),
        finish=_pt("38.5816", "-121.4944"),
        estimated_driving_mi=580,
    ),  # interior of the northern gap-fill band, end to end
    WestCoastProbe(
        slug="los_angeles_ca-eugene_or",
        label="Los Angeles, CA -> Eugene, OR",
        start=_pt("34.0522", "-118.2437"),
        waypoints=(),
        finish=_pt("44.0521", "-123.0868"),
        estimated_driving_mi=900,
    ),  # the only probe that spans BOTH gap-fill boxes: exercises the
    # SoCal extension and the northern band in a single route
)


# D-06: WEST_COAST_PROBE_VEHICLE's values are deliberately identical to
# DEMO_CHIP_VEHICLE's -- the SPA hero preset, which is literally what a
# visitor planning Seattle -> San Diego sends -- but this is a SEPARATELY
# NAMED constant rather than a reuse, following the same "named here
# explicitly so the vehicles are never conflated" discipline
# DEMO_CHIP_VEHICLE's own comment applies to the three existing vehicles.
# There are now four: ADMISSION_MANIFEST_VEHICLE (mpg=10,
# starting_fuel=0.5), DEMO_CHIP_VEHICLE, the API default (mpg=10,
# tank_range_mi=500, starting_fuel=1), and this one. PRICE_BASIS_NEUTRAL
# matches what ADMISSION_MANIFEST_VEHICLE and DEMO_CHIP_VEHICLE both use,
# so a single factor_for lookup remains valid across all of them.
WEST_COAST_PROBE_VEHICLE = {
    "mpg": Decimal("6.5"),
    "tank_range_mi": Decimal(1050),
    "starting_fuel": Decimal(1),
    "price_basis": PRICE_BASIS_NEUTRAL,
}


def load_west_coast_probe_route(slug):
    """Load West Coast probe `slug`'s committed Mapbox Directions fixture
    and replay it through the existing, already-tested parser -- same
    fixture directory and same shape as `load_corridor_route`/
    `load_demo_chip_route`.

    Raises `FileNotFoundError` naming the exact ad-hoc
    `fetch_corridor_geometry` invocation that captures it. No substitution
    fallback of any kind (D-08/D-16).
    """
    fixture_path = CORRIDOR_GEOMETRY_DIR / f"{slug}.json"
    if not fixture_path.exists():
        probe = next((p for p in WEST_COAST_PROBES if p.slug == slug), None)
        coord_hint = ""
        if probe is not None:
            coord_hint = (
                f" --start {probe.start[0]},{probe.start[1]} --finish "
                f"{probe.finish[0]},{probe.finish[1]}"
            )
        raise FileNotFoundError(
            f"No committed geometry fixture for West Coast probe {slug!r} "
            f"at {fixture_path}. Run `manage.py fetch_corridor_geometry "
            f"--slug {slug}{coord_hint}` to capture it (one-time, online, "
            "see fetch_corridor_geometry.py)."
        )
    with open(fixture_path, encoding="utf-8") as f:
        raw = json.load(f)
    routes = _parse_directions_response(raw)
    return routes[0]


class CorridorFixtureTests(SimpleTestCase):
    """CI-side guard that the twelve committed corridor fixtures (D-15)
    stay valid: present, parseable, single-leg, geometrically plausible,
    and CONUS-bounded. Does NOT re-verify the corridor set itself is
    exactly the D-16 table (that is a code-review concern, confirmed by
    inspection when this plan's tasks were written) -- it verifies the
    committed *fixtures* have not silently drifted from what a live
    Mapbox call would return.
    """

    def test_corridors_has_exactly_twelve_unique_slugs_including_mandatory_two(self):
        self.assertEqual(len(CORRIDORS), 12)
        slugs = [c.slug for c in CORRIDORS]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertIn("dallas_tx-seattle_wa", slugs)
        self.assertIn("toronto_oh-hillsboro_or", slugs)

    def test_every_corridor_fixture_file_exists(self):
        for corridor in CORRIDORS:
            with self.subTest(slug=corridor.slug):
                fixture_path = CORRIDOR_GEOMETRY_DIR / f"{corridor.slug}.json"
                self.assertTrue(
                    fixture_path.exists(),
                    f"Missing committed fixture for {corridor.slug!r} at "
                    f"{fixture_path}",
                )

    def test_every_corridor_loads_and_parses_without_raising(self):
        for corridor in CORRIDORS:
            with self.subTest(slug=corridor.slug):
                route = load_corridor_route(corridor.slug)
                self.assertIsNotNone(route)

    def test_every_corridor_is_single_leg_feasible_geometry(self):
        for corridor in CORRIDORS:
            with self.subTest(slug=corridor.slug):
                route = load_corridor_route(corridor.slug)
                self.assertLessEqual(len(route.leg_distances_mi), 1)
                self.assertGreater(route.total_route_mi, 0)
                self.assertGreaterEqual(
                    len(route.raw_coordinates), _MIN_COORDINATE_COUNT
                )

    def test_every_coordinate_is_a_conus_bounded_pair(self):
        min_lng, max_lng = _CONUS_LNG_RANGE
        min_lat, max_lat = _CONUS_LAT_RANGE
        for corridor in CORRIDORS:
            with self.subTest(slug=corridor.slug):
                route = load_corridor_route(corridor.slug)
                for coord in route.raw_coordinates:
                    self.assertEqual(len(coord), 2)
                    lng, lat = Decimal(str(coord[0])), Decimal(str(coord[1]))
                    self.assertGreaterEqual(lng, min_lng)
                    self.assertLessEqual(lng, max_lng)
                    self.assertGreaterEqual(lat, min_lat)
                    self.assertLessEqual(lat, max_lat)

    def test_every_corridor_measured_mileage_within_loose_sanity_band(self):
        for corridor in CORRIDORS:
            with self.subTest(slug=corridor.slug):
                route = load_corridor_route(corridor.slug)
                pinned = Decimal(corridor.estimated_driving_mi)
                tolerance = pinned * _MILEAGE_TOLERANCE_FRACTION
                self.assertLessEqual(
                    abs(route.total_route_mi - pinned),
                    tolerance,
                    f"{corridor.slug}: measured {route.total_route_mi} mi vs "
                    f"pinned estimate {pinned} mi exceeds the "
                    f"{_MILEAGE_TOLERANCE_FRACTION:.0%} sanity band",
                )


class DemoChipFixtureTests(SimpleTestCase):
    """CI-side guard that the two demo-chip geometry fixtures (D-13/D-14)
    stay valid: present, parseable, CONUS-bounded, geometrically
    plausible, and consumed by production's own flattening path. Mirrors
    `CorridorFixtureTests`' shape but with the leg assertion INVERTED for
    the multi-stop chip -- it must parse to exactly two legs and flatten
    through `multi_leg.flatten_route` into two lines, never a merged
    single line, honouring the standing "multi-leg routes never merge"
    constraint the same way production does.
    """

    def test_both_fixture_files_exist_and_parse(self):
        for chip in DEMO_CHIPS:
            with self.subTest(slug=chip.slug):
                route = load_demo_chip_route(chip.slug)
                self.assertIsNotNone(route)

    def test_single_leg_chip_parses_to_at_most_one_leg(self):
        route = load_demo_chip_route("demo_la_ca-new_york_ny")
        self.assertLessEqual(len(route.leg_distances_mi), 1)

    def test_multi_stop_chip_parses_to_exactly_two_legs(self):
        route = load_demo_chip_route("demo_la_ca-denver_co-chicago_il")
        self.assertEqual(len(route.leg_distances_mi), 2)
        self.assertEqual(len(route.leg_annotation_lengths), 2)
        for length in route.leg_annotation_lengths:
            self.assertGreater(length, 0)

    def test_multi_stop_chip_flattens_into_two_lines_not_a_merged_line(self):
        from routing.services.multi_leg import flatten_route

        route = load_demo_chip_route("demo_la_ca-denver_co-chicago_il")
        flattened = flatten_route(route)
        self.assertEqual(len(flattened.leg_lines), 2)
        self.assertEqual(len(flattened.leg_boundaries_mi), 2)
        self.assertEqual(flattened.leg_boundaries_mi[0], Decimal(0))
        self.assertGreater(flattened.leg_boundaries_mi[1], Decimal(0))

    def test_every_demo_chip_coordinate_is_a_conus_bounded_pair(self):
        min_lng, max_lng = _CONUS_LNG_RANGE
        min_lat, max_lat = _CONUS_LAT_RANGE
        for chip in DEMO_CHIPS:
            with self.subTest(slug=chip.slug):
                route = load_demo_chip_route(chip.slug)
                for coord in route.raw_coordinates:
                    self.assertEqual(len(coord), 2)
                    lng, lat = Decimal(str(coord[0])), Decimal(str(coord[1]))
                    self.assertGreaterEqual(lng, min_lng)
                    self.assertLessEqual(lng, max_lng)
                    self.assertGreaterEqual(lat, min_lat)
                    self.assertLessEqual(lat, max_lat)

    def test_every_demo_chip_coordinate_count_at_or_above_floor(self):
        for chip in DEMO_CHIPS:
            with self.subTest(slug=chip.slug):
                route = load_demo_chip_route(chip.slug)
                self.assertGreaterEqual(
                    len(route.raw_coordinates), _MIN_COORDINATE_COUNT
                )

    def test_every_demo_chip_measured_mileage_within_loose_sanity_band(self):
        for chip in DEMO_CHIPS:
            with self.subTest(slug=chip.slug):
                route = load_demo_chip_route(chip.slug)
                pinned = Decimal(chip.estimated_driving_mi)
                tolerance = pinned * _MILEAGE_TOLERANCE_FRACTION
                self.assertLessEqual(
                    abs(route.total_route_mi - pinned),
                    tolerance,
                    f"{chip.slug}: measured {route.total_route_mi} mi vs "
                    f"pinned estimate {pinned} mi exceeds the "
                    f"{_MILEAGE_TOLERANCE_FRACTION:.0%} sanity band",
                )


# 4 decimal places -- the endpoint-match precision D-08's no-substitution
# rule is verified against (roughly 11 m at these latitudes), matching the
# plan's own "match the registry to 4 decimal places" acceptance criterion.
_PROBE_COORD_MATCH_TOLERANCE = Decimal("0.0001")


class WestCoastProbeFixtureTests(SimpleTestCase):
    """CI-side guard that the four West Coast feasibility probes (D-06/D-07)
    stay valid, mirroring `CorridorFixtureTests`'/`DemoChipFixtureTests`'
    shape. Until plan 22-06 Task 2 captures the four geometry fixtures, the
    per-fixture assertions below fail with a `FileNotFoundError` naming the
    exact capture invocation -- that failure IS the no-substitution rule
    (D-08/D-16) demonstrating itself: there is no fallback path that could
    let this class pass on missing geometry, and no assertion here treats a
    missing fixture as anything other than a hard error.
    """

    def test_west_coast_probes_has_exactly_four_unique_slugs_in_order(self):
        self.assertEqual(len(WEST_COAST_PROBES), 4)
        slugs = [p.slug for p in WEST_COAST_PROBES]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(
            slugs,
            [
                "seattle_wa-san_diego_ca",
                "san_francisco_ca-seattle_wa",
                "portland_or-sacramento_ca",
                "los_angeles_ca-eugene_or",
            ],
        )

    def test_every_probe_fixture_file_exists(self):
        for probe in WEST_COAST_PROBES:
            with self.subTest(slug=probe.slug):
                fixture_path = CORRIDOR_GEOMETRY_DIR / f"{probe.slug}.json"
                self.assertTrue(
                    fixture_path.exists(),
                    f"Missing committed fixture for {probe.slug!r} at "
                    f"{fixture_path}",
                )

    def test_every_probe_loads_and_parses_without_raising(self):
        for probe in WEST_COAST_PROBES:
            with self.subTest(slug=probe.slug):
                route = load_west_coast_probe_route(probe.slug)
                self.assertIsNotNone(route)

    def test_every_probe_is_single_leg_feasible_geometry(self):
        for probe in WEST_COAST_PROBES:
            with self.subTest(slug=probe.slug):
                route = load_west_coast_probe_route(probe.slug)
                self.assertLessEqual(len(route.leg_distances_mi), 1)
                self.assertGreater(route.total_route_mi, 0)

    def test_every_probe_endpoint_matches_registry_to_four_decimal_places(self):
        for probe in WEST_COAST_PROBES:
            with self.subTest(slug=probe.slug):
                route = load_west_coast_probe_route(probe.slug)
                first_lng, first_lat = route.raw_coordinates[0]
                last_lng, last_lat = route.raw_coordinates[-1]
                expected_start_lat, expected_start_lng = probe.start
                expected_finish_lat, expected_finish_lng = probe.finish
                self.assertLessEqual(
                    abs(Decimal(str(first_lat)) - expected_start_lat),
                    _PROBE_COORD_MATCH_TOLERANCE,
                )
                self.assertLessEqual(
                    abs(Decimal(str(first_lng)) - expected_start_lng),
                    _PROBE_COORD_MATCH_TOLERANCE,
                )
                self.assertLessEqual(
                    abs(Decimal(str(last_lat)) - expected_finish_lat),
                    _PROBE_COORD_MATCH_TOLERANCE,
                )
                self.assertLessEqual(
                    abs(Decimal(str(last_lng)) - expected_finish_lng),
                    _PROBE_COORD_MATCH_TOLERANCE,
                )

    def test_corridors_and_admission_manifest_are_unmoved(self):
        # Read-only import of the manifest, done here (inside the test
        # method) rather than at module level to avoid a circular import:
        # test_solver_dispatch.py itself imports CORRIDORS/DEMO_CHIPS from
        # this module.
        from routing.tests.test_solver_dispatch import ADMISSION_MANIFEST

        self.assertEqual(len(CORRIDORS), 12)
        self.assertEqual(len(ADMISSION_MANIFEST), 26)


# ---------------------------------------------------------------------------
# D-37: which corridors/demo chips the gap-fill import may legitimately move
# ---------------------------------------------------------------------------


def _route_intersects_gap_fill(route):
    """True when at least one vertex of `route.raw_coordinates` (the real
    committed Mapbox polyline, `[lng, lat]` pairs) falls inside the union of
    `overture_scope.GAP_FILL_BOXES`. Walks the whole vertex list -- a route
    can pass through the gap-fill region without either endpoint being
    inside it, so an endpoint-only check would under-count."""
    for lng, lat in route.raw_coordinates:
        if overture_scope.contains(lat, lng):
            return True
    return False


def compute_gap_fill_intersecting_slugs():
    """Walk every committed corridor and demo-chip route's real polyline and
    return the frozenset of slugs with at least one vertex inside the
    gap-fill multi-box (D-37). Re-derived from committed geometry every time
    this runs -- never hand-maintained -- so a later geometry or bbox change
    is caught by `GapFillIntersectionPreDeclarationTests` rather than
    silently going stale."""
    intersecting = set()
    for corridor in CORRIDORS:
        if _route_intersects_gap_fill(load_corridor_route(corridor.slug)):
            intersecting.add(corridor.slug)
    for chip in DEMO_CHIPS:
        if _route_intersects_gap_fill(load_demo_chip_route(chip.slug)):
            intersecting.add(chip.slug)
    return frozenset(intersecting)


# Pinned by running compute_gap_fill_intersecting_slugs() once, 2026-08-08,
# against the twelve committed corridor fixtures and the two committed
# demo-chip fixtures, before any dispatch measurement exists (D-37). The
# computed set DIFFERS from RESEARCH.md's endpoint-based starting
# hypothesis (dallas_tx-seattle_wa, san_diego_ca-jacksonville_fl,
# sacramento_ca-salt_lake_city_ut, both demo chips "likely"; toronto_oh-
# hillsboro_or "borderline") -- both are recorded side by side in this
# plan's SUMMARY and deliberately not reconciled; the geometry-derived
# answer below is the one that ships, per this project's own
# record-side-by-side convention.
#
#   - dallas_tx-seattle_wa does NOT intersect: its Seattle endpoint sits at
#     lat 47.6062, north of GAP_FILL_BOXES' first box's ymax=44.0 (that box
#     is deliberately scoped to "the Grapevine north through southern
#     Oregon" -- it stops at the OR/WA state line by design, see
#     overture_scope.py's own comment). The hypothesis reasoned from the
#     endpoint alone and was wrong.
#   - toronto_oh-hillsboro_or does NOT intersect: Hillsboro, OR sits at
#     lng -122.94/lat 45.53, outside both boxes on both axes (lat 45.53 >
#     44.0 ymax; lng -122.94 < -124.0 xmin is false so lng is in-range, but
#     lat alone excludes it). The "borderline" hypothesis resolves to a
#     clean miss on real geometry, not a near-hit.
#   - san_diego_ca-jacksonville_fl, sacramento_ca-salt_lake_city_ut, and
#     both demo chips (demo_la_ca-new_york_ny,
#     demo_la_ca-denver_co-chicago_il) all intersect, confirming that half
#     of the hypothesis.
#
# D-37's prediction: every corridor/demo-chip slug NOT in this set must
# show byte-zero change on every deterministic field in plan 22-14's diff.
# Movement there is a defect to root-cause before the phase closes, not a
# number to re-pin -- PITFALLS 4's own named warning sign.
GAP_FILL_INTERSECTING_SLUGS = frozenset(
    {
        "san_diego_ca-jacksonville_fl",
        "sacramento_ca-salt_lake_city_ut",
        "demo_la_ca-new_york_ny",
        "demo_la_ca-denver_co-chicago_il",
    }
)


class GapFillIntersectionPreDeclarationTests(SimpleTestCase):
    """D-37: which of the twelve pinned corridors and two demo chips may
    legitimately move under the Overture gap-fill import is pre-declared
    here, before any dispatch measurement exists (this class lands in wave
    3, three waves before plan 22-14's diff). The central assertion is an
    equality between the pinned literal above and a live re-derivation from
    committed geometry -- if a geometry fixture or `overture_scope.
    GAP_FILL_BOXES` ever changes, this test fails and a human re-derives
    the set rather than the prediction silently going stale.

    The prediction this class encodes (D-37): corridors and demo chips that
    do NOT geographically intersect the gap-fill multi-box must show
    byte-zero change on every deterministic field in plan 22-14's
    before/after diff. Movement on a non-intersecting corridor is a defect
    to root-cause before the phase closes, not a number to re-pin --
    PITFALLS.md Pitfall 4's own named warning sign is exactly this: a stop
    count or total cost changing on a pinned corridor that does not
    geographically intersect the gap-fill region at all. This is stated
    before any measurement exists, per this project's pin-then-measure
    discipline.
    """

    def test_computed_intersection_equals_pinned_literal(self):
        self.assertEqual(
            compute_gap_fill_intersecting_slugs(), GAP_FILL_INTERSECTING_SLUGS
        )

    def test_every_corridor_and_demo_chip_slug_is_classified_exactly_once(self):
        all_slugs = {c.slug for c in CORRIDORS} | {chip.slug for chip in DEMO_CHIPS}
        self.assertEqual(len(all_slugs), 14)
        non_intersecting = all_slugs - GAP_FILL_INTERSECTING_SLUGS
        # Every one of the 14 slugs falls into exactly one of the two
        # buckets -- the pinned set is a subset of all_slugs, and its
        # complement (within all_slugs) accounts for the rest. None is
        # omitted from either side.
        self.assertTrue(GAP_FILL_INTERSECTING_SLUGS.issubset(all_slugs))
        self.assertEqual(
            GAP_FILL_INTERSECTING_SLUGS | non_intersecting, all_slugs
        )
        self.assertEqual(
            len(GAP_FILL_INTERSECTING_SLUGS) + len(non_intersecting), 14
        )

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

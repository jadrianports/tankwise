"""Match incoming Overture candidates against the existing OPIS dataset and
drop the ones that are really the same real-world station under a second
name.

The rule, verbatim from the discussion that pinned it, so the command
docstring and the committed report can quote the same words:

    Match tight on distance only when the existing row is rooftop-precision
    -- both sides are then real coordinates. For city-centroid rows,
    distance is not a dedup signal at all; match on normalized brand plus
    city and state. Never one shared radius.

Of the existing 6,738 rows, 526 are rooftop-geocoded (a real US Census
address-batch geocode) and 5,764 are city-centroid pins that can sit miles
from the real lot -- see `data/geocode-report.md`. Against a rooftop row,
both sides are real coordinates and a tight distance match is meaningful.
Against a city-centroid row, distance is not a dedup signal at all -- the
pin is a city, not a place -- so the match is normalized brand plus city and
state instead. A single shared radius cannot serve both cases: wide enough
to catch a city-centroid duplicate, it also merges two genuinely distinct
truck stops sitting at one interchange.

On a match, the incoming Overture row is dropped and the existing row is
returned completely untouched -- no id, no price, no coordinate moves.
There is no code path anywhere in this module that mutates an existing row
or writes one back; the import only ever ADDS rows. Upgrading a matched
city-centroid row's coordinates from the Overture point -- a genuine data
improvement and a free partial delivery of the deferred rooftop backfill --
was considered and is deliberately deferred: doing it here would move
existing stations under every pinned corridor and destroy this phase's
ability to attribute a dispatch change to a new station rather than a moved
one.

Pure Python: stdlib plus `decimal`, importing only `overture_scope`. No
Django ORM, no network, no Parquet toolchain -- this module must be
importable with no database provisioned, exactly like
`routing.pipeline.overture`. The equirectangular point-to-point distance
below deliberately re-derives `routing/services/corridor.py`'s
cos(mean_lat) x MI_PER_DEGREE_LAT projection approach (this codebase's
standing no-PostGIS/no-GDAL stance) rather than importing corridor.py
itself, because corridor.py pulls in Django's settings and the Station
model -- exactly what this module must stay free of.

Called by `routing.pipeline.overture.transform()`, between `assign_price`
and `mint_identities` -- see that module's own docstring for the full
pipeline stage order.
"""
import math
from dataclasses import dataclass
from decimal import Decimal

from routing.pipeline import overture_scope

# ---------------------------------------------------------------------------
# Field literals mirroring routing.models' GeocodePrecision -- hardcoded
# rather than imported for the same reason routing.pipeline.overture hand-
# copies its own field literals: importing routing.models would pull the
# Django ORM into a module that must run with no database provisioned. Keep
# these in sync BY HAND with routing.models's GeocodePrecision.ROOFTOP /
# GeocodePrecision.CITY.
# ---------------------------------------------------------------------------
_GEOCODE_PRECISION_ROOFTOP = "rooftop"
_GEOCODE_PRECISION_CITY = "city"


# ---------------------------------------------------------------------------
# Distance -- an equirectangular re-derivation of corridor.py's approach,
# not an import of it (see module docstring).
# ---------------------------------------------------------------------------

# Mirrors routing/services/corridor.py's MI_PER_DEGREE_LAT (Decimal
# "69.172"). Kept in sync by hand.
_MI_PER_DEGREE_LAT = 69.172

# 1.0 mi comfortably exceeds every value this module is ever asked to check
# a distance against -- TIGHT_TIER_THRESHOLD_MI is 0.25 and the widest
# TIGHT_TIER_SENSITIVITY_MI neighbour is 0.40 -- a better-than-2x margin so
# the 3x3 neighbor-cell search in `_nearest_rooftop_candidate` below never
# misses a genuine candidate at any threshold this module checks.
GRID_CELL_SIZE_MI = 1.0

# CONUS latitudes run roughly 25 deg (south Texas) to 49 deg (the US-Canada
# border); cos(49 deg) is approximately 0.656, the smallest value in that
# range and therefore the value that needs the MOST degrees of longitude to
# cover GRID_CELL_SIZE_MI worth of real ground. A single fixed conservative
# divisor below that bound keeps every longitude grid cell at least
# GRID_CELL_SIZE_MI wide everywhere the gap-fill boxes or the existing
# dataset could plausibly place a row, at the cost of slightly oversized
# (safe, never undersized) cells further south.
_CONSERVATIVE_MIN_COS_LAT = 0.6


def _distance_mi(lat1, lng1, lat2, lng2):
    """Equirectangular point-to-point distance in miles between two
    (lat, lng) pairs, in degrees. Accurate to well under 1% at the sub-mile
    scale this module compares at -- see the module docstring for why this
    is a re-derivation of corridor.py's projection rather than an import of
    it."""
    mean_lat_rad = math.radians((lat1 + lat2) / 2.0)
    cos_lat = math.cos(mean_lat_rad)
    dx = (lng1 - lng2) * _MI_PER_DEGREE_LAT * cos_lat
    dy = (lat1 - lat2) * _MI_PER_DEGREE_LAT
    return math.hypot(dx, dy)


def _grid_cell(lat, lng, cell_size_mi):
    """Coarse (lat_idx, lng_idx) bucket key for `lat`/`lng`, sized so a
    cell is at least `cell_size_mi` wide in both directions everywhere in
    CONUS (see `_CONSERVATIVE_MIN_COS_LAT`)."""
    lat_f = float(lat)
    lng_f = float(lng)
    lat_cell_deg = cell_size_mi / _MI_PER_DEGREE_LAT
    lng_cell_deg = cell_size_mi / (_MI_PER_DEGREE_LAT * _CONSERVATIVE_MIN_COS_LAT)
    return (math.floor(lat_f / lat_cell_deg), math.floor(lng_f / lng_cell_deg))


# ---------------------------------------------------------------------------
# The existing (OPIS) row this module compares candidates against
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingStationRow:
    """One existing OPIS row from `data/stations_geocoded.csv`, the
    read-only comparison set `deduplicate()` matches Overture candidates
    against. Carries only the fields the two-tier rule needs -- there is no
    code path anywhere in this module that turns one of these back into a
    write, and no function here returns a modified copy of one."""

    opis_id: int
    name: str
    city: str
    state: str
    latitude: Decimal
    longitude: Decimal
    geocode_precision: str


def load_existing_rows(reader):
    """Parse `csv.DictReader` rows off `data/stations_geocoded.csv` (or any
    CSV sharing its column names) into `ExistingStationRow`. Reads only the
    seven fields the two-tier rule needs off the existing dataset -- name,
    city, state, coordinates, precision and the id used purely for
    reporting which existing row a match landed on."""
    rows = []
    for raw in reader:
        rows.append(
            ExistingStationRow(
                opis_id=int(raw["opis_id"]),
                name=raw["name"],
                city=raw["city"],
                state=raw["state"],
                latitude=Decimal(raw["latitude"]),
                longitude=Decimal(raw["longitude"]),
                geocode_precision=raw["geocode_precision"],
            )
        )
    return rows


# ---------------------------------------------------------------------------
# The per-decision record -- the reviewable artifact criterion 4 demands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DedupeDecision:
    """One row of the committed per-decision report: every incoming
    post-hygiene candidate, whichever tier decided it, and why."""

    gers_id: str
    name: str
    brand_token: str
    latitude: Decimal
    longitude: Decimal
    city: str
    state: str
    tier: str
    matched_opis_id: int
    matched_name: str
    matched_geocode_precision: str
    distance_mi: str
    decision: str
    reason: str


# CSV, not JSONL: every other committed data artifact in this repository is
# CSV (data/stations_geocoded.csv, data/overture_raw_extract.csv), `csv` is
# already the only parsing dependency in this half of the pipeline, and a
# reviewer opens this file in the same tool they already open the station
# CSVs with.
DEDUPE_DECISION_HEADER = [
    "gers_id",
    "name",
    "brand_token",
    "latitude",
    "longitude",
    "city",
    "state",
    "tier",
    "matched_opis_id",
    "matched_name",
    "matched_geocode_precision",
    "distance_mi",
    "decision",
    "reason",
]


def _make_decision(row, *, tier, matched, distance, outcome, reason):
    return DedupeDecision(
        gers_id=row.gers_id,
        name=row.name,
        brand_token=row.chain_token,
        latitude=row.latitude,
        longitude=row.longitude,
        city=row.address_locality,
        state=row.address_region,
        tier=tier,
        matched_opis_id=matched.opis_id if matched is not None else None,
        matched_name=matched.name if matched is not None else None,
        matched_geocode_precision=(
            matched.geocode_precision if matched is not None else None
        ),
        distance_mi=f"{distance:.3f}" if distance is not None else "",
        decision=outcome,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Indexes -- built once per `deduplicate()`/`sensitivity_counts()` call,
# never per-candidate. Roughly 5,000 incoming rows against 6,738 existing
# rows is 34 million comparisons under a naive nested scan; these two
# indexes make matching linear.
# ---------------------------------------------------------------------------


def _build_rooftop_index(existing_rows, cell_size_mi):
    index = {}
    for row in existing_rows:
        if row.geocode_precision != _GEOCODE_PRECISION_ROOFTOP:
            continue
        cell = _grid_cell(row.latitude, row.longitude, cell_size_mi)
        index.setdefault(cell, []).append(row)
    return index


def _build_city_index(existing_rows):
    """Keyed on (normalized chain token, casefolded city, uppercased
    state). Where more than one existing city-centroid row shares a key,
    the first one encountered (stable CSV order) is the one a match
    reports -- which existing row it names is immaterial to D-16, since the
    match only ever drops the incoming row and never touches the existing
    one."""
    index = {}
    for row in existing_rows:
        if row.geocode_precision != _GEOCODE_PRECISION_CITY:
            continue
        token = overture_scope.chain_alias_for(row.name)
        if token is None:
            continue
        key = (token, row.city.strip().casefold(), row.state.strip().upper())
        index.setdefault(key, row)
    return index


def _nearest_rooftop_candidate(lat, lng, index, cell_size_mi):
    """Return `(nearest_row, nearest_distance_mi)` among the rooftop rows
    in the incoming point's own grid cell and its eight neighbours --
    `(None, None)` if the neighbourhood holds no rooftop row at all. This
    is the nearest candidate found in the searched neighbourhood, not
    necessarily within any threshold; the caller decides whether it is
    close enough to count as a match."""
    center = _grid_cell(lat, lng, cell_size_mi)
    lat_f = float(lat)
    lng_f = float(lng)
    best_row = None
    best_distance = None
    for d_lat in (-1, 0, 1):
        for d_lng in (-1, 0, 1):
            for candidate in index.get((center[0] + d_lat, center[1] + d_lng), ()):
                distance = _distance_mi(
                    lat_f, lng_f, float(candidate.latitude), float(candidate.longitude)
                )
                if best_distance is None or distance < best_distance:
                    best_row = candidate
                    best_distance = distance
    return best_row, best_distance


# ---------------------------------------------------------------------------
# The dedup pass itself
# ---------------------------------------------------------------------------


def deduplicate(incoming, existing_rows):
    """Match each `incoming` (post-hygiene, post-price `OvertureRow`)
    candidate against `existing_rows`, tier one then tier two, and return
    `(kept, decisions)`. `kept` is the subset of `incoming` that matched
    nothing -- the only list this function ever drops from. `decisions`
    holds exactly one `DedupeDecision` per incoming row, in input order.

    No existing row is ever mutated or returned in modified form: matched
    existing rows are read for their id/name/precision only, to populate
    the decision record.
    """
    rooftop_index = _build_rooftop_index(existing_rows, GRID_CELL_SIZE_MI)
    city_index = _build_city_index(existing_rows)

    kept = []
    decisions = []

    for row in incoming:
        nearest_row, nearest_distance = _nearest_rooftop_candidate(
            row.latitude, row.longitude, rooftop_index, GRID_CELL_SIZE_MI
        )

        # Tier one, tight: a rooftop-precision existing row within the
        # pinned threshold. Both sides are real coordinates here, so
        # distance is a meaningful signal.
        if (
            nearest_row is not None
            and nearest_distance <= overture_scope.TIGHT_TIER_THRESHOLD_MI
        ):
            decisions.append(
                _make_decision(
                    row,
                    tier="tight",
                    matched=nearest_row,
                    distance=nearest_distance,
                    outcome="dropped",
                    reason=(
                        f"tight-tier match: {nearest_distance:.3f} mi from "
                        f"rooftop row opis_id={nearest_row.opis_id}"
                    ),
                )
            )
            continue

        # Tier two, city: only reached when tier one did not match, and
        # only when the incoming row carries a normalized brand token --
        # brand is half this tier's key, so a brandless row can never
        # match here. Distance is never consulted in this branch.
        matched_city_row = None
        if row.chain_token is not None:
            key = (
                row.chain_token,
                row.address_locality.strip().casefold(),
                row.address_region.strip().upper(),
            )
            matched_city_row = city_index.get(key)

        if matched_city_row is not None:
            decisions.append(
                _make_decision(
                    row,
                    tier="city",
                    matched=matched_city_row,
                    distance=None,
                    outcome="dropped",
                    reason=(
                        f"city-tier match on brand {row.chain_token!r}, city "
                        "and state (no distance signal)"
                    ),
                )
            )
            continue

        # Neither tier matched -- kept. `tier` records the deepest tier
        # actually reached: "city" when a brand token existed and the city
        # tier was consulted (and still found nothing), "tight" when the
        # row is brandless and the city tier never applied. `distance_mi`
        # still records the nearest rooftop candidate found, if any --
        # informational, and exactly what `select_spot_check_clusters`
        # uses to surface a near-miss sitting right at the tier boundary.
        if nearest_row is not None:
            miss = (
                f"nearest rooftop candidate {nearest_distance:.3f} mi away, "
                f"outside the {overture_scope.TIGHT_TIER_THRESHOLD_MI} mi "
                "tight-tier threshold"
            )
        else:
            miss = "no rooftop candidate found within the search radius"

        if row.chain_token is None:
            tier = "tight"
            reason = miss + "; brand token is None so the city tier does not apply"
        else:
            tier = "city"
            reason = miss + "; no city-tier match on brand, city and state"

        decisions.append(
            _make_decision(
                row,
                tier=tier,
                matched=None,
                distance=nearest_distance,
                outcome="kept",
                reason=reason,
            )
        )
        kept.append(row)

    return kept, decisions


def sensitivity_counts(incoming, existing_rows):
    """Return `{threshold_mi: tight-tier-only drop count}` for
    `overture_scope.TIGHT_TIER_THRESHOLD_MI` and its two
    `TIGHT_TIER_SENSITIVITY_MI` neighbours -- exactly three entries.
    Published in the committed report as information only. Tier two (city)
    is deliberately excluded here: it is not threshold-sensitive at all, so
    including it would blur the one parameter D-15 actually governs. The
    shipped threshold moves only via a recorded human decision citing
    spot-check evidence -- never citing this count, and never citing the
    dispatch result.
    """
    rooftop_index = _build_rooftop_index(existing_rows, GRID_CELL_SIZE_MI)
    thresholds = sorted(
        {overture_scope.TIGHT_TIER_THRESHOLD_MI, *overture_scope.TIGHT_TIER_SENSITIVITY_MI}
    )

    counts = {threshold: 0 for threshold in thresholds}
    for row in incoming:
        _, distance = _nearest_rooftop_candidate(
            row.latitude, row.longitude, rooftop_index, GRID_CELL_SIZE_MI
        )
        if distance is None:
            continue
        for threshold in thresholds:
            if distance <= threshold:
                counts[threshold] += 1
    return counts


def select_spot_check_clusters(decisions):
    """D-17's pinned rule: the `overture_scope.SPOT_CHECK_CLUSTER_COUNT`
    densest clusters by candidate count inside the gap-fill boxes, plus any
    cluster containing a tight-tier decision whose recorded distance falls
    within 20% of `overture_scope.TIGHT_TIER_THRESHOLD_MI` in either
    direction -- a boundary decision that could easily flip under a
    different sensitivity value, whether it matched or narrowly missed.
    Clustered by the same coarse grid cell `_build_rooftop_index` uses.

    Pure and deterministic: ties in candidate count break on the grid
    cell's own (lat_idx, lng_idx) sort order, so re-running against the
    same decisions list always yields the same selection in the same
    order. Every selected cluster is meant to be reported whole, whether it
    looks good or bad -- this function only selects, it never filters by
    outcome.

    Returns a list of `(cell, decisions_in_cell)` pairs, sorted by
    candidate count descending then cell order.
    """
    clusters = {}
    for decision in decisions:
        cell = _grid_cell(decision.latitude, decision.longitude, GRID_CELL_SIZE_MI)
        clusters.setdefault(cell, []).append(decision)

    ranked = sorted(clusters.keys(), key=lambda cell: (-len(clusters[cell]), cell))
    selected = set(ranked[: overture_scope.SPOT_CHECK_CLUSTER_COUNT])

    boundary_lo = overture_scope.TIGHT_TIER_THRESHOLD_MI * 0.8
    boundary_hi = overture_scope.TIGHT_TIER_THRESHOLD_MI * 1.2
    for cell, cell_decisions in clusters.items():
        if cell in selected:
            continue
        for decision in cell_decisions:
            if decision.tier != "tight" or not decision.distance_mi:
                continue
            distance = float(decision.distance_mi)
            if boundary_lo <= distance <= boundary_hi:
                selected.add(cell)
                break

    final_order = sorted(selected, key=lambda cell: (-len(clusters[cell]), cell))
    return [(cell, clusters[cell]) for cell in final_order]

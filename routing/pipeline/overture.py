"""Turn the committed raw Overture extract into station rows -- hygiene,
brand, price, identity, precision, provenance.

Pure Python: stdlib plus `decimal`, importing only
`routing.pipeline.overture_scope` and `routing.services.regions`. No Django
ORM, no Parquet toolchain, no network, and `Station` is never imported or
touched. This separation exists because the transform must be able to run
inside a job with no database provisioned at all (Phase 23's scheduled
refresh pipeline), and because every reviewable decision in this import
belongs here, over a CSV a human can open, rather than inside a management
command nobody reads (plan 22-10, D-24).

`import_overture_stations.py` is the thin command that reads
`data/overture_raw_extract.csv`, calls `transform()`, and writes the
station CSV plus the committed report -- it owns no hygiene, price, or
identity decision of its own.

Pipeline stages, in the order `transform()` runs them:
    parse_extract_rows -> apply_hygiene -> assign_brand -> assign_price ->
    (plan 22-11 inserts `overture_dedupe.deduplicate()` here) ->
    mint_identities -> to_station_rows
"""
import logging
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

from routing.pipeline import overture_scope
from routing.services import regions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field literals mirroring routing.models' TextChoices values
# ---------------------------------------------------------------------------
# Hardcoded rather than imported -- `from routing.models import ...` would
# pull the Django ORM into a module that must run with no database
# provisioned (see the module docstring). Keep these in sync BY HAND with
# routing/models.py's GeocodeStatus.OK, GeocodePrecision.ROOFTOP,
# PriceSource.EIA_REGIONAL_ESTIMATE and StationSource.OVERTURE -- read the
# model before changing any of these.
_GEOCODE_STATUS_OK = "ok"
_GEOCODE_PRECISION_ROOFTOP = "rooftop"
_PRICE_SOURCE_EIA_REGIONAL_ESTIMATE = "eia_regional_estimate"
_STATION_SOURCE_OVERTURE = "overture"

# Same 17-column order as geocode_stations.EXPORT_HEADER -- the CSV this
# import writes must match it exactly so seed_stations replays either file
# identically.
EXPORT_HEADER = [
    "opis_id",
    "name",
    "address",
    "city",
    "state",
    "rack_id",
    "retail_price",
    "observation_count",
    "price_min",
    "price_max",
    "latitude",
    "longitude",
    "geocode_precision",
    "geocode_status",
    "price_source",
    "source",
    "gers_id",
]

# Mirrors fetch_overture_extract.RAW_EXTRACT_HEADER -- the columns
# parse_extract_rows requires present in the raw extract's own header.
RAW_EXTRACT_REQUIRED_COLUMNS = (
    "gers_id",
    "name",
    "brand_name",
    "address_freeform",
    "address_locality",
    "address_region",
    "address_postcode",
    "category",
    "confidence",
    "operating_status",
    "longitude",
    "latitude",
)

# Four hygiene exclusion buckets plus a fifth malformed_row bucket for rows
# that fail structural parsing (at parse time) or carry an unresolvable
# state (at price-assignment time). Each is counted separately because the
# committed report needs per-category counts -- folding them into one
# "skipped" counter would make the report unable to answer the question it
# exists to answer.
OVERTURE_HYGIENE_BUCKETS = (
    "mojibake",
    "alt_fuel_only",
    "closed_status",
    "below_confidence_floor",
    "malformed_row",
)


class MalformedExtractHeaderError(ValueError):
    """Raised when the raw extract's header is missing a required column --
    a structural problem with the file, not a row problem, so it aborts the
    run loudly rather than being skip-and-logged like a bad row (mirrors
    seed_stations._read_csv_rows' CommandError on a missing column)."""


class OvertureIdCollisionError(Exception):
    """Raised when two distinct GERS ids mint to the same `opis_id`. A
    collision is unlikely at this import's actual scale but plausible --
    roughly 1.2% at n=5,000 and 1.8% at n=6,000, per
    overture_scope.mint_opis_id's own docstring -- which is exactly why
    this raises loudly rather than dropping, renumbering, or silently
    overwriting either row."""


@dataclass(frozen=True)
class OvertureRow:
    """One row of the raw extract, augmented as it moves through the
    pipeline via `dataclasses.replace` -- every stage returns a new,
    still-frozen instance rather than mutating. The trailing fields are
    unset (`None`) until the stage that computes them runs:
    `chain_token`/`assign_brand`, `region`/`retail_price`/`price_min`/
    `price_max`/`observation_count`/`assign_price`, `opis_id`/
    `mint_identities`."""

    gers_id: str
    name: str
    brand_name: str
    address_freeform: str
    address_locality: str
    address_region: str
    address_postcode: str
    category: str
    confidence: float
    operating_status: str
    longitude: Decimal
    latitude: Decimal
    chain_token: str = None
    region: str = None
    retail_price: Decimal = None
    price_min: Decimal = None
    price_max: Decimal = None
    observation_count: int = None
    opis_id: int = None


@dataclass(frozen=True)
class OvertureStation:
    """One emitted station row, in EXPORT_HEADER order."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: str
    retail_price: Decimal
    observation_count: int
    price_min: Decimal
    price_max: Decimal
    latitude: Decimal
    longitude: Decimal
    geocode_precision: str
    geocode_status: str
    price_source: str
    source: str
    gers_id: str


@dataclass(frozen=True)
class OvertureImportReport:
    """Everything the committed report needs to render: release, both bbox
    rectangles, the filter parameters, the input row count, each hygiene
    bucket's count, the kept count, and the per-region priced-row counts."""

    release: str
    boxes: tuple
    category_filter: tuple
    confidence_floor: float
    input_row_count: int
    bucket_counts: dict
    kept_count: int
    priced_row_counts_by_region: dict = field(default_factory=dict)


def parse_extract_rows(reader):
    """Read `csv.DictReader` rows off the raw extract into `OvertureRow`.
    A structurally malformed row (bad confidence/longitude/latitude) is
    skipped, logged with its line number, and counted -- reusing
    `import_stations._read_valid_rows`' skip-and-log shape verbatim, so one
    bad row never aborts the run. A **missing header column**, however,
    raises `MalformedExtractHeaderError` loudly, exactly as `seed_stations`
    does for its own derived CSVs -- a structural header problem is not a
    row problem.

    Returns `(rows, malformed_count)`.
    """
    fieldnames = set(reader.fieldnames or [])
    missing = [c for c in RAW_EXTRACT_REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        raise MalformedExtractHeaderError(
            f"Raw extract is missing required column(s): {', '.join(missing)}"
        )

    rows = []
    malformed = 0
    for line_num, raw in enumerate(reader, start=2):
        try:
            confidence = float(raw["confidence"])
            longitude = Decimal(raw["longitude"])
            latitude = Decimal(raw["latitude"])
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            malformed += 1
            logger.warning(
                "Skipping malformed extract row %d: %r (%s)", line_num, raw, exc
            )
            continue

        rows.append(
            OvertureRow(
                gers_id=raw["gers_id"],
                name=raw["name"],
                brand_name=raw["brand_name"],
                address_freeform=raw["address_freeform"],
                address_locality=raw["address_locality"],
                address_region=(raw["address_region"] or "").strip().upper(),
                address_postcode=raw["address_postcode"],
                category=raw["category"],
                confidence=confidence,
                operating_status=raw["operating_status"],
                longitude=longitude,
                latitude=latitude,
            )
        )
    return rows, malformed


def apply_hygiene(rows):
    """Drop mojibake names, alternative-fuel-only entries, closed-status
    rows, and below-confidence-floor rows, each counted in its own bucket.
    Checks run in a fixed order so every row lands in exactly one bucket;
    the sum of the bucket counts plus the kept count is asserted to equal
    the input row count.

    Returns `(kept, counts)` where `counts` covers exactly the four
    hygiene buckets (never `malformed_row` -- that bucket is populated by
    `transform()` itself, at parse time and at price-assignment time, not
    here).
    """
    counts = {bucket: 0 for bucket in OVERTURE_HYGIENE_BUCKETS if bucket != "malformed_row"}
    kept = []

    for row in rows:
        if overture_scope.has_mojibake(row.name):
            counts["mojibake"] += 1
            continue
        if overture_scope.is_alt_fuel_only(row.name):
            counts["alt_fuel_only"] += 1
            continue
        # A NULL/blank operating_status means unknown, not closed -- see
        # overture_scope.is_closed_status' own docstring. Roughly 35.8% of
        # dead-zone rows carry no status at all; a rule expressed as
        # anything other than membership in the pinned closed-value set
        # would silently discard about a third of legitimate candidates
        # while inflating this bucket with rows that were never a hygiene
        # problem. This retained-unknown behaviour is asserted by a test
        # (ImportOvertureStationsCommandTests), not merely assumed -- a
        # hygiene exclusion count anywhere near a third of the input row
        # count is the documented warning sign that this mistake was made.
        if overture_scope.is_closed_status(row.operating_status):
            counts["closed_status"] += 1
            continue
        if row.confidence < overture_scope.CONFIDENCE_FLOOR:
            counts["below_confidence_floor"] += 1
            continue
        kept.append(row)

    assert len(kept) + sum(counts.values()) == len(rows), (
        "hygiene bucket counts plus kept count must equal the input row count"
    )
    return kept, counts


def assign_brand(row):
    """Return `row` with a normalized chain token attached: matched first
    against the place name, then against the Overture-supplied brand
    field, else `None`. The upstream brand field may be an INPUT
    considered here but is never the alias table itself -- it is not
    guaranteed-stable and is inconsistently populated across rows for the
    same real-world chain (overture_scope.CHAIN_ALIASES' own docstring).

    This function only tags a row; it never drops one. A row filed as the
    truck-specific category is retained whether or not a chain token is
    found here -- D-02's category set was decided at the fetch boundary
    (overture_scope.CATEGORY_FILTER), not here, so "category or alias, not
    both" is a fact about what the fetch already selected, not a second
    filter this function applies.
    """
    token = overture_scope.chain_alias_for(row.name)
    if token is None and row.brand_name:
        token = overture_scope.chain_alias_for(row.brand_name)
    return replace(row, chain_token=token)


def assign_price(row):
    """Return `row` with its region and priced fields attached, or `None`
    if `row.address_region` cannot resolve to a region -- a blank or
    non-US state cell. That row is a malformed row, not a priced one; the
    caller routes a `None` result to the `malformed_row` bucket rather
    than guessing a region.

    `retail_price = price_min = price_max = regions.BASELINE_VALUES[region]`
    and `observation_count = 0`. The cancellation identity this rests on:

        indexed_price = retail_price * factor_for(state)
                       = BASELINE_VALUES[region] * (current_EIA[region] / BASELINE_VALUES[region])
                       = current_EIA[region]

    so the shown price **is** the current EIA regional average by
    construction (`routing/services/corridor.py`'s `factor_for` is the one
    application point) -- exactly what the `eia_regional_estimate` label
    claims, with no second constant and no reconstruction. Two rejected
    alternatives, recorded because both were seriously considered:
    `PADD_AVERAGE_PRICE` is an OPIS-retail basis, so after the division it
    yields a station-mix-weighted number rather than a regional estimate,
    and its CALIFORNIA entry rests on eight Imperial Valley stations that
    *are* the coverage hole this import exists to close; and a newly
    measured basis would be a third price basis in a codebase already
    carrying two plus a live factor, with no requirement asking for one.

    `observation_count` is 0, not 1: zero observations is the literal
    truth for an estimate-priced row (this row was never observed at a
    pump), `PositiveIntegerField` permits 0, and it makes an estimate row
    self-identifying in the database without consulting `price_source`.
    """
    region = regions.region_for_state(row.address_region)
    if region is None:
        return None
    price = regions.BASELINE_VALUES[region]
    return replace(
        row,
        region=region,
        retail_price=price,
        price_min=price,
        price_max=price,
        observation_count=0,
    )


def mint_identities(rows):
    """Apply `overture_scope.mint_opis_id` to each row's GERS id and
    return the rows with `opis_id` set. Maintains a dict from minted id to
    GERS id and raises `OvertureIdCollisionError` loudly, naming both GERS
    ids and the collided integer, on a second GERS id minting to an
    already-seen integer. Never drops, skips, deduplicates, or renumbers a
    row on collision."""
    minted_by_id = {}
    minted_rows = []

    for row in rows:
        opis_id = overture_scope.mint_opis_id(row.gers_id)
        seen_gers_id = minted_by_id.get(opis_id)
        if seen_gers_id is not None and seen_gers_id != row.gers_id:
            raise OvertureIdCollisionError(
                f"opis_id {opis_id} minted from both gers_id={seen_gers_id!r} "
                f"and gers_id={row.gers_id!r} -- refusing to drop, renumber, "
                "or overwrite either row"
            )
        minted_by_id[opis_id] = row.gers_id
        minted_rows.append(replace(row, opis_id=opis_id))

    return minted_rows


def to_station_rows(rows):
    """Assemble `OvertureStation` records from fully-processed rows (post
    hygiene, brand, price and identity). `geocode_precision` is the
    rooftop value -- Overture points are real coordinates snapped to a lot
    or building centroid roughly 50-150m out, which is rooftop-class
    against the 5-mile corridor window; no new precision value is used
    because `corridor.py` branches on equality with ROOFTOP at two call
    sites with a 20-mile else, so any third value would silently inherit
    the wider window at both. `rack_id` is the empty string -- Overture
    carries no rack identifier."""
    return [
        OvertureStation(
            opis_id=row.opis_id,
            name=row.name,
            address=row.address_freeform,
            city=row.address_locality,
            state=row.address_region,
            rack_id="",
            retail_price=row.retail_price,
            observation_count=row.observation_count,
            price_min=row.price_min,
            price_max=row.price_max,
            latitude=row.latitude,
            longitude=row.longitude,
            geocode_precision=_GEOCODE_PRECISION_ROOFTOP,
            geocode_status=_GEOCODE_STATUS_OK,
            price_source=_PRICE_SOURCE_EIA_REGIONAL_ESTIMATE,
            source=_STATION_SOURCE_OVERTURE,
            gers_id=row.gers_id,
        )
        for row in rows
    ]


def transform(reader):
    """The single entry point: `csv.DictReader` in, `(station_rows,
    OvertureImportReport)` out. Runs parse -> hygiene -> brand -> price ->
    mint -> row assembly, sorting the minted rows by `opis_id` ascending
    before assembly so output row order is a deterministic function of the
    input, never of dict or set iteration. Returns a report object; does
    not print anything -- that is the command's job."""
    parsed_rows, malformed_parse_count = parse_extract_rows(reader)
    hygiene_kept, bucket_counts = apply_hygiene(parsed_rows)
    bucket_counts = dict(bucket_counts)
    bucket_counts["malformed_row"] = malformed_parse_count

    branded = [assign_brand(row) for row in hygiene_kept]

    priced = []
    for row in branded:
        priced_row = assign_price(row)
        if priced_row is None:
            bucket_counts["malformed_row"] += 1
            continue
        priced.append(priced_row)

    # Plan 22-11 inserts routing.pipeline.overture_dedupe.deduplicate()
    # here, right before minting/row assembly, once the caller has loaded
    # and passed in the existing dataset (data/stations_geocoded.csv) --
    # see 22-11-PLAN.md. `priced` is exactly the list that future call
    # would consume and return a filtered version of; this plan leaves
    # every row here to keep transform() correct with no dedup stage yet.
    minted = mint_identities(priced)
    minted_sorted = sorted(minted, key=lambda r: r.opis_id)
    station_rows = to_station_rows(minted_sorted)

    priced_row_counts_by_region = {}
    for row in minted_sorted:
        priced_row_counts_by_region[row.region] = (
            priced_row_counts_by_region.get(row.region, 0) + 1
        )

    report = OvertureImportReport(
        release=overture_scope.OVERTURE_RELEASE,
        boxes=overture_scope.GAP_FILL_BOXES,
        category_filter=overture_scope.CATEGORY_FILTER,
        confidence_floor=overture_scope.CONFIDENCE_FLOOR,
        input_row_count=len(parsed_rows) + malformed_parse_count,
        bucket_counts=bucket_counts,
        kept_count=len(station_rows),
        priced_row_counts_by_region=priced_row_counts_by_region,
    )
    return station_rows, report

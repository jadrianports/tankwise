"""Every parameter that decides what the Overture gap-fill import contains.

Pure module -- no Django import, no ORM, no network, stdlib only. Every
constant below is pinned before any measurement exists: the row count the
pinned filter yields on the pinned release is the answer, recorded once and
never adjusted after a dispatch diff is seen. This is the same
pin-then-measure discipline this codebase already applies to
`CORPUS_PARAMS`, `PRUNE_CORPUS_PARAMS` and `MARGIN_LADDER` -- a value
re-tuned after a measurement would launder a scope change as a finding.

Imported by `fetch_overture_extract`, `routing/pipeline/overture.py` and
`routing/pipeline/overture_dedupe.py`, plus the gap-fill pre-declaration that
predicts which corridors may legitimately move. Collecting every pinned
value in one importable module -- rather than inline in each command -- is
what makes the pin-then-measure discipline enforceable: a value cannot be
quietly re-tuned in one consumer after a downstream measurement is seen,
because every consumer imports the same name.

`routing/services/` may never import this module (see
`routing/tests/test_boundaries.py`'s `ImportBoundaryTest`, which forbids
`routing.pipeline` imports inside the request-path layer) -- this module and
its consumers are strictly offline/import-time tooling.

Recorded fact, not a task: the upstream `categories` field this module reads
is deprecated as of the pinned release and scheduled for removal in the
September 2026 release, replaced by `basic_category` and `taxonomy`. All
three fields coexist in the pinned release, and `CATEGORY_FILTER` below is
correct against it, but a refresh against any later release must migrate the
field this module reads `categories.primary` from.
"""
import re
from hashlib import sha256
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Release, licence and source location
# ---------------------------------------------------------------------------

# The exact pinned release string -- never the word that means "newest
# available". Overture ships a new release roughly monthly; this is the
# current release as of 2026-08-08, the latest in an observed monthly
# sequence extending back through 2026-01-21. Bumping this string to track a
# later release is Phase 23's refresh pipeline, not a local edit here.
OVERTURE_RELEASE = "2026-07-22.0"

OVERTURE_LICENCE = "CDLA-Permissive-2.0"

# Echoed verbatim into the repo-root NOTICE file so both carry the same
# attribution text; a test binds the two together.
OVERTURE_ATTRIBUTION = (
    "Data derived from the Overture Maps Foundation Places theme, used "
    "under the Community Data License Agreement - Permissive - Version 2.0."
)

# Hive-partitioned S3 URI shape for the Places theme, anonymous read access
# (no AWS credentials required). `OVERTURE_S3_REGION` is the bucket's own
# region, needed by any S3 client configuration even though access is
# unauthenticated.
OVERTURE_S3_PATH_TEMPLATE = (
    "s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*.parquet"
)
OVERTURE_S3_REGION = "us-west-2"


def overture_s3_path() -> str:
    """Format `OVERTURE_S3_PATH_TEMPLATE` against the pinned release."""
    return OVERTURE_S3_PATH_TEMPLATE.format(release=OVERTURE_RELEASE)


# ---------------------------------------------------------------------------
# Scope geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GapFillBox:
    """One rectangle of the gap-fill multi-box, in (lng, lat) bounds."""

    xmin: float
    xmax: float
    ymin: float
    ymax: float
    label: str


# The scope geometry is an I-5-shaped multi-box, not the west-coast probe
# rectangle a prior live probe used (that rectangle stopped at lng -120 and
# so left the LA-to-San-Diego leg of the route this phase must cover
# uncovered). I-5 itself runs to -118.9 at the Grapevine, -118.2 through Los
# Angeles and -117.2 into San Diego, so covering that route end to end
# requires two boxes, not one.
#
# Three alternatives were considered and rejected:
#   - Reproducing the probed rectangle exactly: leaves LA -> San Diego
#     uncovered, so the route this phase must serve would still fail.
#   - One wide rectangle spanning both bands: buys eastern CA/NV desert rows
#     no pinned corridor uses, spending dispatch headroom for nothing.
#   - CA/OR/WA statewide: adds density to the already-covered Willamette
#     Valley and Puget Sound instead of the actually-uncovered stretch.
#
# Every mile of extra bbox spends dispatch headroom that a later
# measurement then has to account for, so the geometry is the narrowest
# shape that actually covers the named route end to end.
GAP_FILL_BOXES = (
    GapFillBox(
        xmin=-124.0,
        xmax=-120.0,
        ymin=35.0,
        ymax=44.0,
        label="I-5 corridor: the Grapevine north through southern Oregon",
    ),
    GapFillBox(
        xmin=-119.0,
        xmax=-116.5,
        ymin=32.5,
        ymax=35.0,
        label="SoCal extension: the Los Angeles to San Diego leg",
    ),
)


def contains(lat, lng) -> bool:
    """True when (lat, lng) falls inside the union of `GAP_FILL_BOXES`."""
    for box in GAP_FILL_BOXES:
        if box.ymin <= lat <= box.ymax and box.xmin <= lng <= box.xmax:
            return True
    return False


def bbox_predicate_sql() -> str:
    """OR-of-two-range-predicates SQL fragment against a Parquet row's own
    `bbox.xmin`/`bbox.xmax`/`bbox.ymin`/`bbox.ymax` fields (the Overture
    Places bbox struct), testing rectangle overlap against each gap-fill
    box. This is the predicate the fetch command pushes down to DuckDB so
    the S3 scan itself is bbox-filtered rather than filtering client-side
    after a full read.
    """
    clauses = []
    for box in GAP_FILL_BOXES:
        clauses.append(
            "(bbox.xmin <= {xmax} AND bbox.xmax >= {xmin} "
            "AND bbox.ymin <= {ymax} AND bbox.ymax >= {ymin})".format(
                xmin=box.xmin, xmax=box.xmax, ymin=box.ymin, ymax=box.ymax
            )
        )
    return " OR ".join(clauses)


# ---------------------------------------------------------------------------
# Category and confidence filter
# ---------------------------------------------------------------------------

# A prior live probe measured `truck_gas_station` alone selecting only 48
# rows in the dead zone, while Pilot, TravelCenters of America, Flying J and
# Petro locations all file as plain `gas_station` -- filtering on
# `truck_gas_station` alone drops exactly the chains a long-haul driver
# plans a route around. Including plain `gas_station` and relying on the
# chain-alias table plus the confidence floor below to separate the
# truck-capable majors from car-scale stations was chosen over admitting
# every fuel POI above a confidence floor (~4,823 rows in the same probe,
# mostly car-scale stations a loaded semi cannot use) -- that alternative
# was rejected because it guarantees feasibility on paper while resting the
# feasibility claim on stops that are not real for this vehicle class.
CATEGORY_FILTER = ("gas_station", "truck_gas_station")

# Pinned before any measurement. Overture's published confidence band is
# 0-1; Overture documents a `confidence` of 0 as pairing exclusively with a
# `permanently_closed` operating status, so any floor strictly above 0
# removes that cohort by construction. A prior probe found roughly 76% of
# lower-48 fuel POIs score >= 0.9, so a 0.9 floor was considered and
# rejected -- it would discard on the order of a quarter of the candidate
# pool, including the middle-confidence band where the miscategorized major
# chains sit. 0.5 is the midpoint of the published band, chosen for that
# reason and not tuned to any observed row count. This value is not
# adjusted after the row count or a dispatch diff is seen.
CONFIDENCE_FLOOR = 0.5


# ---------------------------------------------------------------------------
# Hygiene: closed status
# ---------------------------------------------------------------------------

# The pinned release's observed `operating_status` value set inside the
# gap-fill bbox is `open` / `permanently_closed` / NULL, with roughly 35.8%
# of rows NULL. A NULL status means unknown -- it carries no closed signal
# at all -- and must survive the filter. Expressing this rule as anything
# other than membership in an explicit closed-value set (for example, as a
# negation of the open value) would silently discard about a third of
# legitimate candidates while inflating the hygiene-exclusion count in the
# committed report with rows that were never a hygiene problem. A hygiene
# exclusion count anywhere near 35% of the pre-filter row count is the
# warning sign that this mistake was made.
CLOSED_OPERATING_STATUS_VALUES = frozenset({"permanently_closed", "temporarily_closed"})


def is_closed_status(value) -> bool:
    """True only when `value`, casefolded and stripped, is a member of
    `CLOSED_OPERATING_STATUS_VALUES`. `None` and the empty string are not
    closed -- they are unknown, and unknown rows are retained."""
    if not value:
        return False
    return value.strip().casefold() in CLOSED_OPERATING_STATUS_VALUES


# ---------------------------------------------------------------------------
# Hygiene: alternative-fuel-only entries
# ---------------------------------------------------------------------------

# Conventional #2 diesel is the fuel this solver plans a route around. A
# prior probe found 5 dead-zone rows named as an alternative-energy entry of
# a major chain (for example a charging-only listing sharing a chain's
# name); these are dropped outright as hygiene, not treated as a diesel
# fuel stop this solver could route a plan through.
ALT_FUEL_NAME_TOKENS = frozenset(
    {
        "cng",
        "lng",
        "hydrogen",
        "h2",
        "supercharger",
        "chargepoint",
        "evgo",
        "blink",
        "alternative energy",
        "ev charging",
        "electrify america",
    }
)

_ALT_FUEL_SINGLE_TOKENS = frozenset(t for t in ALT_FUEL_NAME_TOKENS if " " not in t)
_ALT_FUEL_PHRASE_TOKENS = frozenset(t for t in ALT_FUEL_NAME_TOKENS if " " in t)


def _name_tokens(name):
    normalized = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    return normalized.split()


def is_alt_fuel_only(name) -> bool:
    """True when `name` carries a whole-token or whole-phrase
    alternative-fuel-only marker, matched against `ALT_FUEL_NAME_TOKENS`."""
    tokens = _name_tokens(name)
    if set(tokens) & _ALT_FUEL_SINGLE_TOKENS:
        return True
    bigrams = {" ".join(pair) for pair in zip(tokens, tokens[1:])}
    return bool(bigrams & _ALT_FUEL_PHRASE_TOKENS)


# ---------------------------------------------------------------------------
# Hygiene: mojibake
# ---------------------------------------------------------------------------

# These rows are dropped outright with no encoding recovery attempted. A
# prior probe's dead-zone sample carried a name mangled with the Unicode
# replacement character, e.g. "Pilot � Dunnigan, CA" -- the source name
# was corrupted upstream of this import and is not something this pipeline
# can safely reconstruct.
MOJIBAKE_MARKERS = frozenset(
    {
        "�",
        "Ã¢â‚¬",  # mis-decoded UTF-8 em-dash/quote run
        "Ã©",  # mis-decoded UTF-8 "e" with acute accent
        "Ã±",  # mis-decoded UTF-8 "n" with tilde
        "Ã¢€™",  # mis-decoded UTF-8 right single quote
    }
)


def has_mojibake(name) -> bool:
    """True when `name` contains any marker in `MOJIBAKE_MARKERS`."""
    return any(marker in name for marker in MOJIBAKE_MARKERS)


# ---------------------------------------------------------------------------
# Hygiene: chain-alias table
# ---------------------------------------------------------------------------

# Hand-authored, pinned in code -- no fuzzy or edit-distance matching
# anywhere. Overture's own `brand` field may be an INPUT considered when
# authoring this table but is never the table itself: it is not
# guaranteed-stable and is inconsistently populated across rows for the
# same real-world chain, so this table is the single source of truth for
# chain identity, matched against the normalized station name only.
CHAIN_ALIASES = {
    "pilot": "PILOT",
    "pilot travel center": "PILOT",
    "flying j": "FLYING J",
    "flying j travel center": "FLYING J",
    "loves": "LOVES",
    "loves travel stop": "LOVES",
    "ta": "TA",
    "ta travel center": "TA",
    "ta petro": "TA",
    "travelcenters of america": "TA",
    "petro": "PETRO",
    "petro travel center": "PETRO",
    "sapp bros": "SAPP BROS",
    "roadys": "ROADYS",
}

# Sorted longest-alias-first so a multi-token alias (e.g. "ta petro") is
# tried before any of its single-token members -- the substring trap this
# table exists to avoid is a naive containment test matching a two-letter
# alias like "ta" inside an unrelated city name such as "Atlanta"; matching
# on whole, contiguous tokens rather than substrings avoids that trap by
# construction, and checking the longest phrases first is what makes
# "longest alias wins" true when more than one alias could match the same
# position.
_CHAIN_ALIAS_ENTRIES = sorted(
    CHAIN_ALIASES.items(), key=lambda item: -len(item[0].split())
)


def normalize_brand(name):
    """Casefold, strip apostrophes and other punctuation, collapse
    whitespace, and return the token list."""
    text = name.casefold().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return text.split()


def chain_alias_for(name):
    """Return the canonical chain token when the normalized token sequence
    of `name` contains an alias phrase from `CHAIN_ALIASES` as a contiguous
    whole-token subsequence, else `None`. Matching is whole-token only --
    `"ta"` never matches inside `"atlanta"` because the tokens `"ta"` and
    `"atlanta"` are never equal, regardless of one being a substring of the
    other."""
    tokens = normalize_brand(name)
    for alias_phrase, canonical in _CHAIN_ALIAS_ENTRIES:
        alias_tokens = tuple(alias_phrase.split())
        span = len(alias_tokens)
        for start in range(len(tokens) - span + 1):
            if tuple(tokens[start : start + span]) == alias_tokens:
                return canonical
    return None


# ---------------------------------------------------------------------------
# Dedup tier thresholds
# ---------------------------------------------------------------------------

# Pinned from a stated error budget, before any dedup run: Overture's own
# POI snap error is roughly 150m, plus the existing committed row's US
# Census rooftop geocode error, plus roughly half a typical truck-stop lot's
# long axis, summing to approximately 400m, which is 0.25 miles. The two
# neighbouring values are published in the committed report as sensitivity
# information only, never as alternate shipped values. The shipped
# threshold moves only via a recorded human decision citing spot-check
# evidence -- never citing row count and never citing a dispatch result.
TIGHT_TIER_THRESHOLD_MI = 0.25
TIGHT_TIER_SENSITIVITY_MI = (0.15, 0.40)

# The N densest clusters by candidate count inside the multi-box, plus any
# cluster containing a match whose distance falls within 20% of
# `TIGHT_TIER_THRESHOLD_MI`, pinned before the run. Every selected cluster
# is reported whether it looks good or bad, so the sample cannot drift
# toward the clusters that happen to look right.
SPOT_CHECK_CLUSTER_COUNT = 8

# Phase 17's own committed corridor-geometry fixture threshold
# (`fetch_corridor_geometry.SIZE_FINDING_THRESHOLD_BYTES = 1_500_000`) is the
# precedent shape. The category-filtered dead-zone extract is expected
# around 1-2 MB, so 3 MB is deliberate headroom above that estimate; a
# breach pauses for a decision rather than failing silently.
EXTRACT_SIZE_FINDING_THRESHOLD_BYTES = 3_000_000


# Every pinned constant above, named so the anti-drift test can assert the
# pinned set by name rather than by inspecting the module by hand.
SCOPE_PARAM_NAMES = (
    "OVERTURE_RELEASE",
    "OVERTURE_LICENCE",
    "OVERTURE_ATTRIBUTION",
    "OVERTURE_S3_PATH_TEMPLATE",
    "OVERTURE_S3_REGION",
    "GAP_FILL_BOXES",
    "CATEGORY_FILTER",
    "CONFIDENCE_FLOOR",
    "CLOSED_OPERATING_STATUS_VALUES",
    "ALT_FUEL_NAME_TOKENS",
    "MOJIBAKE_MARKERS",
    "CHAIN_ALIASES",
    "TIGHT_TIER_THRESHOLD_MI",
    "TIGHT_TIER_SENSITIVITY_MI",
    "SPOT_CHECK_CLUSTER_COUNT",
    "EXTRACT_SIZE_FINDING_THRESHOLD_BYTES",
)


# ---------------------------------------------------------------------------
# GERS-derived ID minting
# ---------------------------------------------------------------------------

# `opis_id` is minted as a pure function of the upstream GERS entity id,
# rather than a sorted-index offset or a running counter, because either
# alternative shifts every subsequent id the moment one new upstream row is
# inserted -- the same real station would not keep the same integer across
# imports. `opis_id` is also the third key in the DP's deterministic
# tie-break order, so an id that silently moves changes which plan a route
# gets.
#
# A modulus is applied to the hash digest rather than truncating the raw
# hash width, because without it, range control belongs to the hash
# function and "provably disjoint from every OPIS id" becomes an argument
# rather than an assertion.
#
# The reserved span is high, `[1_000_000_000, 2_000_000_000)`, rather than
# negative:
#   - `BASE + SPAN - 1 = 1_999_999_999`, under the signed-32-bit ceiling of
#     `2_147_483_647` that `Station.opis_id` (a Django `IntegerField`)
#     requires.
#   - It sits four orders of magnitude above today's maximum real OPIS id
#     of 73,131, leaving the whole 73k-to-1e9 gap free for OPIS growth.
#   - Decisively: higher ids sort last in the DP's tie-break order, so a
#     real-priced OPIS station wins a tie against an estimate-priced
#     Overture one -- the same direction the trust margin already points. A
#     negative span would invert exactly that.
OVERTURE_ID_BASE = 1_000_000_000
OVERTURE_ID_SPAN = 1_000_000_000
OVERTURE_ID_HASH_HEX_WIDTH = 12

OVERTURE_ID_RANGE = (OVERTURE_ID_BASE, OVERTURE_ID_BASE + OVERTURE_ID_SPAN)


def mint_opis_id(gers_id: str) -> int:
    """Deterministically mint an int32-safe `opis_id` from a GERS entity id
    string. 12 hex digits is 48 bits of hash output, well above the ~30
    bits needed to make the modulo reduction over `OVERTURE_ID_SPAN`
    statistically uniform (the bias from 2**48 not being an exact multiple
    of 1e9 is on the order of 1 part in 281,475, negligible). The
    collision probability is therefore a pure birthday question over a
    uniform space of size `OVERTURE_ID_SPAN`: approximately
    `1 - exp(-n*(n-1) / (2 * OVERTURE_ID_SPAN))`, which works out to
    roughly 1.2% at n = 5,000 and 1.8% at n = 6,000 -- the live-measured
    pre-hygiene candidate scale this import operates at. That is not
    negligible, which is why the import asserts loudly on collision rather
    than dropping a row silently."""
    digest = sha256(gers_id.encode()).hexdigest()[:OVERTURE_ID_HASH_HEX_WIDTH]
    return OVERTURE_ID_BASE + int(digest, 16) % OVERTURE_ID_SPAN


def is_overture_id(value) -> bool:
    """True when `value` falls inside `OVERTURE_ID_RANGE` -- the one shared
    definition every disjointness assertion is checked against."""
    return OVERTURE_ID_RANGE[0] <= value < OVERTURE_ID_RANGE[1]


# A permanent, reproducible synthetic collision witness: two distinct GERS
# id strings that mint to the identical integer under the exact formula
# above. A natural collision is unlikely inside this import's real row
# count, so a test that only feeds the committed extract through the
# collision guard would pass vacuously on a lucky run and prove nothing
# about whether the guard's raise branch is actually reachable -- the same
# vacuity class as an invariance check aimed at a file that does not exist.
# If this pinned pair ever fails to reproduce (for example, on a future
# Python whose `hashlib` implementation differs), the correct response is
# to regenerate a fresh pair by brute-forcing random UUID v4 candidates
# through `mint_opis_id` (expected order 10**4-10**5 trials) and pin that,
# never to weaken or delete the witness test itself.
COLLISION_WITNESS_PAIR = (
    "a73d1c9a-91d9-4121-a260-62d9b44284d4",
    "2df04625-917d-4309-9b98-c903ececdc32",
)
COLLISION_WITNESS_MINTED_ID = 1_220_971_289

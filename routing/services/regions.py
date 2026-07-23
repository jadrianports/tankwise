"""State -> EIA sub-PADD region mapping, baseline denominators, and the
factor sanity-clamp band.

Pure module -- no Django, ORM, HTTP, or cache import. Consumed server-side
only by corridor.py (factor application seam), eia.py (the client this
module's facet codes drive), and the view's route-dominant-region calc.
Mirrors corridor.py's `_as_decimal` discipline: every numeric constant is a
`Decimal(str(...))` literal, never a bare float.

Region -> EIA v2 facet code cross-reference (confirmed live, Plan 12-01):
    region code    duoarea   product      states
    PADD1A         R1X       EPD2D        CT, ME, MA, NH, RI, VT
    PADD1B         R1Y       EPD2D        DE, DC, MD, NJ, NY, PA
    PADD1C         R1Z       EPD2D        FL, GA, NC, SC, VA, WV
    PADD2          R20       EPD2D        IL, IN, IA, KS, KY, MI, MN, MO, NE,
                                          ND, SD, OH, OK, TN, WI
    PADD3          R30       EPD2D        AL, AR, LA, MS, NM, TX
    PADD4          R40       EPD2DXL0     CO, ID, MT, UT, WY
    PADD5_EX_CA    R5XCA     EPD2D        AK, AZ, HI, NV, OR, WA
    CALIFORNIA     SCA       EPD2D        CA

Facet key names confirmed by Plan 12-01's live metadata probe against
`petroleum/pri/gnd`: region facet id is `duoarea`, diesel-type facet id is
`product` (both match this research's best guess exactly; see
`12-01-SUMMARY.md`). Plan 12-01 also found PADD 4 (R40) rows exist under
BOTH `EPD2D` and `EPD2DXL0` in the live data (identical values) -- eia.py's
parser must dedupe by region rather than assume a clean per-region product
split; `REGION_PRODUCT["PADD4"] = "EPD2DXL0"` below is used to select the
row when both are present, not because `EPD2D` is absent for PADD4.

Baseline denominators (D-05 provenance): EIA API v2 petroleum/pri/gnd,
region facet `duoarea`, week 2024-12-30 (nearest to the 2025-01-01
FUEL_PRICE_AS_OF snapshot vintage), fetched 2026-07-23 via one live
authenticated call (Plan 12-01). Units: $/gallon. These are real,
provenance-backed constants -- only the CURRENT week is ever fetched live;
the baseline never is.
"""
from decimal import Decimal

REGION_STATES = {
    "PADD1A": frozenset({"CT", "ME", "MA", "NH", "RI", "VT"}),
    "PADD1B": frozenset({"DE", "DC", "MD", "NJ", "NY", "PA"}),
    "PADD1C": frozenset({"FL", "GA", "NC", "SC", "VA", "WV"}),
    "PADD2": frozenset(
        {
            "IL", "IN", "IA", "KS", "KY", "MI", "MN", "MO", "NE", "ND",
            "SD", "OH", "OK", "TN", "WI",
        }
    ),
    "PADD3": frozenset({"AL", "AR", "LA", "MS", "NM", "TX"}),
    "PADD4": frozenset({"CO", "ID", "MT", "UT", "WY"}),
    "PADD5_EX_CA": frozenset({"AK", "AZ", "HI", "NV", "OR", "WA"}),
    "CALIFORNIA": frozenset({"CA"}),
}

REGION_LABELS = {
    "PADD1A": "New England",
    "PADD1B": "Central Atlantic",
    "PADD1C": "Lower Atlantic",
    "PADD2": "Midwest",
    "PADD3": "Gulf Coast",
    "PADD4": "Rocky Mountain",
    "PADD5_EX_CA": "West Coast",
    "CALIFORNIA": "California",
}

# EIA v2 facet value for the `duoarea` (region) facet.
REGION_DUOAREA = {
    "PADD1A": "R1X",
    "PADD1B": "R1Y",
    "PADD1C": "R1Z",
    "PADD2": "R20",
    "PADD3": "R30",
    "PADD4": "R40",
    "PADD5_EX_CA": "R5XCA",
    "CALIFORNIA": "SCA",
}

# EIA v2 facet value for the `product` (diesel-type) facet. EPD2D ("All
# Types" No. 2 Diesel) for every region except PADD4, which selects
# EPD2DXL0 (ULSD) -- see module docstring for the both-codes-present nuance.
REGION_PRODUCT = {
    "PADD1A": "EPD2D",
    "PADD1B": "EPD2D",
    "PADD1C": "EPD2D",
    "PADD2": "EPD2D",
    "PADD3": "EPD2D",
    "PADD4": "EPD2DXL0",
    "PADD5_EX_CA": "EPD2D",
    "CALIFORNIA": "EPD2D",
}

ALL_REGION_CODES = (
    "PADD1A",
    "PADD1B",
    "PADD1C",
    "PADD2",
    "PADD3",
    "PADD4",
    "PADD5_EX_CA",
    "CALIFORNIA",
)

# Baseline (denominator) $/gallon values, week 2024-12-30 -- see module
# docstring for provenance. `factor = current_value / BASELINE_VALUES[region]`.
BASELINE_VALUES = {
    "PADD1A": Decimal("3.753"),
    "PADD1B": Decimal("3.774"),
    "PADD1C": Decimal("3.501"),
    "PADD2": Decimal("3.469"),
    "PADD3": Decimal("3.196"),
    "PADD4": Decimal("3.37"),
    "PADD5_EX_CA": Decimal("3.705"),
    "CALIFORNIA": Decimal("4.576"),
}

# Sanity clamp band (Claude's Discretion). An out-of-band factor is treated
# as corrupt and routed through the same fallback path as EIA-unreachable.
FACTOR_CLAMP_MIN = Decimal("0.5")
FACTOR_CLAMP_MAX = Decimal("2.0")

# Reverse index (state -> region), built once at import for O(1) lookup.
_STATE_TO_REGION = {
    state: region
    for region, states in REGION_STATES.items()
    for state in states
}


def region_for_state(state):
    """Return the region code for a USPS state code, or `None` if the code
    is unknown/blank/foreign (D-03 neutral-factor fallback -- the caller
    applies factor 1.0 for a `None` result). Case-insensitive."""
    if not state:
        return None
    return _STATE_TO_REGION.get(state.strip().upper())


def clamp_factor(factor):
    """Return `factor` if within [FACTOR_CLAMP_MIN, FACTOR_CLAMP_MAX], else
    `None` to signal "out-of-band, treat as corrupt" -- the caller (eia.py)
    routes a `None` result through the same fallback path as
    EIA-unreachable."""
    if FACTOR_CLAMP_MIN <= factor <= FACTOR_CLAMP_MAX:
        return factor
    return None


def dominant_region(region_codes):
    """Return the plurality region code from an ordered list of region
    codes (one per chosen fuel stop, in stop order). Ties resolve toward
    the earliest-occurring region in the list. Returns `None` for an empty
    list. This is the pure core of D-06's route-dominant-region selection."""
    if not region_codes:
        return None
    counts = {}
    first_seen_order = []
    for code in region_codes:
        if code not in counts:
            counts[code] = 0
            first_seen_order.append(code)
        counts[code] += 1
    best_code = None
    best_count = -1
    for code in first_seen_order:
        if counts[code] > best_count:
            best_count = counts[code]
            best_code = code
    return best_code

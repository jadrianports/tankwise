"""EIA v2 client: regional diesel price index factor table.

Request-path HTTP + Django settings only -- no `routing.models`/
`routing.pipeline` import. Every EIA numeric value is exact, unrounded
`Decimal`, consistent with the project's money/measure discipline; the
`EIA_API_KEY` always rides in `requests.get(params=...)`, never
interpolated into the URL string.

Unlike `mapbox.py`'s fail-loud `ImproperlyConfigured` contract,
`get_factor_table()` deliberately CATCHES both `ImproperlyConfigured`
(missing key) and `EiaRequestError` and degrades to a stale or frozen
factor table -- it never lets either propagate to a 500 (D-20/EIA-03).
"""
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from routing.services import regions

EIA_DATA_URL = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"

CURRENT_KEY = "eia:factors:current"
LAST_KNOWN_KEY = "eia:factors:last_known"
COOLDOWN_KEY = "eia:cooldown"
CURRENT_TTL = 86400  # D-14: ~24h fixed (EIA publishes weekly)
COOLDOWN_TTL = 900  # D-18: ~15min negative-cache after a failed fetch

# One pooled keep-alive session for all EIA calls. EIA is a single
# low-frequency (lazy, ~24h TTL) call, so the pool is sized smaller than
# mapbox.py's per-request pool. The bounded Retry recovers transient
# 5xx/429 and a stale reused connection; it does NOT retry auth/4xx --
# the app's own status-code check owns the final response.
_RETRY = Retry(
    total=2,
    connect=2,
    read=2,
    status=2,
    backoff_factor=0.3,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
)
_SESSION = requests.Session()
_SESSION.mount(
    "https://",
    HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=_RETRY),
)


class EiaError(Exception):
    """Base class for all EIA client errors."""


class EiaRequestError(EiaError):
    """The EIA request itself failed: a non-200 HTTP status or a
    transport-level failure (connection error, timeout). The caller
    (`get_factor_table`) degrades to last-known or frozen factors --
    this error never propagates to the view layer."""


def fetch_current_week() -> dict:
    """Fetch the current week's regional diesel prices in one EIA v2
    call and return the parsed factor table.

    Raises `ImproperlyConfigured` if `settings.EIA_API_KEY` is unset,
    before any HTTP call is attempted -- the caller (`get_factor_table`)
    catches this and degrades to frozen-snapshot mode (D-20), unlike
    Mapbox's fail-loud token contract. Raises `EiaRequestError` on a
    non-200 status or a `requests` transport failure.
    """
    if not settings.EIA_API_KEY:
        raise ImproperlyConfigured(
            "EIA_API_KEY is not set -- cannot call the EIA API"
        )

    try:
        response = _SESSION.get(
            EIA_DATA_URL,
            params={
                "api_key": settings.EIA_API_KEY,
                "frequency": "weekly",
                "data[0]": "value",
                "facets[duoarea][]": [
                    regions.REGION_DUOAREA[region]
                    for region in regions.ALL_REGION_CODES
                ],
                "facets[product][]": ["EPD2D", "EPD2DXL0"],
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",
                # 8 regions x 2 product codes x 2 weeks -- enough rows to
                # dedupe by region/product and still see a prior week for
                # the week-over-week trend delta.
                "length": len(regions.ALL_REGION_CODES) * 2 * 2,
            },
            timeout=3,  # D-13: hard timeout, only the first request after
            # TTL expiry can ever block, and only for <=3s.
        )
    except requests.RequestException as exc:
        raise EiaRequestError("EIA request failed") from exc

    if response.status_code != 200:
        raise EiaRequestError(
            f"EIA request failed with status {response.status_code}"
        )

    return _parse_eia_response(response.json())


def _parse_eia_response(data) -> dict:
    """Parse an EIA v2 `petroleum/pri/gnd` JSON response into a factor
    table. Kept separate from the transport call so it is
    fixture-testable offline.

    For each region, selects rows matching that region's confirmed
    (duoarea, product) facet pair (`regions.REGION_DUOAREA`/
    `REGION_PRODUCT`) -- this dedupes the response, which may carry the
    same region under BOTH product codes (confirmed live by Plan 12-01;
    the request above deliberately asks for both codes across all
    regions, not just PADD4's ULSD-only series). Rows are sorted by
    `period` desc; the two most recent distinct periods become
    current/prior. A region with only one observed period (e.g. this
    phase's committed single-week fixture) reports prior_value ==
    current_value, i.e. delta_cents == 0 -- never an IndexError.

    A region whose current/baseline ratio is out-of-band
    (`regions.clamp_factor` -> None) is omitted entirely from the
    returned `factors` (and `deltas_cents`) dict -- never set to any
    value, never carried as None -- so `make_factor_lookup`'s
    `.get(region, Decimal("1"))` applies the neutral 1.0 default and a
    corrupt upstream value can never poison the table.
    """
    rows = data.get("response", {}).get("data") or []

    week = None
    factors = {}
    deltas_cents = {}

    for region in regions.ALL_REGION_CODES:
        duoarea = regions.REGION_DUOAREA[region]
        product = regions.REGION_PRODUCT[region]

        region_rows = [
            row
            for row in rows
            if row.get("duoarea") == duoarea and row.get("product") == product
        ]
        if not region_rows:
            continue

        # Read the week-identifying date straight from the row's own
        # `period` field -- never derived from datetime.now() (Pitfall 2:
        # EIA's weekly release-day timing has changed before; the
        # underlying data-as-of date is the only honest source of truth).
        region_rows.sort(key=lambda row: row["period"], reverse=True)

        current_row = region_rows[0]
        prior_row = region_rows[1] if len(region_rows) > 1 else current_row

        current_value = Decimal(str(current_row["value"]))
        prior_value = Decimal(str(prior_row["value"]))

        if week is None or current_row["period"] > week:
            week = current_row["period"]

        raw_factor = current_value / regions.BASELINE_VALUES[region]
        factor = regions.clamp_factor(raw_factor)
        if factor is None:
            # Out-of-band/corrupt -- omit this region entirely so the
            # neutral 1.0 default applies (V5 input validation, T-12-03-01).
            continue

        factors[region] = factor
        deltas_cents[region] = round((current_value - prior_value) * 100)

    return {"week": week, "factors": factors, "deltas_cents": deltas_cents}


def _frozen_table() -> dict:
    """The permanent-fallback table: no week, no factors, no deltas --
    `make_factor_lookup` yields the neutral 1.0 for every region (D-15)."""
    return {"week": None, "factors": {}, "deltas_cents": {}}


def get_factor_table(force_refresh=False):
    """Return `(table, status)` where `status` is one of `"current"`,
    `"stale"`, or `"frozen"`. This is the sole cache-aside seam: at most
    one EIA HTTP call happens per ~`CURRENT_TTL` window regardless of
    `/api/route` traffic volume -- never a per-request EIA call (EIA-01).

    Never raises: a missing `EIA_API_KEY`, an unreachable/slow/corrupt
    EIA response, or an out-of-band factor all degrade to last-known
    ("stale") or the frozen 1.0 table ("frozen") -- EIA-03.

    `force_refresh=True` (used by the `refresh_eia_factors` management
    command) bypasses the `current` cache read only -- it still respects
    an active cooldown, since that is the DoS-budget mitigation
    (T-12-03-02), not a freshness optimization.
    """
    if not force_refresh:
        cached = cache.get(CURRENT_KEY)
        if cached is not None:
            return cached, "current"

    if cache.get(COOLDOWN_KEY):
        last_known = cache.get(LAST_KNOWN_KEY)
        return (last_known, "stale") if last_known else (_frozen_table(), "frozen")

    try:
        table = fetch_current_week()
        if not table.get("factors"):
            raise EiaRequestError("EIA response carried no usable factors")
    except (EiaRequestError, ImproperlyConfigured):
        cache.set(COOLDOWN_KEY, True, timeout=COOLDOWN_TTL)
        last_known = cache.get(LAST_KNOWN_KEY)
        return (last_known, "stale") if last_known else (_frozen_table(), "frozen")

    cache.set(CURRENT_KEY, table, timeout=CURRENT_TTL)
    cache.set(LAST_KNOWN_KEY, table, timeout=None)  # never expires (D-15)
    return table, "current"


def make_factor_lookup(table):
    """Return a `factor_for(state)` closure over `table` -- a callable
    the `corridor.candidates()` seam threads through to price each
    candidate. Always returns a `Decimal`, never a float, so
    `station.retail_price * factor_for(state)` stays exact (D-04)."""

    def factor_for(state):
        region = regions.region_for_state(state)
        return table["factors"].get(region, Decimal("1"))

    return factor_for

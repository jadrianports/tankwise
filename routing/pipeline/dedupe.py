"""Collapse duplicate OPIS Truckstop ID rows into one representative Station
row per ID.

Pure, Django-free module: importable in isolation for unit tests and safely
reachable only from management commands (routing/pipeline/ boundary).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median_low

# Lower-48 US state postal codes. The source CSV's remaining codes are
# Canadian provinces (ON/AB/BC/MB/SK/YT/NS/QC/NB), anything not in this set
# is out_of_scope. No AK/HI/DC appear in this dataset.
LOWER_48_STATES = frozenset(
    {
        "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "ID", "IL", "IN",
        "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
        "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
        "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    }
)


@dataclass
class StationGroup:
    """One collapsed representative row for a single OPIS Truckstop ID."""

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
    out_of_scope: bool

    def mutable_fields(self):
        """Fields the import command upserts via update_or_create defaults.
        Does not include geocode_status/latitude/longitude directly, the
        command decides those based on out_of_scope invalidation.
        """
        return {
            "name": self.name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "rack_id": self.rack_id,
            "retail_price": self.retail_price,
            "observation_count": self.observation_count,
            "price_min": self.price_min,
            "price_max": self.price_max,
        }


@dataclass
class DedupeReport:
    """Split report of duplicate-ID groups."""

    total_rows: int = 0
    total_groups: int = 0
    exact_duplicate_group_count: int = 0
    conflicting_price_group_count: int = 0
    conflicting_price_spreads: list = field(default_factory=list)

    @property
    def duplicate_group_count(self):
        return self.exact_duplicate_group_count + self.conflicting_price_group_count

    @property
    def median_conflicting_spread(self):
        if not self.conflicting_price_spreads:
            return None
        return median_low(sorted(self.conflicting_price_spreads))

    @property
    def max_conflicting_spread(self):
        if not self.conflicting_price_spreads:
            return None
        return max(self.conflicting_price_spreads)


def _is_out_of_scope(state):
    return state.strip().upper() not in LOWER_48_STATES


def _select_name(names_in_order):
    """Longest variant wins; ties broken by first file occurrence."""
    best = names_in_order[0]
    for candidate in names_in_order[1:]:
        if len(candidate) > len(best):
            best = candidate
    return best


def collapse_duplicates(rows):
    """Group CSV rows by OPIS Truckstop ID and collapse each group to one
    representative StationGroup, preserving first-file-occurrence order.

    `rows` is the list of dicts from `csv.DictReader` over the source CSV
    with columns: "OPIS Truckstop ID", "Truckstop Name", "Address", "City",
    "State", "Rack ID", "Retail Price".

    Returns (groups, report) where groups is a list[StationGroup] in
    first-occurrence order and report is a DedupeReport.
    """
    order = []
    buckets = {}

    for row in rows:
        opis_id = int(row["OPIS Truckstop ID"])
        if opis_id not in buckets:
            order.append(opis_id)
            buckets[opis_id] = []
        buckets[opis_id].append(row)

    report = DedupeReport(total_rows=len(rows))
    groups = []

    for opis_id in order:
        bucket = buckets[opis_id]
        first = bucket[0]

        names = [r["Truckstop Name"] for r in bucket]
        prices = [Decimal(r["Retail Price"]) for r in bucket]

        representative_price = median_low(sorted(prices))
        price_min = min(prices)
        price_max = max(prices)

        if len(bucket) > 1:
            if price_min == price_max:
                report.exact_duplicate_group_count += 1
            else:
                report.conflicting_price_group_count += 1
                report.conflicting_price_spreads.append(price_max - price_min)

        groups.append(
            StationGroup(
                opis_id=opis_id,
                name=_select_name(names),
                address=first["Address"],
                city=first["City"].strip(),
                state=first["State"].strip().upper(),
                rack_id=first["Rack ID"],
                retail_price=representative_price,
                observation_count=len(bucket),
                price_min=price_min,
                price_max=price_max,
                out_of_scope=_is_out_of_scope(first["State"]),
            )
        )

    report.total_groups = len(groups)
    return groups, report

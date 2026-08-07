"""Measure how much routing.services.prune's domination rule shrinks the
candidate set, across the twelve pinned corridors, both pinned tank
ranges, and both pinned price bases (D-16/D-18).

Read-only: no writes, no database mutation, no network calls. The twelve
corridors were captured once, offline, by `fetch_corridor_geometry` and
committed as fixtures under routing/tests/fixtures/corridor_geometry/ --
this command replays those committed geometries through the existing
Directions-response parser and never invokes that capture command again.
It works with no routing-provider token set in the environment, because it
never constructs a routing-provider client at all.

Must NOT run in CI -- the figures below are evidence, not a pass/fail gate,
exactly as measure_penalty_disagreement.py and benchmark_corridor.py are
evidence for their own subjects. The CI-enforcing guard for this rule is
PruneReductionGuardTests in routing/tests/test_prune_soundness.py, which
asserts a closed-form equality plus a seeded-corpus floor on every commit.
This command exists to report the full, real-geometry table that guard
merely protects.

Per D-16/D-18, this module defines no corridor, tank range, or price basis
of its own: CORRIDORS, TANK_RANGES_MI, PRICE_BASES, factor_lookup_for_basis
and load_corridor_route are all imported from
routing.tests.test_corridor_fixtures, the single shared source of truth
pinned by plan 17-02 before any measurement was taken.

Four correctness guards (D-03 widened this from two to four -- guards 3
and 4 below are new), following benchmark_corridor.py's own
alternate-computation-must-agree idiom -- any one raises CommandError
rather than printing a table that quietly contradicts itself:

  1. A corridor's raw (pre-prune) candidate count must be identical across
     both price bases, since a price factor only scales money and can
     never change which stations sit inside the geometric corridor.
  2. `classify_removals()` (below) is written independently from
     `prune.py`'s own implementation, straight off the rule's three
     conditions (D-01 added the third, provenance-based one), and
     cross-checked: every removal it cannot attribute to either a
     co-located dominator or a tail dominator means the prune and an
     independent reading of its own stated rule disagree, and that must
     stop the report rather than appear silently inside it.
  3. Every margin-blocked RETENTION -- a candidate a price-only reading
     would have removed but the actual margin-aware prune keeps -- must
     be individually attributable to a specific estimate-priced dominator
     that a price-only reading would have accepted as sufficient. An
     unattributable one is the same disagreement as guard 2, in the
     retention direction rather than the removal direction.
  4. The closed-form equality `margin_blocked_count == price_only_removal
     _count - actual_removal_count` must hold exactly. Adding a third
     bucket without this equality would re-open the `prune(x) -> x`
     vacuity hole Phase 17 closed: a prune that retained everything would
     produce zero removals, zero unexplained removals, and a passing sum,
     with nothing left to say the third bucket ever did anything. The
     equality is a true set identity -- the margin-aware qualifying set
     is a subset of the price-only one, so margin-aware removals are a
     subset of price-only removals and their difference is exactly the
     margin-blocked set -- and it fails in both directions: a prune that
     over-prunes trips guards 2/3 above, a prune that under-prunes beyond
     what the margin justifies trips this one.

This report measures candidate-set reduction only and makes NO latency
claim -- Phase 18 criterion 5's measured solver latency is where that is
proven (D-24).
"""
from dataclasses import dataclass
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from routing.models import GeocodePrecision, Station
from routing.services import corridor
from routing.services.prune import prune_dominated_candidates
from routing.services.solver import is_estimate_priced
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    PRICE_BASES,
    TANK_RANGES_MI,
    factor_lookup_for_basis,
    load_corridor_route,
)

_TOTAL_ORDER_KEY = lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id)  # noqa: E731


@dataclass(frozen=True)
class CorridorReductionRow:
    """One measured (corridor, price basis, tank range) cell."""

    slug: str
    label: str
    total_route_mi: Decimal
    price_basis: str
    tank_range_mi: Decimal
    raw_count: int
    retained_count: int
    co_located_removals: int
    tail_removals: int
    margin_blocked_removals: int = 0


def _would_be_removed_under_price_only(candidate, earlier, *, tank_range_mi, total_route_mi):
    """Whether `candidate` would be removed under the PRE-margin,
    price-only reading of the rule -- conditions 1 (price) and 2
    (position) only, ignoring provenance entirely -- given `earlier`, the
    candidates ranked strictly before it under the D-11 total order.

    Written independently of `prune.py`'s implementation, straight off
    the rule's own two pre-margin conditions.
    """
    return any(
        b.price_per_gallon <= candidate.price_per_gallon
        and (
            b.distance_from_start_mi == candidate.distance_from_start_mi
            or b.distance_from_start_mi + tank_range_mi >= total_route_mi
        )
        for b in earlier
    )


def _margin_blocking_dominator_exists(candidate, earlier, *, tank_range_mi, total_route_mi):
    """Whether an earlier-ranked, estimate-priced B exists that would
    have qualified as `candidate`'s dominator under the price-only
    reading (price plus position), but is blocked by condition 3 (D-01)
    because `candidate` itself is real-priced. Only ever `True` for a
    real-priced `candidate` -- condition 3 restricts exactly and only
    "an estimate-priced station may not dominate a real-priced one", so
    an estimate-priced candidate can never be margin-blocked.
    """
    if is_estimate_priced(candidate):
        return False
    return any(
        is_estimate_priced(b)
        and b.price_per_gallon <= candidate.price_per_gallon
        and (
            b.distance_from_start_mi == candidate.distance_from_start_mi
            or b.distance_from_start_mi + tank_range_mi >= total_route_mi
        )
        for b in earlier
    )


def classify_removals(ordered, retained, *, tank_range_mi, total_route_mi):
    """Attribute every station removed by the prune to either a co-located
    or a tail dominator, AND every margin-blocked RETENTION to a specific
    estimate-priced dominator a price-only reading would have accepted --
    all derived independently from `prune.py`'s own implementation,
    straight off the rule's three admission conditions (D-01 added the
    third, provenance-based one) rather than by inspecting the code.

    `ordered`: the full candidate list sorted by the D-11 total order
    `(distance_from_start_mi, price_per_gallon, opis_id)`. `retained`: the
    survivors `prune_dominated_candidates` returned for the same
    `tank_range_mi`/`total_route_mi` -- membership is determined by object
    identity, since the prune returns input objects unchanged.

    For each removed station A, this looks for an earlier-ranked station B
    (earlier under the SAME total order) with `price_B <= price_A` AND
    NOT (B is estimate-priced while A is real-priced) -- condition 3's
    margin-aware qualifying set. Among all such B, A is classified
    `co_located` when at least one shares A's exact position
    (`pos_B == pos_A`) -- tested FIRST so the two buckets stay disjoint --
    otherwise `tail` when at least one has
    `pos_B + tank_range_mi >= total_route_mi`.

    Separately, for each RETAINED station A that a price-only reading
    (conditions 1 and 2 only, ignoring provenance) would have removed,
    this is a margin-blocked retention: A survives only because condition
    3 disqualified every dominator a price-only reading would have
    accepted. Each such A must be individually attributable to a specific
    earlier-ranked, estimate-priced B satisfying the price-only
    conditions -- not merely counted.

    Because the margin-aware qualifying set (used for the removal
    attribution above) is a STRICT SUBSET of the price-only qualifying set
    (D-03's extended closed-form equality), margin-aware removals are
    themselves a subset of price-only removals, and their difference is
    exactly the margin-blocked set: `margin_blocked_count ==
    price_only_removal_count - actual_removal_count`. Adding this third
    bucket without asserting that equality would re-open the
    `prune(x) -> x` vacuity hole Phase 17 closed -- a prune that retained
    everything would pass every OTHER check here with zero unexplained
    removals and a passing sum, leaving nothing to say the third bucket
    ever did anything; the equality closes that hole in both directions.

    Returns `(co_located_count, tail_count, margin_blocked_count)`.
    Raises `CommandError` if: any removal cannot be explained by either
    the co-located or the tail bucket; the co-located and tail tallies do
    not sum to the total number of removed stations; any margin-blocked
    retention cannot be individually attributed to a specific
    estimate-priced dominator; or `margin_blocked_count` does not equal
    the price-only removal count minus the actual removal count. Any of
    these means this independent reading of the rule disagrees with what
    the prune actually did, and the report must stop rather than
    mis-report.
    """
    retained_ids = {id(c) for c in retained}

    co_located = 0
    tail = 0
    unexplained = []
    margin_blocked = 0
    margin_blocked_unexplained = []
    price_only_removed_count = 0

    for index, candidate in enumerate(ordered):
        earlier = ordered[:index]
        is_retained = id(candidate) in retained_ids

        price_only_would_remove = _would_be_removed_under_price_only(
            candidate, earlier, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )
        if price_only_would_remove:
            price_only_removed_count += 1

        if is_retained:
            if price_only_would_remove:
                margin_blocked += 1
                if not _margin_blocking_dominator_exists(
                    candidate, earlier, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
                ):
                    margin_blocked_unexplained.append(candidate)
            continue

        qualifying = [
            b
            for b in earlier
            if b.price_per_gallon <= candidate.price_per_gallon
            and not (is_estimate_priced(b) and not is_estimate_priced(candidate))
        ]

        if any(b.distance_from_start_mi == candidate.distance_from_start_mi for b in qualifying):
            co_located += 1
        elif any(b.distance_from_start_mi + tank_range_mi >= total_route_mi for b in qualifying):
            tail += 1
        else:
            unexplained.append(candidate)

    if unexplained:
        opis_ids = [c.opis_id for c in unexplained]
        raise CommandError(
            f"classify_removals could not attribute {len(unexplained)} removal(s) "
            f"(opis_id={opis_ids}) to either a co-located or a tail dominator -- "
            "the prune and an independent reading of its own stated rule disagree."
        )

    if margin_blocked_unexplained:
        opis_ids = [c.opis_id for c in margin_blocked_unexplained]
        raise CommandError(
            f"classify_removals could not individually attribute "
            f"{len(margin_blocked_unexplained)} margin-blocked retention(s) "
            f"(opis_id={opis_ids}) to a specific estimate-priced dominator that a "
            "price-only reading would have accepted as sufficient -- the "
            "margin-aware retention and an independent reading of the rule disagree."
        )

    total_removed = len(ordered) - len(retained)
    if co_located + tail != total_removed:
        raise CommandError(
            f"classify_removals tallies (co_located={co_located}, tail={tail}) do not "
            f"sum to the total number removed ({total_removed}) -- the prune and an "
            "independent reading of its own stated rule disagree."
        )

    expected_margin_blocked = price_only_removed_count - total_removed
    if margin_blocked != expected_margin_blocked:
        raise CommandError(
            f"margin_blocked_count ({margin_blocked}) does not equal the price-only "
            f"removal count ({price_only_removed_count}) minus the actual removal "
            f"count ({total_removed}) -- the closed-form equality that catches "
            "over-pruning and under-pruning alike has failed."
        )

    return co_located, tail, margin_blocked


def _station_provenance():
    """Live counts off the currently-seeded Station table -- read-only,
    never hardcoded, so this line can never drift from the table the
    figures above were actually measured against (D-34)."""
    total = Station.objects.count()
    routable = Station.objects.routable().count()
    rooftop = Station.objects.routable().filter(
        geocode_precision=GeocodePrecision.ROOFTOP
    ).count()
    city = Station.objects.routable().filter(
        geocode_precision=GeocodePrecision.CITY
    ).count()
    return total, routable, rooftop, city


class Command(BaseCommand):
    help = (
        "Measure the domination prune's candidate-set reduction across the "
        "twelve pinned real-geometry corridors, both pinned tank ranges, and "
        "both pinned price bases, with every removal attributed to a "
        "co-located or a tail dominator. Read-only: no writes, no network "
        "calls, works with no routing-provider token set. Must NOT run in "
        "CI -- the figures are evidence, not a pass/fail gate; "
        "PruneReductionGuardTests in routing/tests/test_prune_soundness.py "
        "is the CI-enforcing guard that exists instead."
    )

    def add_arguments(self, parser):
        valid_slugs = [c.slug for c in CORRIDORS]
        parser.add_argument(
            "--corridor",
            type=str,
            default=None,
            help=(
                "Optional corridor slug filter (default: all twelve). One of: "
                + ", ".join(valid_slugs)
            ),
        )
        parser.add_argument(
            "--tank-range",
            type=str,
            default=None,
            help=(
                "Optional tank range (miles) filter (default: both). One of: "
                + ", ".join(str(t) for t in TANK_RANGES_MI)
            ),
        )
        parser.add_argument(
            "--price-basis",
            type=str,
            default=None,
            choices=list(PRICE_BASES),
            help=f"Optional price basis filter (default: both). One of: {list(PRICE_BASES)}",
        )

    def handle(self, *args, **options):
        selected_corridors = self._select_corridors(options["corridor"])
        selected_tank_ranges = self._select_tank_ranges(options["tank_range"])
        selected_price_bases = [options["price_basis"]] if options["price_basis"] else list(PRICE_BASES)

        rows = []
        for corridor_def in selected_corridors:
            rows.extend(
                self._measure_corridor(corridor_def, selected_tank_ranges, selected_price_bases)
            )

        self._print_table(rows)
        self._print_provenance()
        self._print_reproduction_recipe()
        self._print_d37_observation(rows)
        self._print_d24_disclaimer()

    def _select_corridors(self, slug):
        if slug is None:
            return list(CORRIDORS)
        matches = [c for c in CORRIDORS if c.slug == slug]
        if not matches:
            valid_slugs = [c.slug for c in CORRIDORS]
            raise CommandError(
                f"Unknown corridor slug {slug!r}. Valid slugs: {valid_slugs}"
            )
        return matches

    def _select_tank_ranges(self, raw_value):
        if raw_value is None:
            return list(TANK_RANGES_MI)
        try:
            parsed = Decimal(raw_value)
        except Exception as exc:
            raise CommandError(
                f"--tank-range could not be parsed as a Decimal: {raw_value!r}"
            ) from exc
        matches = [t for t in TANK_RANGES_MI if t == parsed]
        if not matches:
            raise CommandError(
                f"Unknown tank range {raw_value!r}. Valid tank ranges: "
                f"{[str(t) for t in TANK_RANGES_MI]}"
            )
        return matches

    def _measure_corridor(self, corridor_def, tank_ranges, price_bases):
        route = load_corridor_route(corridor_def.slug)

        # Build candidates for BOTH pinned price bases, regardless of the
        # requested filter -- Guard 1 checks a structural invariant of the
        # corridor build (candidate membership is purely geometric) that
        # must hold no matter which basis a reviewer chose to narrow to.
        candidates_by_basis = {}
        raw_counts_by_basis = {}
        for basis in PRICE_BASES:
            factor_for = factor_lookup_for_basis(basis)
            candidates = corridor.candidates(route, factor_for)
            candidates_by_basis[basis] = candidates
            raw_counts_by_basis[basis] = len(candidates)

        # Guard 1.
        distinct_counts = set(raw_counts_by_basis.values())
        if len(distinct_counts) != 1:
            raise CommandError(
                f"{corridor_def.slug}: raw candidate count differs across price "
                f"bases -- {raw_counts_by_basis}. Candidate membership is "
                "purely geometric and must not depend on price basis."
            )
        raw_count = distinct_counts.pop()

        rows = []
        for basis in price_bases:
            candidates = candidates_by_basis[basis]
            ordered = sorted(candidates, key=_TOTAL_ORDER_KEY)
            for tank_range_mi in tank_ranges:
                retained = prune_dominated_candidates(
                    candidates,
                    tank_range_mi=tank_range_mi,
                    total_route_mi=route.total_route_mi,
                )
                co_located, tail, margin_blocked = classify_removals(
                    ordered,
                    retained,
                    tank_range_mi=tank_range_mi,
                    total_route_mi=route.total_route_mi,
                )
                rows.append(
                    CorridorReductionRow(
                        slug=corridor_def.slug,
                        label=corridor_def.label,
                        total_route_mi=route.total_route_mi,
                        price_basis=basis,
                        tank_range_mi=tank_range_mi,
                        raw_count=raw_count,
                        retained_count=len(retained),
                        co_located_removals=co_located,
                        tail_removals=tail,
                        margin_blocked_removals=margin_blocked,
                    )
                )
        return rows

    def _print_table(self, rows):
        by_slug = {}
        for row in rows:
            by_slug.setdefault(row.slug, []).append(row)

        for corridor_def in CORRIDORS:
            corridor_rows = by_slug.get(corridor_def.slug)
            if not corridor_rows:
                continue
            first = corridor_rows[0]
            self.stdout.write(
                self.style.SUCCESS(
                    f"{first.label} ({first.slug}): "
                    f"total_route_mi={first.total_route_mi} raw={first.raw_count}"
                )
            )
            for row in corridor_rows:
                self.stdout.write(
                    f"    tank={row.tank_range_mi}mi price={row.price_basis}: "
                    f"retained={row.retained_count} "
                    f"co_located={row.co_located_removals} tail={row.tail_removals} "
                    f"margin_blocked={row.margin_blocked_removals}"
                )

    def _print_provenance(self):
        total, routable, rooftop, city = _station_provenance()
        self.stdout.write("")
        self.stdout.write(
            f"Station table: {total} total row(s), {routable} routable "
            f"(geocoded) -- {rooftop} rooftop, {city} city centroid."
        )
        self.stdout.write(
            "The gap between the source CSV's row count and this seeded row "
            "count is explained by data/dedupe-report.md and "
            "data/geocode-report.md, both committed: the source CSV's raw "
            "rows collapse to distinct OPIS Truckstop IDs (duplicate-ID "
            "groups merged into one Station each), and only a subset of "
            "those distinct stations resolve to usable coordinates -- some "
            "rooftop, some city-centroid, some failing to geocode at all, "
            "and some falling outside the routable lower-48 scope."
        )

    def _print_reproduction_recipe(self):
        self.stdout.write("")
        self.stdout.write(
            "Reproduction: seed the station table with "
            "`manage.py seed_stations` (idempotent replay of the committed "
            "derived CSV, no network call), then run "
            "`manage.py measure_prune_reduction`. No routing-provider token "
            "is required -- the twelve corridor geometries are committed "
            "fixtures, replayed offline."
        )

    def _print_d37_observation(self, rows):
        if not rows:
            return
        by_slug = {}
        for row in rows:
            by_slug.setdefault(row.slug, row)

        longest_slug = max(by_slug, key=lambda slug: by_slug[slug].total_route_mi)
        longest_row = by_slug[longest_slug]

        self.stdout.write("")
        if "dallas_tx-seattle_wa" in by_slug:
            dallas_row = by_slug["dallas_tx-seattle_wa"]
            self.stdout.write(
                f"Observation (not investigated, D-37): the longest measured "
                f"corridor in this run is {longest_row.label} "
                f"({longest_row.slug}, {longest_row.total_route_mi} mi, raw "
                f"candidate count {longest_row.raw_count}). Dallas, TX -> "
                f"Seattle, WA -- the corridor the historical 508 figure was "
                f"attributed to -- measures a raw candidate count of "
                f"{dallas_row.raw_count} here. The corridor with the largest "
                "raw candidate set is not the one 508 was attributed to. No "
                "claim is made about why."
            )
        else:
            self.stdout.write(
                f"Observation (D-37, partial -- Dallas-Seattle not in this "
                f"run's corridor selection): the longest measured corridor in "
                f"this run is {longest_row.label} ({longest_row.slug}, "
                f"{longest_row.total_route_mi} mi, raw candidate count "
                f"{longest_row.raw_count})."
            )

    def _print_d24_disclaimer(self):
        self.stdout.write("")
        self.stdout.write(
            "This report measures candidate-set reduction only and makes NO "
            "latency claim. Phase 18 criterion 5's measured solver latency "
            "is where that is proven."
        )

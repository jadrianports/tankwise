"""Pure per-leg breakdown builder: turns a fueling plan plus Mapbox
per-segment annotations into a `legs[]` array with real driving durations.

Request-path math only -- no Django, no DB, no HTTP client. All money,
gallon, and distance values are exact, unrounded `Decimal`; shapely/JSON
floats reaching this module are already coerced to `Decimal` upstream (see
`routing.services.mapbox`), and any float this module touches directly is
coerced the same way, via `Decimal(str(value))`, never `Decimal(float)`.

N stops always produce N+1 legs (START to Stop 1, ..., Stop N to FINISH).
Durations are read from Mapbox's per-segment `annotation_durations`, never
fabricated by splitting `route.duration_s` pro-rata across distance --
pro-rata assumes uniform speed and compounds error leg over leg on any
route mixing interstate and city driving. A `None` duration (when the
route carries empty annotation arrays) is an honest absence, never a
confident fabrication.
"""
import bisect
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Leg:
    """One leg of the trip: the drive between two consecutive nodes
    (START, a fuel stop, or FINISH)."""

    from_name: str
    to_name: str
    distance_mi: Decimal
    duration_s: Decimal | None
    gallons: Decimal
    cost: Decimal


def _cumulative_axes(route):
    """Build parallel cumulative-miles and cumulative-seconds arrays from
    ``route``'s per-segment annotation arrays.

    Each returned array has one more entry than the corresponding
    annotation array, starting at zero. Returns two empty lists when either
    annotation array is empty.
    """
    distances = route.annotation_distances
    durations = route.annotation_durations

    if not distances or not durations:
        return [], []

    cumulative_miles = [Decimal(0)]
    for d in distances:
        cumulative_miles.append(cumulative_miles[-1] + d)

    cumulative_seconds = [Decimal(0)]
    for t in durations:
        cumulative_seconds.append(cumulative_seconds[-1] + t)

    return cumulative_miles, cumulative_seconds


def _duration_at_mile(mile, cumulative_miles, cumulative_seconds, total_route_mi):
    """Return the interpolated cumulative seconds elapsed at ``mile`` miles
    along the route, or ``None`` when the annotation axes are empty.

    Mapbox's summed annotation distances and its reported route ``distance``
    are computed independently and differ slightly, so ``mile`` is first
    normalized onto the annotation distance axis by the ratio of the axis
    total to ``total_route_mi`` -- this is what keeps the last leg from
    absorbing that drift.
    """
    if not cumulative_miles or not cumulative_seconds:
        return None

    axis_total = cumulative_miles[-1]
    if axis_total == 0 or total_route_mi == 0:
        return Decimal(0)

    normalized_mile = mile * (axis_total / total_route_mi)

    if normalized_mile <= 0:
        return cumulative_seconds[0]
    if normalized_mile >= axis_total:
        return cumulative_seconds[-1]

    idx = bisect.bisect_right(cumulative_miles, normalized_mile) - 1
    idx = max(0, min(idx, len(cumulative_miles) - 2))

    seg_start_mi = cumulative_miles[idx]
    seg_end_mi = cumulative_miles[idx + 1]
    seg_start_s = cumulative_seconds[idx]
    seg_end_s = cumulative_seconds[idx + 1]

    seg_len_mi = seg_end_mi - seg_start_mi
    if seg_len_mi == 0:
        return seg_start_s

    fraction = (normalized_mile - seg_start_mi) / seg_len_mi
    return seg_start_s + fraction * (seg_end_s - seg_start_s)


def build_legs(route, plan) -> list:
    """Build the per-leg breakdown for ``plan`` against ``route``.

    Produces exactly ``len(plan.stops) + 1`` legs. Each purchase is
    attributed to the leg DEPARTING the node where it was made: leg 0
    departs START (always zero gallons/cost, since START is
    non-purchasable), and leg k for k >= 1 departs ``plan.stops[k - 1]``
    and carries that stop's gallons and cost. This attribution is what
    makes leg costs sum to ``plan.total_cost`` and leg gallons sum to
    ``plan.total_gallons`` exactly, by construction.
    """
    cumulative_miles, cumulative_seconds = _cumulative_axes(route)

    node_names = ["START"] + [s.name for s in plan.stops] + ["FINISH"]
    node_miles = (
        [Decimal(0)]
        + [s.distance_from_start_mi for s in plan.stops]
        + [route.total_route_mi]
    )
    node_gallons = [Decimal(0)] + [s.gallons for s in plan.stops]
    node_cost = [Decimal(0)] + [s.cost for s in plan.stops]

    legs = []
    for i in range(len(node_names) - 1):
        from_mi = node_miles[i]
        to_mi = node_miles[i + 1]

        duration_from = _duration_at_mile(
            from_mi, cumulative_miles, cumulative_seconds, route.total_route_mi
        )
        duration_to = _duration_at_mile(
            to_mi, cumulative_miles, cumulative_seconds, route.total_route_mi
        )
        duration_s = (
            duration_to - duration_from
            if duration_from is not None and duration_to is not None
            else None
        )

        legs.append(
            Leg(
                from_name=node_names[i],
                to_name=node_names[i + 1],
                distance_mi=to_mi - from_mi,
                duration_s=duration_s,
                gallons=node_gallons[i],
                cost=node_cost[i],
            )
        )

    return legs

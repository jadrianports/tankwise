"""Pure fuel-stop solver: cheapest feasible fueling plan.

Request-path math only -- no Django, no DB, no HTTP client. All
money and gallon values are exact, unrounded `Decimal`; rounding to cents
happens only at the HTTP response serialization boundary.
"""
from dataclasses import dataclass
from decimal import Decimal

from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError


class PurchaseReason:
    """String-enum constants for `FuelStop.purchase_reason`.

    Each value names the exact solver branch that produced the stop --
    recorded at the moment the branch fires, never re-derived afterward by
    inspecting the finished plan. Wire values are the lowercase strings.
    """

    REACH_CHEAPER_STOP = "reach_cheaper_stop"
    FILL_TO_CONTINUE = "fill_to_continue"
    REACH_FINISH = "reach_finish"
    TOP_UP_AT_CHEAPEST = "top_up_at_cheapest"


@dataclass(frozen=True)
class Candidate:
    """A candidate fuel stop positioned along the route."""

    name: str
    opis_id: int
    price_per_gallon: Decimal
    distance_from_start_mi: Decimal


@dataclass(frozen=True)
class FuelStop:
    """A purchase recorded at a real, along-route station.

    The rationale fields (`purchase_reason` onward) are additive and
    default to `None`/`0` so callers constructing a `FuelStop` with only
    the original six fields keep working unchanged.
    """

    name: str
    opis_id: int
    price_per_gallon: Decimal
    distance_from_start_mi: Decimal
    gallons: Decimal
    cost: Decimal
    purchase_reason: str | None = None
    reason_target_opis_id: int | None = None
    reason_target_name: str | None = None
    skipped_count: int = 0
    skipped_avg_price: Decimal | None = None
    price_percentile: Decimal | None = None
    corridor_avg_price: Decimal | None = None


@dataclass(frozen=True)
class FuelPlan:
    """The cheapest feasible fueling plan for a route."""

    stops: list[FuelStop]
    total_cost: Decimal
    total_gallons: Decimal


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validate(candidates, total_route_mi, tank_range_mi, mpg, starting_fuel):
    if total_route_mi <= 0:
        raise InvalidRouteInputError(
            f"total_route_mi must be positive, got {total_route_mi}"
        )
    if tank_range_mi <= 0:
        raise InvalidRouteInputError(
            f"tank_range_mi must be positive, got {tank_range_mi}"
        )
    if mpg <= 0:
        raise InvalidRouteInputError(f"mpg must be positive, got {mpg}")
    if starting_fuel < 0 or starting_fuel > 1:
        raise InvalidRouteInputError(
            f"starting_fuel must be within [0, 1], got {starting_fuel}"
        )
    for c in candidates:
        if c.price_per_gallon < 0:
            raise InvalidRouteInputError(
                f"price_per_gallon must not be negative, got {c.price_per_gallon} "
                f"for candidate {c.name!r}"
            )
        if c.distance_from_start_mi < 0 or c.distance_from_start_mi > total_route_mi:
            raise InvalidRouteInputError(
                f"distance_from_start_mi ({c.distance_from_start_mi}) for candidate "
                f"{c.name!r} must be within [0, total_route_mi={total_route_mi}]"
            )


def solve(
    candidates,
    total_route_mi,
    *,
    tank_range_mi=Decimal(500),
    mpg=Decimal(10),
    starting_fuel=Decimal(1),
) -> FuelPlan:
    """Return the cheapest feasible fueling plan for a route of
    ``total_route_mi`` miles, given an iterable of ``Candidate`` stations.

    ``starting_fuel`` is a 0.0-1.0 fraction of ``tank_range_mi`` already in
    the tank at the origin (default ``1`` -- a full tank). START is a
    non-purchasable node: it can never be billed for fuel, so the
    reachable set at START is bounded by the fuel actually on board
    (``starting_fuel * tank_range_mi``), not by the tank's full capacity.
    At every real station the tank can be topped off, so the bound there
    stays ``tank_range_mi``.

    Raises ``InvalidRouteInputError`` on malformed input (including a
    ``starting_fuel`` outside ``[0, 1]``) and ``InfeasibleRouteError`` when
    no feasible plan exists (a gap between two along-route nodes exceeds
    the usable range at that node).
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    mpg = _as_decimal(mpg)
    starting_fuel = _as_decimal(starting_fuel)

    candidates = list(candidates)

    _validate(candidates, total_route_mi, tank_range_mi, mpg, starting_fuel)

    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )

    total_candidates = len(candidates)
    corridor_avg_price = (
        sum((c.price_per_gallon for c in candidates), Decimal(0)) / total_candidates
        if total_candidates
        else None
    )

    def _price_percentile(price):
        if not total_candidates:
            return None
        cheaper_count = sum(1 for c in candidates if c.price_per_gallon < price)
        return Decimal(cheaper_count) / Decimal(total_candidates)

    def _skipped_context(prev_stop_mi, pos):
        skipped = [
            c for c in candidates if prev_stop_mi < c.distance_from_start_mi < pos
        ]
        skipped_count = len(skipped)
        skipped_avg_price = (
            sum((c.price_per_gallon for c in skipped), Decimal(0)) / skipped_count
            if skipped_count
            else None
        )
        return skipped_count, skipped_avg_price

    pos = Decimal(0)
    fuel = starting_fuel * tank_range_mi
    price_here = Decimal(0)
    current_name = "START"
    current_opis_id = None
    stops = []

    while True:
        usable_range = tank_range_mi if current_opis_id is not None else fuel
        reachable = [
            c for c in ordered if pos < c.distance_from_start_mi <= pos + usable_range
        ]
        cheaper = [c for c in reachable if c.price_per_gallon < price_here]

        if cheaper:
            # (a) nearest strictly-cheaper reachable station.
            target = min(
                cheaper,
                key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
            )
            gap = target.distance_from_start_mi - pos
            buy_mi = max(Decimal(0), gap - fuel)
            if buy_mi > 0:
                gallons = buy_mi / mpg
                prev_stop_mi = stops[-1].distance_from_start_mi if stops else Decimal(0)
                skipped_count, skipped_avg_price = _skipped_context(prev_stop_mi, pos)
                stops.append(
                    FuelStop(
                        name=current_name,
                        opis_id=current_opis_id,
                        price_per_gallon=price_here,
                        distance_from_start_mi=pos,
                        gallons=gallons,
                        cost=gallons * price_here,
                        purchase_reason=PurchaseReason.REACH_CHEAPER_STOP,
                        reason_target_opis_id=target.opis_id,
                        reason_target_name=target.name,
                        skipped_count=skipped_count,
                        skipped_avg_price=skipped_avg_price,
                        price_percentile=_price_percentile(price_here),
                        corridor_avg_price=corridor_avg_price,
                    )
                )
            fuel = fuel + buy_mi - gap
            pos = target.distance_from_start_mi
            price_here = target.price_per_gallon
            current_name = target.name
            current_opis_id = target.opis_id
            continue

        if (total_route_mi - pos) <= usable_range:
            # (b) finish reachable, no cheaper station first -- buy just
            # enough to finish, never fill (endpoint rule).
            gap = total_route_mi - pos
            buy_mi = max(Decimal(0), gap - fuel)
            if buy_mi > 0:
                gallons = buy_mi / mpg
                prev_stop_mi = stops[-1].distance_from_start_mi if stops else Decimal(0)
                skipped_count, skipped_avg_price = _skipped_context(prev_stop_mi, pos)
                stops.append(
                    FuelStop(
                        name=current_name,
                        opis_id=current_opis_id,
                        price_per_gallon=price_here,
                        distance_from_start_mi=pos,
                        gallons=gallons,
                        cost=gallons * price_here,
                        purchase_reason=PurchaseReason.REACH_FINISH,
                        reason_target_opis_id=None,
                        reason_target_name=None,
                        skipped_count=skipped_count,
                        skipped_avg_price=skipped_avg_price,
                        price_percentile=_price_percentile(price_here),
                        corridor_avg_price=corridor_avg_price,
                    )
                )
            break

        # (c) no cheaper station in range and finish out of range.
        if not reachable:
            remaining_nodes = [
                (c.distance_from_start_mi, c.name) for c in ordered if c.distance_from_start_mi > pos
            ]
            remaining_nodes.append((total_route_mi, "FINISH"))
            next_dist, next_name = min(remaining_nodes, key=lambda n: n[0])
            raise InfeasibleRouteError(
                from_station=current_name,
                to_station=next_name,
                gap_mi=next_dist - pos,
                max_range_mi=usable_range,
            )

        # Fill the tank (only possible at a real, purchasable station --
        # START can never be billed), then hop to the cheapest reachable
        # station (ties broken by nearest) -- never the farthest.
        target = min(
            reachable,
            key=lambda c: (c.price_per_gallon, c.distance_from_start_mi, c.opis_id),
        )
        if current_opis_id is not None:
            buy_mi = tank_range_mi - fuel
            if buy_mi > 0:
                gallons = buy_mi / mpg
                ahead = [c for c in candidates if c.distance_from_start_mi > pos]
                if not ahead or price_here <= min(c.price_per_gallon for c in ahead):
                    reason = PurchaseReason.TOP_UP_AT_CHEAPEST
                else:
                    reason = PurchaseReason.FILL_TO_CONTINUE
                prev_stop_mi = stops[-1].distance_from_start_mi if stops else Decimal(0)
                skipped_count, skipped_avg_price = _skipped_context(prev_stop_mi, pos)
                stops.append(
                    FuelStop(
                        name=current_name,
                        opis_id=current_opis_id,
                        price_per_gallon=price_here,
                        distance_from_start_mi=pos,
                        gallons=gallons,
                        cost=gallons * price_here,
                        purchase_reason=reason,
                        reason_target_opis_id=target.opis_id,
                        reason_target_name=target.name,
                        skipped_count=skipped_count,
                        skipped_avg_price=skipped_avg_price,
                        price_percentile=_price_percentile(price_here),
                        corridor_avg_price=corridor_avg_price,
                    )
                )
            fuel = tank_range_mi
        fuel -= target.distance_from_start_mi - pos
        pos = target.distance_from_start_mi
        price_here = target.price_per_gallon
        current_name = target.name
        current_opis_id = target.opis_id

    return FuelPlan(
        stops=stops,
        total_cost=sum((s.cost for s in stops), Decimal(0)),
        total_gallons=sum((s.gallons for s in stops), Decimal(0)),
    )

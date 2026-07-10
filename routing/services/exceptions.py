"""Exception hierarchy for the fuel-stop solver (FUEL-04, FUEL-10, D-10).

Standard-library only -- no Django, no DB, no HTTP (FUEL-05).

`InfeasibleRouteError` is a legitimate "no plan exists" outcome (the gap
between two along-route nodes exceeds the vehicle's max range). It carries
structured detail so Phase 4 can map it to a specific 4xx response.

`InvalidRouteInputError` guards the solver's own contract against malformed
caller input (non-positive route length, negative price, an out-of-route
position, etc.) -- a defensive backstop, since the untrusted HTTP boundary
is validated separately by DRF in Phase 4.
"""


class SolverError(Exception):
    """Base class for all fuel-stop solver errors."""


class InfeasibleRouteError(SolverError):
    """No feasible fueling plan exists: the gap between two along-route
    nodes exceeds the vehicle's max range.

    Attributes:
        from_station: name of the station the trip is stuck at, or the
            "START" sentinel when the gap begins at the origin.
        to_station: name of the nearest unreached node ahead, or the
            "FINISH" sentinel when the gap ends at the destination.
        gap_mi: Decimal distance between from_station and to_station.
        max_range_mi: Decimal max range the vehicle can travel on a full
            tank.
    """

    def __init__(self, *, from_station, to_station, gap_mi, max_range_mi):
        self.from_station = from_station
        self.to_station = to_station
        self.gap_mi = gap_mi
        self.max_range_mi = max_range_mi
        super().__init__(
            f"No feasible fuel plan: gap of {gap_mi} mi between "
            f"{from_station!r} and {to_station!r} exceeds max range of "
            f"{max_range_mi} mi"
        )


class InvalidRouteInputError(SolverError):
    """Malformed caller input reached the solver (e.g. a non-positive
    route length, a negative price, or a candidate positioned outside the
    route)."""

    def __init__(self, message):
        super().__init__(message)

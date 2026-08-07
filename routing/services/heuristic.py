"""Penalty-aware heuristic fuel-stop solver: an approximate fallback for
routes whose fixed-charge DP (`routing.services.dp.solve_fixed_charge`)
would exceed the pre-flight latency budget
(`routing.services.dp.DP_TRANSITION_BUDGET`,
`routing.services.dp.estimate_transition_count`).

This is NOT `routing/services/greedy.py`. The pre-Phase-18 greedy that
module restores is structurally blind to the flat per-stop `penalty` --
it always detours to the NEAREST strictly-cheaper reachable station
whenever one exists, regardless of how little fuel that detour actually
buys. That is exactly the micro-stop behaviour v3.1 exists to eliminate:
a chain of successively-slightly-cheaper stations each earns its own
stop even when the saving from stopping there is a fraction of the fixed
charge. `greedy.py` stays untouched -- it remains the production twin
`routing/tests/test_greedy.py`'s differential referee depends on -- and
is never imported or modified here. This module is a SEPARATE fallback,
wired in at the dispatch seam (`routing/services/solver.py`) specifically
because it approximates the fixed-charge objective
`dp.solve_fixed_charge` minimises exactly, instead of ignoring it.

## The idea

At every real (purchasable) station where a strictly-cheaper reachable
station exists, this heuristic asks the same question `dp.py`'s own
`BYPASS_CHEAPER_NOT_WORTH_STOP` reason answers structurally: if I fill
the tank here and drive straight to the farthest node a full tank
reaches, how many strictly-cheaper stations would I fly past, and how
much fuel-dollar saving would that cost me? If the flat `penalty` --
plus, since phase 21 (PROV-03), this station's own flat trust-margin
charge when it is `eia_regional_estimate`-priced -- outweighs that
saving, filling up and bypassing them is cheaper than stopping at each --
so this heuristic fills the tank and skips ahead. If the saving outweighs
the penalty (plus margin), the detour is worth it, so the heuristic buys
just enough fuel here to reach the single CHEAPEST reachable station (not
merely the nearest cheaper one -- see "Design choice" below) and asks the
same question again there.

**D-04's honest partial, recorded here in plain words:** the
`penalty > saving_total` comparison above is the ONLY place this arm's
trust margin changes behaviour (`_margin_for`, PROV-03). The
cheapest-in-window `min()`, `_farthest`'s tiebreak, and the direct
`c.price_per_gallon < price_here` test all stay margin-blind -- they are
raw per-gallon quantities with no quantity attached, so a flat margin has
no defined meaning there and none is invented (D-05). Consequently this
heuristic will still HOP TOWARD a cheap `eia_regional_estimate`-priced
station without paying its margin whenever the bypass test itself is
never reached (no strictly-cheaper station in the window at all) -- a
documented limitation of this proof-free arm, not a defect being hidden.
The exact DP (`dp.py`) carries no such gap: its margin applies to every
purchase transition, not just a bypass decision.

## What this heuristic does NOT guarantee

- **Not fixed-charge-optimal.** It is a single forward pass with no
  backtracking and no lookahead beyond one tank's own reach -- it can
  produce a plan strictly more expensive (in
  `total_cost + penalty * len(stops)`) than `dp.solve_fixed_charge`'s
  exact answer. `18-04d-SUMMARY.md` quantifies this gap, measured
  against the exact DP on every corridor cell where the DP itself is
  tractable, so the size of the approximation is a measured number, not
  a guess.
- **Not guaranteed to minimise stop count** -- only to avoid a stop the
  bypass test itself judges not worth its penalty. A different sequence
  of stops could, in principle, cost less under the same objective.
- **Never claims optimality** anywhere in its own output, docstrings, or
  the API's `solver_strategy` field (`"penalty_aware_heuristic"`, never
  `"exact_dp"`).

## Design choice: cheapest-in-window, not nearest-cheaper

The pre-Phase-18 greedy this module improves on always detours to the
NEAREST strictly-cheaper reachable station (`greedy.solve_greedy`'s own
`cheaper` branch) -- exactly how a chain of trivially-small detours
accumulates: each station a little cheaper than the last earns its own
stop, even when the saving from stopping there is a fraction of the
fixed charge. This heuristic detours to the CHEAPEST reachable station
in the window instead (ties broken by nearest position, then `opis_id`
-- the same total order every other solver module in this codebase
uses): when a detour is judged worth taking at all, it should buy as
much useful range as the best available price offers, rather than
splitting that purchase across several successively-cheaper stops.

## Feasibility

Every branch below buys either (a) exactly enough fuel to reach a node
already confirmed reachable within the current usable range, or (b) a
full tank, whose reach is by construction bounded by `tank_range_mi` and
therefore always covers the farthest node this function ever targets
with it. No branch can ever leave the vehicle short of the fuel its own
chosen target requires -- see `routing/tests/test_heuristic.py`'s
feasibility property test for the executable proof (every intermediate
`fuel` value stays within `[0, tank_range_mi]`, and the tank is never
asked to travel farther than it holds).

Request-path math only -- no Django, no DB, no HTTP client, exactly as
`solver.py`/`dp.py`/`greedy.py`/`prune.py`.
"""
import bisect
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from routing.services import solver
from routing.services.exceptions import InfeasibleRouteError
from routing.services.solver import FuelPlan, FuelStop, PurchaseReason


def solve_penalty_aware_heuristic(
    candidates, total_route_mi, *, tank_range_mi, mpg, starting_fuel,
    penalty=Decimal(0), trust_margin=Decimal(0),
) -> FuelPlan:
    """Return a feasible, `penalty`-aware (but not fixed-charge-optimal)
    fueling plan for a route of `total_route_mi` miles, given an iterable
    of `Candidate` stations. See the module docstring for the algorithm,
    what it does and does not guarantee, and why it differs from
    `greedy.solve_greedy`.

    `penalty` genuinely changes which stations this algorithm buys at
    (unlike `greedy.solve_greedy`, which only uses `penalty` to compute
    the reported `penalised_objective`) -- at `penalty=0` every bypass
    test's `penalty > saving_total` comparison can never hold (`saving_total`
    is never negative), so this heuristic collapses to always detouring to
    the cheapest reachable station, the same shape `greedy.solve_greedy`
    takes at `penalty=0` (though not always byte-identical to it, since
    this heuristic targets the CHEAPEST reachable station rather than the
    NEAREST cheaper one -- see the module docstring's "Design choice").

    `trust_margin` (PROV-03, D-04/D-16) mirrors `penalty`'s own default
    shape (`Decimal(0)`, inert) and joins it on the bypass test's left-hand
    side ONLY -- see the module docstring's "D-04's honest partial" for
    which comparisons stay margin-blind and why.

    Assumes the caller has already run `dp.preflight_gap_check` (or
    equivalent) over `candidates`/`total_route_mi`/`tank_range_mi`/
    `starting_fuel` -- `solver.solve()`, this function's only production
    caller, always does. This function still raises `InfeasibleRouteError`
    itself if a genuine gap is encountered regardless (defensive, matching
    `greedy.solve_greedy`'s own behaviour), rather than assuming that
    precondition silently.

    `candidates`/`total_route_mi`/`tank_range_mi`/`mpg`/`starting_fuel`/
    `penalty` are assumed already-validated, already-`Decimal` values --
    exactly the same assumption `dp.solve_fixed_charge` and
    `greedy.solve_greedy` make about their own inputs. Validation is
    `solver.solve()`'s sole responsibility.

    Only `name`, `opis_id`, `price_per_gallon`, `distance_from_start_mi`,
    `gallons`, `cost`, `purchase_reason`, `reason_target_opis_id`,
    `reason_target_name`, `bypassed_cheaper_count`, and
    `bypassed_saving_forgone` are populated on each returned `FuelStop` --
    `skipped_count`, `skipped_avg_price`, `price_percentile`, and
    `corridor_avg_price` are left at their dataclass defaults, exactly as
    `dp.solve_fixed_charge` and `greedy.solve_greedy` leave them, because
    `solver.solve()` rebuilds those over the FULL, unpruned candidate list
    itself (D-20) regardless of which producer built the raw plan.
    """
    candidates = list(candidates)
    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )
    positions = [c.distance_from_start_mi for c in ordered]
    node_count = len(ordered)

    def _window(from_pos, reach):
        """Ordered candidates strictly ahead of `from_pos`, within
        `reach` miles of it -- an O(log n) slice via `bisect` over the
        already-sorted `positions`, the same `bisect_right`-over-a-sorted-
        array idiom `dp.py`'s own `_reachable_ticks` uses (no tick
        conversion is needed here the way `dp.py`'s exact-integer money
        comparison needs one -- `Decimal` is already totally ordered, and
        this module never compares monetary sums, only makes routing
        decisions from already-materialized per-purchase dollar amounts)."""
        lo = bisect.bisect_right(positions, from_pos)
        hi = bisect.bisect_right(positions, from_pos + reach, lo, node_count)
        return ordered[lo:hi]

    def _farthest(items):
        """The farthest-by-position item in `items` (non-empty), ties
        broken by cheapest price then `opis_id` -- the same total order
        convention every other tie-break in this module and `dp.py` uses.
        A full-tank fill should always travel as far as it can (maximizing
        distance covered per fixed charge paid) -- targeting merely the
        CHEAPEST reachable station instead (as the pre-Phase-18 greedy's
        own fill branch does) can strand the vehicle with a mostly-full
        tank at a nearby station, forcing another top-off stop only a few
        miles later. That is a second, independent source of the
        micro-stop problem this module exists to eliminate, distinct from
        (but just as real as) the nearest-vs-cheapest chain the module
        docstring's "Design choice" section already covers."""
        return min(items, key=lambda c: (-c.distance_from_start_mi, c.price_per_gallon, c.opis_id))

    def _margin_for(price_source):
        """The flat PROV-03 trust-margin charge for a purchase whose
        station carries `price_source`, read through the phase's ONE
        shared provenance lookup on `solver` (see that module's own
        function of this name) rather than a direct `price_source`
        comparison in this module -- see `PriceSourceUsagePurityTest`.
        `SimpleNamespace` stands in for a full `Candidate` here because
        this walk only ever tracks a station's own `price_source` string
        locally (`current_price_source`), never the original `Candidate`
        object it came from."""
        return solver.trust_margin_for(
            SimpleNamespace(price_source=price_source), trust_margin
        )

    def _raise_infeasible(from_name, from_pos, max_range):
        remaining_nodes = [
            (c.distance_from_start_mi, c.name)
            for c in ordered
            if c.distance_from_start_mi > from_pos
        ]
        remaining_nodes.append((total_route_mi, "FINISH"))
        next_dist, next_name = min(remaining_nodes, key=lambda n: n[0])
        raise InfeasibleRouteError(
            from_station=from_name,
            to_station=next_name,
            gap_mi=next_dist - from_pos,
            max_range_mi=max_range,
        )

    def _make_stop(
        name, opis_id, price, position, buy_mi, reason, target, *,
        bypassed_count=0, bypassed_saving=None, price_source,
    ):
        # `price_source` is keyword-only with NO default, deliberately --
        # every call site must state it explicitly, so a forgotten site is
        # a TypeError at import-test time rather than a silent None. The
        # correct argument at every call site is always the WALK's own
        # tracking local for the station this purchase is made at (where
        # the walk currently stands), never the upcoming target's own
        # provenance (where the walk is going next) -- getting that
        # backwards would produce a plausible-looking but wrong plan.
        gallons = buy_mi / mpg
        return FuelStop(
            name=name,
            opis_id=opis_id,
            price_per_gallon=price,
            distance_from_start_mi=position,
            gallons=gallons,
            cost=gallons * price,
            purchase_reason=reason,
            reason_target_opis_id=target.opis_id if target is not None else None,
            reason_target_name=target.name if target is not None else None,
            bypassed_cheaper_count=bypassed_count,
            bypassed_saving_forgone=bypassed_saving,
            price_source=price_source,
        )

    pos = Decimal(0)
    fuel = starting_fuel * tank_range_mi
    price_here = Decimal(0)
    current_name = "START"
    current_opis_id = None
    current_price_source = None
    stops = []

    while True:
        usable_range = tank_range_mi if current_opis_id is not None else fuel
        reachable = _window(pos, usable_range)
        finish_reachable = (total_route_mi - pos) <= usable_range
        cheaper = [c for c in reachable if c.price_per_gallon < price_here]

        if current_opis_id is None:
            # START -- non-purchasable, bounded strictly by the fuel
            # already on board: every reachable candidate here costs
            # nothing extra to reach (buy_mi is structurally <= 0, since
            # `reachable` is itself bounded by that same fuel), so there
            # is no fixed-charge tradeoff to weigh yet. Mirrors
            # `greedy.solve_greedy`'s own START handling exactly.
            if cheaper:
                target = min(
                    cheaper,
                    key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
                )
            elif finish_reachable:
                break
            elif reachable:
                target = min(
                    reachable,
                    key=lambda c: (c.price_per_gallon, c.distance_from_start_mi, c.opis_id),
                )
            else:
                _raise_infeasible(current_name, pos, usable_range)
            fuel = fuel - (target.distance_from_start_mi - pos)
            pos = target.distance_from_start_mi
            price_here = target.price_per_gallon
            current_name = target.name
            current_opis_id = target.opis_id
            current_price_source = target.price_source
            continue

        # Real, purchasable station from here on.
        if not cheaper:
            # No cheaper option anywhere within one tank -- nothing to
            # weigh against the penalty. Fill (or finish directly) and
            # hop to the cheapest reachable station, the same shape
            # `greedy.solve_greedy`'s own fill/finish branches take.
            if finish_reachable:
                gap = total_route_mi - pos
                buy_mi = max(Decimal(0), gap - fuel)
                if buy_mi > 0:
                    stops.append(
                        _make_stop(
                            current_name, current_opis_id, price_here, pos,
                            buy_mi, PurchaseReason.REACH_FINISH, None,
                            price_source=current_price_source,
                        )
                    )
                break
            if not reachable:
                _raise_infeasible(current_name, pos, usable_range)
            # Nothing cheaper anywhere in the window -- no price benefit
            # to weigh, so travel as far as this full tank allows rather
            # than hopping to the nearest/cheapest-among-non-cheaper
            # station (see `_farthest`'s own docstring).
            target = _farthest(reachable)
            buy_mi = tank_range_mi - fuel
            if buy_mi > 0:
                ahead = [c for c in candidates if c.distance_from_start_mi > pos]
                reason = (
                    PurchaseReason.TOP_UP_AT_CHEAPEST
                    if not ahead or price_here <= min(c.price_per_gallon for c in ahead)
                    else PurchaseReason.FILL_TO_CONTINUE
                )
                stops.append(
                    _make_stop(
                        current_name, current_opis_id, price_here, pos, buy_mi,
                        reason, target, price_source=current_price_source,
                    )
                )
            fuel = tank_range_mi - (target.distance_from_start_mi - pos)
            pos = target.distance_from_start_mi
            price_here = target.price_per_gallon
            current_name = target.name
            current_opis_id = target.opis_id
            current_price_source = target.price_source
            continue

        # A cheaper station exists within one tank -- the penalty-aware
        # decision. `reachable` was already built with usable_range ==
        # tank_range_mi here (current_opis_id is not None), so it IS the
        # full-tank window a fill-and-hop move would use -- no separate
        # computation needed.
        target_full = None if finish_reachable else _farthest(reachable)

        if target_full is not None and target_full.price_per_gallon < price_here:
            # The farthest node a full tank reaches also happens to be
            # strictly cheaper than here -- best of both worlds, nothing
            # to weigh (mirrors `dp.py`'s own
            # `target_index in reachable_cheaper_idx` branch: reached,
            # not bypassed).
            buy_mi = tank_range_mi - fuel
            if buy_mi > 0:
                stops.append(
                    _make_stop(
                        current_name, current_opis_id, price_here, pos,
                        buy_mi, PurchaseReason.REACH_CHEAPER_STOP, target_full,
                        price_source=current_price_source,
                    )
                )
            fuel = tank_range_mi - (target_full.distance_from_start_mi - pos)
            pos = target_full.distance_from_start_mi
            price_here = target_full.price_per_gallon
            current_name = target_full.name
            current_opis_id = target_full.opis_id
            current_price_source = target_full.price_source
            continue

        target_full_pos = total_route_mi if target_full is None else target_full.distance_from_start_mi
        bypassed = [c for c in cheaper if c.distance_from_start_mi < target_full_pos]
        saving_total = Decimal(0)
        for c in bypassed:
            c_gap = c.distance_from_start_mi - pos
            c_buy_mi = max(Decimal(0), c_gap - fuel)
            saving_total += (c_buy_mi / mpg) * (price_here - c.price_per_gallon)

        # D-04: the ONE place this arm's margin joins a selection decision
        # -- the current station's own margin (PROV-03) added onto the
        # penalty side of the flat-charge-vs-fuel-saving comparison this
        # branch was already making. `saving_total`'s accumulation above
        # already converts per-gallon prices into dollars, exactly what a
        # flat charge needs to be comparable against. No other comparison
        # in this module (the cheapest-in-window `min()`, `_farthest`'s
        # tiebreak, the direct `c.price_per_gallon < price_here` test) is
        # touched -- those are raw per-gallon quantities with no quantity
        # attached, so a flat charge has no defined meaning there (D-05).
        current_margin = _margin_for(current_price_source)
        if bypassed and penalty + current_margin > saving_total:
            # The flat per-stop penalty strictly outweighs the summed
            # fuel-dollar saving the bypassed stations would have offered
            # -- filling up and skipping them is cheaper than stopping at
            # each (`dp.py`'s own BYPASS_CHEAPER_NOT_WORTH_STOP rule).
            if target_full is None:
                # REACH_FINISH always overrides (matches
                # `dp.solve_fixed_charge`'s own rule): completing the
                # trip is this edge's purpose, not routing toward a named
                # station -- buy exactly enough to finish, never a full
                # tank.
                gap = total_route_mi - pos
                buy_mi = max(Decimal(0), gap - fuel)
                if buy_mi > 0:
                    stops.append(
                        _make_stop(
                            current_name, current_opis_id, price_here, pos,
                            buy_mi, PurchaseReason.REACH_FINISH, None,
                            price_source=current_price_source,
                        )
                    )
                break
            buy_mi = tank_range_mi - fuel
            if buy_mi > 0:
                stops.append(
                    _make_stop(
                        current_name, current_opis_id, price_here, pos, buy_mi,
                        PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP, target_full,
                        bypassed_count=len(bypassed), bypassed_saving=saving_total,
                        price_source=current_price_source,
                    )
                )
            fuel = tank_range_mi - (target_full.distance_from_start_mi - pos)
            pos = target_full.distance_from_start_mi
            price_here = target_full.price_per_gallon
            current_name = target_full.name
            current_opis_id = target_full.opis_id
            current_price_source = target_full.price_source
            continue

        # Worth the extra stop -- buy just enough here to reach the
        # single cheapest reachable station (not merely the nearest
        # cheaper one -- see the module docstring's "Design choice").
        target = min(
            cheaper,
            key=lambda c: (c.price_per_gallon, c.distance_from_start_mi, c.opis_id),
        )
        gap = target.distance_from_start_mi - pos
        buy_mi = max(Decimal(0), gap - fuel)
        if buy_mi > 0:
            stops.append(
                _make_stop(
                    current_name, current_opis_id, price_here, pos, buy_mi,
                    PurchaseReason.REACH_CHEAPER_STOP, target,
                    price_source=current_price_source,
                )
            )
        fuel = fuel + buy_mi - gap
        pos = target.distance_from_start_mi
        price_here = target.price_per_gallon
        current_name = target.name
        current_opis_id = target.opis_id
        current_price_source = target.price_source

    # The walk above commits to a cheaper `target` and advances
    # `current_name`/`current_opis_id` to it before checking whether the
    # walk terminates there needing no purchase (`buy_mi <= 0`, so the
    # "Worth the extra stop" branch never appended a `FuelStop` for it).
    # When that happens, the PREVIOUS stop is left naming, as its
    # `reason_target_*`, a station that never earns its own entry in
    # `stops` -- a plan that tells the driver they are fuelling to reach
    # somewhere they will never visit. Correcting it here, once, after the
    # loop, covers every termination path uniformly without touching the
    # loop body itself.
    if stops and stops[-1].purchase_reason == PurchaseReason.REACH_CHEAPER_STOP:
        dangling_target_id = stops[-1].reason_target_opis_id
        if dangling_target_id is not None and dangling_target_id not in {
            s.opis_id for s in stops
        }:
            stops[-1] = replace(
                stops[-1],
                purchase_reason=PurchaseReason.REACH_FINISH,
                reason_target_opis_id=None,
                reason_target_name=None,
            )

    total_cost = sum((s.cost for s in stops), Decimal(0))
    total_gallons = sum((s.gallons for s in stops), Decimal(0))
    total_margin_applied = sum(
        (_margin_for(s.price_source) for s in stops), Decimal(0)
    )

    return FuelPlan(
        stops=stops,
        total_cost=total_cost,
        total_gallons=total_gallons,
        penalised_objective=total_cost + penalty * len(stops) + total_margin_applied,
        penalty_applied=penalty,
        trust_margin_applied=trust_margin,
    )

"""Pure domination prune: shrink the candidate set before a solver searches it.

Request-path math only -- no Django, no DB, no HTTP client. All distance
and price values are exact, unrounded ``Decimal``; rounding to cents
happens only at the HTTP response serialization boundary, exactly as in
``solver.py``'s own purity header.

Nothing in production calls this module yet. It lives inside the AST-gated
solver boundary (see ``SOLVER_FILES`` in ``routing/tests/test_boundaries.py``)
and is proven sound by ``routing/tests/test_prune_soundness.py``, but no
plan before Phase 18 wires it into ``solve()``. When Phase 18 does wire it
in, ``solve()`` must still receive the *complete* candidate list and
compute ``corridor_avg_price``, ``_price_percentile()``, and
``_skipped_context()`` over that complete list -- only the set the search
explores may be pruned. The prune must be invisible in the API response;
that is what "sound" means for a search-space reduction: it changes
nothing about the answer, only how much work finding the answer costs.

## The sliver

For two candidates A and B with positions ``pos_A``, ``pos_B`` (miles from
START) and a tank range ``T``, define B's "reach sliver relative to A" as
the half-open interval

    sliver(A, B) = (pos_B + T, pos_A + T]        width = pos_A - pos_B

This is exactly the set of positions a full tank taken on at A can reach
that a full tank taken on at B cannot -- A's unique downstream contribution
over B. If nothing needs that sliver, A contributes nothing B does not
already cover.

## The Domination Theorem (three conditions, D-11 total order applied)

Station A is safely removable from the candidate set -- its removal
changes neither the optimal cost nor feasibility, for the pure-fuel
objective or the fixed-charge (per-stop penalty) objective -- if there
exists a station B, ranked strictly before A under the total order
``(distance_from_start_mi, price_per_gallon, opis_id)``, with:

  1. ``pos_B <= pos_A`` (non-strict)
  2. ``price_B <= price_A``
  3. ``sliver(A, B)`` contains no other candidate station, and does not
     contain ``total_route_mi`` (FINISH)

The total order is load-bearing, not cosmetic (D-11): two candidates at the
identical position with the identical price have a zero-width sliver
between them, which is trivially empty and satisfies condition 3 for both
directions at once. Without a tiebreak, each would see the other as a valid
dominator under a naive "pos_B <= pos_A" test and *both* would be judged
removable, silently deleting the only station at that position. The
``opis_id`` tiebreak breaks that symmetry: under the total order, exactly
one of the pair sorts first, so only the later one ever looks backward and
finds a dominator -- the earlier twin is never examined as removable
relative to a later one it precedes. Exactly one of any co-located,
identically priced pair survives.

## Cost argument (condition 1 + 2)

If B is reachable everywhere A is (condition 3 guarantees this: nothing was
uniquely reachable via A) and B's price is no higher, then substituting a
purchase at B for an equal purchase at A is never worse. Any plan that
bought fuel at A can instead buy the same gallons at B and reach every node
that plan reached via A, at a cost no higher than before.

## Fixed-charge generalization

The above argument holds for pure fuel dollars; the fixed-charge objective
(fuel dollars plus a flat penalty per station actually purchased at) is
also unharmed, because folding a purchase from A into B has exactly two
possible shapes:

  (a) B already has a purchase in the optimal plan being modified -- the
      substituted gallons merge into B's existing stop, so the station
      count is unchanged and the objective strictly improves by the price
      difference alone (never worse, since ``price_B <= price_A``).
  (b) B has no purchase in that plan -- the substitution adds a new stop at
      B in place of the one it removes at A. The number of stops is
      unchanged; one purchase simply moves to an earlier position on the
      route. The fixed-charge term of the objective is identical either
      way, and the fuel-dollar term is no higher by the cost argument
      above.

Either way the fixed-charge objective is never made worse by removing A, so
the theorem holds for both objectives this milestone's solvers need, not
just the pure-fuel one the underlying paper analyzes.

## Feasibility corollary

Condition 3's emptiness is also the exact statement needed to guarantee
removing A cannot create a new "gap exceeds tank range" infeasibility: if
the sliver holds no other candidate and does not hold FINISH, then nothing
was reachable *only* via A's specific position -- B (or whatever candidate
or FINISH sits beyond the sliver) already bridges the same gap. A sound
domination prune, by this theorem, can therefore never be the cause of a
false ``infeasible_route``; that guarantee falls directly out of condition
3, not a separate check.

## Completeness of checking exactly one dominator (D-09)

Every candidate is tested against exactly one dominator -- the nearest
qualifying B (largest ``pos_B <= pos_A`` with ``price_B <= price_A``) --
never every possible B. This is complete, not merely convenient: for any
two candidates B1, B2 both satisfying conditions 1 and 2 for A, with B1
nearer to A than B2 (``pos_B2 <= pos_B1 <= pos_A``),
``sliver(A, B1) = (pos_B1 + T, pos_A + T]`` is a *subset* of
``sliver(A, B2) = (pos_B2 + T, pos_A + T]`` -- both share the same right
edge ``pos_A + T`` and the nearer dominator's left edge is only larger.
So if the nearest dominator's sliver is non-empty, every farther
dominator's sliver is a superset and is also non-empty; the nearest
dominator is therefore always the hardest (most likely to be non-empty)
case, and checking it alone is complete. This is also what collapses the
rule to an O(m) monotonic-stack scan (the "previous smaller-or-equal
element" problem) plus one O(log m) bisection per candidate, with no
distance-window prefilter of any kind: a farther-back dominator can only
ever produce a wider, harder-to-empty sliver, so a window can only ever
prune *less* than the exact rule -- it buys no speed and no correctness,
only an unproven constant. For the same reason, no
``pos_B + tank_range_mi >= total_route_mi`` early-exit branch is added
either: the general sliver/FINISH test above already yields that exact
result (when ``sliver_lo >= total_route_mi`` no candidate can be inside the
sliver and the FINISH test is false by construction), so a separate branch
would add proof surface for zero behavioral change.

## No cascade (D-10)

Every sliver-emptiness test in this module is evaluated against the
original, unpruned candidate list -- a candidate that this pass judges
removable still participates as a potential dominator for later candidates
and still occupies its position in the occupancy check for earlier ones.
There is no fixed-point iteration that re-runs emptiness tests against a
shrinking set. A cascading version could in principle prune more, but it
would need an inductive argument about evaluation order that this
single-pass rule does not, and this phase's entire premise is
provable-over-effective: a subtle ordering induction is exactly the kind of
mistake a "prove it, don't just measure it" prune must not risk.

## Why there is no flattened multi-leg variant (D-07)

This module's entire input is a flattened ``(distance_from_start_mi,
price_per_gallon)`` list on a single distance axis -- it is structurally
incapable of seeing leg boundaries, because nothing in its signature or
algorithm carries per-leg information. A multi-leg route generates this
same flattened-list input shape via a longer single axis (see
``routing.services.multi_leg``), so there is nothing for a "multi-leg
variant" of this module to do differently. The absence of one is a derived
consequence of the input shape, not an oversight.
"""
from bisect import bisect_right
from decimal import Decimal

from routing.services.solver import Candidate


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def prune_dominated_candidates(
    candidates, *, tank_range_mi, total_route_mi
) -> list[Candidate]:
    """Return the subset of ``candidates`` that survive the Domination
    Theorem above, as a new list sorted by the total order
    ``(distance_from_start_mi, price_per_gallon, opis_id)`` -- byte-for-byte
    the key ``solver.solve()`` already sorts its own working copy by. Every
    returned element is one of the input objects, never a reconstruction.
    An empty input returns an empty list.
    """
    tank_range_mi = _as_decimal(tank_range_mi)
    total_route_mi = _as_decimal(total_route_mi)

    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )
    positions = [c.distance_from_start_mi for c in ordered]

    # Single left-to-right monotonic-stack pass: for each i, the nearest
    # earlier index j with price_j <= price_i is i's dominator. The stack
    # holds indices with non-decreasing price bottom-to-top; the pop
    # condition below is STRICT (">"), never ">=" -- see the D-11 co-located
    # note in the module docstring above for why a non-strict pop would
    # remove both halves of an identical-position, identical-price pair.
    dominator = [None] * len(ordered)
    stack = []
    for i, candidate in enumerate(ordered):
        while stack and ordered[stack[-1]].price_per_gallon > candidate.price_per_gallon:
            stack.pop()
        dominator[i] = stack[-1] if stack else None
        # Pushed unconditionally -- every index joins the stack whether or
        # not it will later be judged removable (D-10 no-cascade).
        stack.append(i)

    removable = [False] * len(ordered)
    for i, j in enumerate(dominator):
        if j is None:
            continue

        sliver_lo = positions[j] + tank_range_mi
        sliver_hi = positions[i] + tank_range_mi

        occupied = bisect_right(positions, sliver_hi) - bisect_right(positions, sliver_lo)
        if positions[i] > sliver_lo:
            # i's own position falls inside (sliver_lo, sliver_hi] -- this
            # happens exactly when B cannot reach A on a full tank. The
            # theorem's condition 3 asks about *other* candidates only, so
            # i must not count as its own blocker.
            occupied -= 1

        finish_in_sliver = sliver_lo < total_route_mi <= sliver_hi

        removable[i] = occupied == 0 and not finish_in_sliver

    return [ordered[i] for i in range(len(ordered)) if not removable[i]]

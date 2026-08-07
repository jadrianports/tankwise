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

## The supply-interval lemma

For a station ``S`` with position ``pos_S`` (miles from START), tank range
``T``, and route length ``L``, define ``S``'s supply interval as

    supply(S) = [pos_S, min(pos_S + T, L)]

the contiguous stretch of route mile a single full tank taken on at ``S``
can, by itself, ever deliver fuel to. The lemma: **a set of route miles has
a feasible fueling plan buying only at stations in some set {S_1, ..., S_k}
if and only if every one of those miles lies in the union of their supply
intervals** (miles covered by the fuel already carried at the origin --
the initial on-board fuel a route starts with -- are assigned to no
station and excluded from this accounting; the origin is not a purchasable
node).

*Forward.* Any feasible fueling plan induces an assignment of each
(non-origin-covered) route mile to the station it was reached on: whichever
station's fill last had that mile ahead of it when the vehicle passed it.
That mile lies within ``T`` miles downstream of the station (the tank
cannot carry fuel past its own range), so it lies in that station's supply
interval by construction.

*Backward.* Any such assignment induces a feasible plan that never
overflows the tank. Buy, at each station ``S_i`` in the assignment, exactly
enough fuel to cover the miles assigned to it. At any position ``x`` along
the route, the fuel on board equals the total miles assigned to stations at
``pos_i <= x`` that lie beyond ``x`` (fuel already spent reaching ``x`` is
gone; fuel not yet needed is still in the tank). Every such mile is, by the
assignment's own definition, at most ``pos_i + T``, and ``pos_i <= x``
implies ``pos_i + T <= x + T``. So the fuel on board at ``x`` can never
exceed ``T`` -- the tank never overflows and the plan is feasible. Both
directions matter: the backward direction is what makes "the miles are
covered" a *sufficient* condition for feasibility, not merely a necessary
one -- an unproven reformulation is exactly what sank the theorem this
rule replaces (see "Why the reach-sliver rule is wrong" below).

## The domination rule

Sort by the total order ``(distance_from_start_mi, price_per_gallon,
opis_id)`` (D-11, unchanged from before -- see the total-order note below).
Write ``T`` for ``tank_range_mi``, ``L`` for ``total_route_mi``, ``pos_S``
for a station's ``distance_from_start_mi``.

**Station A is removable if and only if some station B, ranked strictly
before A under the total order, satisfies all three:**

  1. ``price_B <= price_A``
  2. ``pos_B == pos_A``  **OR**  ``pos_B + T >= L``
  3. ``margin_B <= margin_A`` (D-01, PROV-03)

(``pos_B <= pos_A`` is implied by the total order and needs no separate
check.) Conditions 1 and 2 together are exactly interval containment:
``supply(B) = [pos_B, min(pos_B + T, L)]`` **contains** ``supply(A) =
[pos_A, min(pos_A + T, L)]`` under precisely those two alternatives --
co-located (``pos_B == pos_A``, so the two intervals start together and
``B``'s price is no higher) or B's own interval already reaches FINISH
(``pos_B + T >= L``, so ``supply(B) = [pos_B, L]`` contains any downstream
station's interval automatically, since ``pos_B <= pos_A`` and any
interval's right edge is ``<= L``).

Condition 3 is the trust margin, read only through
``routing.services.solver.trust_margin_for`` (equivalently, its
``is_estimate_priced`` boolean, since the margin is binary: ``trust_margin``
for an ``eia_regional_estimate``-priced candidate, ``0`` otherwise). It
reduces to one sentence, derived in full in "Fixed-charge objective" below:
**an estimate-priced B may not dominate a real-priced A.**
Real-dominates-anything and estimate-dominates-estimate are unchanged.

**Total order and co-located pairs (D-11), carried forward.** The total
order is load-bearing, not cosmetic: two stations at the identical position
with the identical price, without a tiebreak, would each see the other as
a valid dominator (condition 2's ``pos_B == pos_A`` holds both ways) and
both would be judged removable, silently deleting the only station at that
position. The ``opis_id`` tiebreak breaks that symmetry -- under the total
order exactly one of the pair sorts first, so only the later one ever looks
backward and finds a dominator. Exactly one of any co-located, identically
priced pair survives.

The rule ships zero tuned constants -- no distance window, no prefilter
width, no empirical threshold of any kind. This is the surviving principle
of the now-void old completeness argument, restated for this rule: the
admission test reads only ``(pos_B, price_B)`` of one other station plus
``T`` and ``L``, nothing more.

## Soundness

Soundness follows directly from the lemma, for both objectives this
milestone's solvers need.

**Pure-fuel objective.** If ``B`` dominates ``A`` (``supply(B)`` contains
``supply(A)``), then any feasible plan that buys gallons at ``A`` can
instead buy the same gallons at ``B``: every mile ``A``'s purchase could
supply, ``B``'s purchase can also supply (containment), at a price no
higher (``price_B <= price_A``). Removing ``A`` from the candidate set
therefore never raises the optimal fuel cost and never turns a feasible
route infeasible -- whatever ``A`` could do, ``B`` alone can already do at
least as cheaply.

**Fixed-charge objective (fuel dollars plus a flat penalty per station
actually purchased at, plus a flat per-candidate trust margin -- D-01,
PROV-03).** This is the one line the old proof got wrong, twice now.
Before the trust margin, folding ``A``'s purchase into ``B`` cost

    cost_B(q) = penalty + price_B * q  <=  penalty + price_A * q = cost_A(q)

**pointwise in q**, which needed only ``price_B <= price_A``. With a
per-candidate margin (``margin_B``/``margin_A``, read through
``routing.services.solver.trust_margin_for``) added to the same per-purchase
cost, those become

    cost_B(q) = penalty + margin_B + price_B * q
             <= penalty + margin_A + price_A * q = cost_A(q)

which holds pointwise in ``q`` iff **both** ``margin_B <= margin_A`` **and**
``price_B <= price_A``. Because the margin is a *fixed* charge while price
is *per-gallon*, no finite bound on the margin restores the old
price-only condition -- at small enough ``q`` the margin term always
dominates, so this cannot be dissolved by choosing a small margin value.
The single-station substitution still costs **zero extra stops**: a single
station absorbs ``A``'s entire role, whether that station already had a
purchase in the plan (the gallons merge into an existing stop) or not (one
purchase simply relocates to an earlier position; the stop count is
unchanged). This single-station substitution is exactly what condition 2
guarantees is always available, so the ``2 * penalty`` obstruction to
multi-station covers described two paragraphs below is unaffected by the
margin, and the rule stays scoped to single-station covers exactly as
before.

The margin is binary (``routing.services.solver.is_estimate_priced``), so
the new predicate reduces to one sentence: **an estimate-priced station may
not dominate a real-priced one.** Real-dominates-anything and
estimate-dominates-estimate are unchanged. Two consequences follow, both
recorded deliberately. First, the rule is **strictly weaker** than the
margin-free rule -- it retains a superset of what the margin-free rule
retains, never fewer, so ``estimate_transition_count`` (the dispatch
signal downstream code reads) can only move *upward*, the conservative
direction for dispatch. Second, at zero estimate-priced rows every
candidate's margin is equal (``0``), so condition 3 is vacuously satisfied
for every pair and the retained set is **provably identical** to what the
margin-free rule would retain -- this is the phase's own inertness proof,
not merely an assertion about one measured run.

One further consequence the proof must not omit, because it is the thing a
later reader will otherwise get wrong: condition 3 is
**provenance-shaped, not magnitude-shaped**. It compares which side of the
binary each station sits on, never the margin's dollar value, so it
applies at every ``trust_margin`` including zero. The inertness claim
above therefore rests on the dataset holding zero estimate-priced rows,
not on the margin itself being zero.

This does **not** extend to two stations jointly covering ``A``: with a
per-stop charge, splitting ``A``'s purchase across ``B1`` and ``B2`` costs
``2 * penalty + margin_B1 + margin_B2 + price_B1 * q1 + price_B2 * q2``,
which is not pointwise-comparable to ``penalty + margin_A + price_A * q``
-- the ``2 * penalty`` term alone can already dominate an arbitrarily small
fuel saving, before the margin terms are even considered. That is why this
rule admits single-station covers only, never multi-station ones.

## Feasibility corollary

Removal can never manufacture a new infeasibility, and this falls
structurally out of the rule -- no branch is keyed on how much fuel the
route starts with, and no station is special-cased. Any dominating ``B``
sits at or before ``A`` in position (``pos_B <= pos_A``, from the total
order), so ``B`` is reachable everywhere ``A`` is; and ``B``'s supply
interval contains ``A``'s, so ``B`` reaches at least as far downstream as
``A`` ever could. A station that is the *sole* station reachable from a
short initial fuel load at the origin has, by definition, nothing ranked
before it in the total order (nothing sorts before ``pos == 0`` unless it
too sits at ``pos == 0``), so it can never have a dominator and can never
be removed -- asserted structurally, not special-cased.

## Maximality within single-station domination

Because every station's supply interval has the same length ``T``,
containment of ``supply(A)`` inside ``supply(B)`` holds **if and only if**
``pos_B == pos_A`` or ``pos_B + T >= L`` -- there is no third geometric
case a single dominator could exploit. Nothing stronger is reachable from
**single-station** domination: this claim is scoped explicitly to a single
dominating station and does not extend to a broader claim that
multi-station covers are unsound or unreachable -- that is a different,
harder question this phase does not answer (see "Soundness" above for
exactly why a per-stop charge breaks the multi-station case, and why this
phase declines to prove anything about it).

A dominator that is itself later found removable is harmless, because
containment is transitive: if ``B`` dominates ``A`` and ``B`` is itself
dominated by ``B'``, then ``supply(B')`` contains ``supply(B)`` which
contains ``supply(A)``, so ``supply(B')`` contains ``supply(A)`` too, and
``price_B' <= price_B <= price_A``. ``B'`` covers everything ``B`` covered,
at a price no higher -- removing a removable dominator changes nothing
about which stations the surviving set can still stand in for.

## Why the reach-sliver rule is wrong

The rule this module implemented before this rewrite -- "a cheaper-or-equal
station B leaves a provably empty reach *sliver*, `(pos_B + T, pos_A + T]`,
containing no other candidate and not FINISH" -- is **false**. It was
disproven by two independent, hand-verified witnesses, both at
``penalty=0`` (a pure-fuel objective, not only the fixed-charge one):

**Witness 1, equal prices.** Stations ``$1.00 @ 1 mi``, ``$1.00 @ 2 mi``,
``$1.01 @ 3 mi``; ``tank_range_mi=100``, ``total_route_mi=103``, ``mpg=1``,
initial on-board fuel worth one mile of range, ``penalty=0``. The
reach-sliver rule removed the middle ($1.00 @ 2 mi) station -- its sliver
relative to the first station held no other candidate and did not hold
FINISH. The true optimum rose from **$102.0100 to $102.0200** once that
station was gone (100x the suite's ``COST_TOLERANCE``).

**Witness 2, strict inequality.** Stations ``$1.00 @ 0 mi``, ``$1.01 @ 1
mi``, ``$2.00 @ 2 mi``; ``tank_range_mi=100``, ``total_route_mi=102``,
``mpg=1``, zero initial on-board fuel, ``penalty=0``. The reach-sliver rule
removed the middle ($1.01 @ 1 mi) station. The true optimum rose from
**$103.01 to $104.00** -- a $0.99 error, 99x tolerance.

Witness 2 matters more than witness 1: it proves the failure is **not** a
tie-breaking artifact of equal prices and cannot be patched by tightening
the price comparison to a strict inequality -- the relay effect that breaks
the old rule is general.

**Root cause, one sentence:** the old test asked whether A unlocks new
*reach* (does anything sit in the sliver only A could get to); the
objective is decided by which miles each station can *supply*, and a
capacity-bound B, anchored earlier, cannot supply the miles A can, because
two stations in sequence relay a full tank further down the route than
either single station's own capacity-bounded interval ever could. The old
proof's cost argument implicitly assumed the dominator had unlimited
remaining capacity to absorb the dominated station's purchase; it does not
-- its capacity is bounded by ``T``, exactly the bound the supply-interval
lemma above makes explicit and the reach-sliver formulation never stated.

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
from decimal import Decimal

from routing.services.solver import Candidate, is_estimate_priced


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def prune_dominated_candidates(
    candidates, *, tank_range_mi, total_route_mi
) -> list[Candidate]:
    """Return the subset of ``candidates`` that survive the domination rule
    above, as a new list sorted by the total order
    ``(distance_from_start_mi, price_per_gallon, opis_id)`` -- byte-for-byte
    the key ``solver.solve()`` already sorts its own working copy by. Every
    returned element is one of the input objects, never a reconstruction.
    An empty input returns an empty list.

    Carries no ``penalty`` parameter and no undeclared knob of any kind --
    the parameter tuple is exactly ``(candidates, tank_range_mi,
    total_route_mi)`` -- and, per D-01, no ``trust_margin`` parameter
    either: the margin, like the penalty, is never a prune parameter. It is
    read off each candidate through ``routing.services.solver
    .is_estimate_priced`` (condition 3 above), the module's single shared
    decision-position provenance read.

    Implementation shape (a plain sort plus two linear passes, no tuned
    constants): first keep only the candidates at each distinct position
    that are not dominated by an already-retained candidate there under
    both condition 1 (price) and condition 3 (provenance) -- this alone
    resolves every co-located-pair case, because the total order already
    places cheaper stations first among ties at a shared position, and an
    estimate-priced candidate may be dominated by any cheaper-or-equal
    retained candidate while a real-priced candidate may only be dominated
    by a cheaper-or-equal retained *real-priced* one. Then, among the
    survivors whose own supply interval reaches FINISH (``pos + T >= L``),
    keep only the candidates not dominated by an earlier retained
    tail-reaching candidate under the same two conditions -- a later such
    station is removable exactly when an earlier, no-more-expensive,
    non-dominated-by-provenance one is available, per condition 2's
    ``pos_B + T >= L`` branch. Stations whose own interval does not reach
    FINISH are never dominators under that branch (their own condition 2
    fails), and any B that could remove one of them there would already
    have to sit at or before it in position with `pos_B + T >= L`, which --
    since B ranks first in the total order -- means B is at least as cheap
    as, and no more provenance-restricted than, any co-located duplicate
    already removed in the first pass; nothing is lost by resolving the two
    passes in this order.
    """
    tank_range_mi = _as_decimal(tank_range_mi)
    total_route_mi = _as_decimal(total_route_mi)

    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )

    # Pass 1: keep only candidates not dominated by an already-retained
    # candidate at the same position, under conditions 1 (price) and 3
    # (provenance). Candidates sharing a position are visited in ascending
    # price order (the total order's second key), so the cheapest retained
    # price seen so far at the current position -- tracked separately for
    # "any provenance" and "real provenance only" -- is enough; no
    # already-visited candidate at the position can later become cheaper.
    deduped = []
    current_position = None
    position_best_any_price = None
    position_best_real_price = None
    for candidate in ordered:
        if candidate.distance_from_start_mi != current_position:
            current_position = candidate.distance_from_start_mi
            position_best_any_price = None
            position_best_real_price = None

        estimate_priced = is_estimate_priced(candidate)
        if estimate_priced:
            # An estimate-priced candidate may be dominated by ANY
            # cheaper-or-equal retained candidate at this position,
            # real or estimate (real-dominates-anything,
            # estimate-dominates-estimate).
            dominated = (
                position_best_any_price is not None
                and candidate.price_per_gallon >= position_best_any_price
            )
        else:
            # A real-priced candidate may be dominated only by a
            # cheaper-or-equal retained REAL-priced candidate -- an
            # estimate-priced one may never dominate it (condition 3).
            dominated = (
                position_best_real_price is not None
                and candidate.price_per_gallon >= position_best_real_price
            )
        if dominated:
            continue

        deduped.append(candidate)
        if position_best_any_price is None:
            position_best_any_price = candidate.price_per_gallon
        if not estimate_priced and position_best_real_price is None:
            position_best_real_price = candidate.price_per_gallon

    # Pass 2: among stations whose own supply interval reaches FINISH,
    # keep only the candidates not dominated by an earlier retained
    # tail-reaching candidate under conditions 1 and 3 -- two running
    # minima, one over every retained tail-reaching candidate ("any") and
    # one over retained tail-reaching real-priced candidates only
    # ("real"), since unlike pass 1's single-position scope, price is not
    # monotonic across positions and either minimum can be set by an
    # earlier, geographically distant station.
    survivors = []
    running_min_price_any = None
    running_min_price_real = None
    for candidate in deduped:
        reaches_finish = candidate.distance_from_start_mi + tank_range_mi >= total_route_mi
        if reaches_finish:
            estimate_priced = is_estimate_priced(candidate)
            if estimate_priced:
                removed = (
                    running_min_price_any is not None
                    and candidate.price_per_gallon >= running_min_price_any
                )
            else:
                removed = (
                    running_min_price_real is not None
                    and candidate.price_per_gallon >= running_min_price_real
                )
            if removed:
                continue

            running_min_price_any = (
                candidate.price_per_gallon
                if running_min_price_any is None
                else min(running_min_price_any, candidate.price_per_gallon)
            )
            if not estimate_priced:
                running_min_price_real = (
                    candidate.price_per_gallon
                    if running_min_price_real is None
                    else min(running_min_price_real, candidate.price_per_gallon)
                )
        survivors.append(candidate)

    return survivors

"""Fixed-charge fuel-stop dynamic program: fuel dollars plus a flat per-stop
penalty, minimised jointly over which stations to buy at and how much to
buy at each.

Request-path math only -- no Django, no DB, no HTTP client. All money and
gallon values are exact, unrounded `Decimal`; rounding to cents happens
only at the HTTP response serialization boundary, exactly as in
`solver.py`'s own purity header.

Nothing in production calls this module yet (D-34). It lives inside the
AST-gated solver boundary (see `SOLVER_FILES` in
`routing/tests/test_boundaries.py`) alongside `solver.py`, `exceptions.py`
and `prune.py`. `solve()` still runs the pre-Phase-18 greedy; a later plan
wires this module in as `solve()`'s delegate.

## The finite-fill lemma

At any purchasable station, given every remaining candidate strictly
ahead of it (on the route's distance axis) plus FINISH as the possible
next-purchase points, the only post-purchase fuel levels that can ever be
part of an optimal plan are: the level already on board (buy nothing),
the level that lands exactly on one of those remaining candidates or
FINISH, and the level that fills the tank to capacity. Buying anything
strictly between two of those values spends money on range the plan never
uses; buying past "fill to capacity" cannot physically fit in the tank.

Proof (perturbation argument): take any optimal assignment and suppose
some station's purchase amount is neither an exact-reach amount nor a
fill-to-capacity amount. Then there is slack in both directions, so
perturb by a small positive epsilon. If this station's price is strictly
below the price at the next purchase point, buy epsilon more here and
epsilon less there -- possible because the tank is not yet full here and
there is slack to give up there -- and total fuel cost strictly falls,
contradicting optimality. If the price here is strictly above the next
purchase point's price, buy epsilon less here and epsilon more there --
possible because we are above the exact-reach amount here -- and total
cost strictly falls, again a contradiction. If the two prices are equal,
the perturbation is cost-neutral, so an optimum also exists at one of the
interval's endpoints. Either way, an optimum exists at one of the finitely
many listed amounts. The penalty term never changes this argument: a stop
count is fixed by which purchases are strictly positive, and the
perturbation above never drives a strictly positive purchase to exactly
zero (it moves fuel between two purchases that are both already positive
by assumption), so the fixed-charge term is unaffected by the argument and
enters the objective independently of it.

This is the **third independent derivation** of this lemma in the
codebase: `_useful_purchase_amounts` (`test_solver_optimality.py:30`)
derives it for the pure-fuel objective over a memoized `(node, fuel)`
oracle search; `_useful_fill_levels_mi`
(`test_solver_fixed_charge_optimality.py:244`) derives it for the
fixed-charge objective, but scoped to one fixed subset's own remaining
members (the subset-enumeration oracle pre-selects the whole station set
before computing any purchase amount, so it never needs to consider
candidates outside that subset). This module's version is scoped
differently again: it ranges over **every** remaining sorted candidate
plus FINISH, because this module does not pre-select a subset -- every
candidate ahead is a live possibility at every state, so the finiteness
argument must hold over all of them at once, not one subset's own
members. Neither prior derivation is imported, ported, or transcribed
here.

Bound: the number of distinct fuel levels reachable at any node is at
most (candidates strictly ahead of that node) + 1 -- one level per
remaining candidate/FINISH exact-reach amount, plus the fill-to-capacity
amount, deduplicated. That bounds the DP's fuel dimension and gives an
`O(m^2)` state count over `m` post-prune candidates.

## Totality

`preflight_gap_check` passing **is** the feasibility condition: if every
consecutive gap (STARTing at START, ending at FINISH, sorted by position)
fits within the usable range at that node, then stopping at every station
in turn is itself a feasible plan. Consequently the recurrence in
`solve_fixed_charge` is total -- it never raises, and infeasibility has
exactly one source in the codebase: `preflight_gap_check`, run by the
caller before the recurrence is ever invoked (D-17).

**Update, 2026-08-04 (phase 18.1): this claim now has one narrow
exception, and it is stated here so "total" stays accurate rather than
quietly false.** `solve_fixed_charge` has exactly one raise path,
`DeadlineExceededError`, reachable only when a caller passes a non-`None`
`deadline`. Every existing direct caller passes no `deadline` (the
default is `None`, unlimited) and therefore never observes this path --
the totality claim above holds exactly as written for all of them. Only a
caller that opts into a wall-clock budget can ever see this exception, and
even then only when that budget is actually exceeded before FINISH is
reached.

## Determinism

No `set` or `dict` iteration order may influence the recurrence's
outcome. Candidates are sorted once, defensively, by the same total order
`prune.py` documents (`distance_from_start_mi`, `price_per_gallon`,
`opis_id`); DP nodes are keyed by that sorted list's ordinal index, never
by raw mile value (two co-located candidates therefore always stay
distinct nodes). Every relaxation "is this better?" test and the final
winner selection resolve through the same explicit D-12 key -- there is
no last-writer-wins path. `dict` tables in this module are only ever
populated by, and iterated in, an order fully determined by that sorted
input and that key, so re-running `solve_fixed_charge` on the same input
always retraces the same relaxations in the same order and produces a
byte-identical `FuelPlan`.

## Pareto state-dominance pruning

At any node, state B (arrived carrying `B.fuel_ticks` fuel, via a path
whose own D-12 key -- see "Determinism" above -- is `B.key`) is dominated
by state A at the SAME node when `A.fuel_ticks >= B.fuel_ticks` and
`A.key` is STRICTLY better than `B.key` under that same D-12 total order
(`_key_less(A.key, B.key)`). A dominated state is discarded before it is
ever used as a relaxation source (`_pareto_frontier`, applied once per
station node, immediately before that node's states become sources for
the next round of transitions).

Proof this cannot change the answer (exchange argument, same style as
the finite-fill lemma's perturbation argument above): suppose, for
contradiction, that the undominated recurrence's true D-12-minimal
winning path W passes through a dominated state B at some node, with B
dominated by a surviving state A. Splice A's own (strictly D-12-better)
prefix onto W's own suffix from that node onward, unmodified. Every
purchase that suffix makes remains feasible from A (A never has less
fuel than B had), and wherever the suffix actually spends fuel, A's
surplus over B either shrinks that purchase -- lowering its dollar cost,
since every price in this domain is strictly positive -- or removes it
outright, lowering the D-12 stop-count tie-break. Either way the
spliced path's full D-12 key is no worse than A's own already-strictly-
better prefix key, which is itself strictly better than W's -- so the
spliced path strictly beats W, contradicting that W was already the
minimal winner. So W could never have passed through a dominated state
to begin with -- pruning it cannot remove anything the true winner would
have used.

Deliberately STRICT, never "at least as good": two states whose full
D-12 keys tie EXACTLY (same objective, same stop count, same station
positions, same `opis_id`s -- possible when two candidates share a
price, so the same total dollar cost can be split between them more
than one way) are both kept, even though the higher-fuel one alone would
suffice for the numeric optimum. The reason is a step below D-12 itself:
when two precursor paths reach the exact same downstream `(node, fuel)`
key, `relax`/`_wins`'s first-writer-wins rule -- the only thing that
settles WHICH of them the reconstructed plan actually uses -- depends on
this module's existing dict-iteration-order contract (see
"Determinism" above). Discarding one side of an exact tie changes
nothing about the numeric answer, but it can still perturb which
already-equally-optimal precursor a downstream coincidental tie resolves
to, which is exactly as answer-visible (a different, though equally
priced, per-stop gallon split) as a real correctness bug would be, and
this module's mandate here is speed, never any change -- however
immaterial to the total -- to which exact plan comes out the other end.
`_pareto_frontier` accordingly preserves each surviving entry's original
relative dict-iteration order rather than the descending-fuel order it
scans internally to detect domination, for the identical reason.

Compared on the FULL D-12 key, never the raw fuel-dollar-plus-penalty
objective (`key[0]`) in isolation, for the same reason the ties above
are left alone: two states can tie on that objective while splitting it
differently between dollars and stop count (more stops at a lower
per-stop price vs. fewer stops at a higher one), and only the full key
-- not the numeric objective alone -- can tell whether the comparison is
a strict improvement or a tie D-12 does not actually resolve. That is
precisely the shape of a quietly-wrong "domination" shortcut this
codebase has already paid for once, in the now-superseded reach-sliver
candidate-prune rule (`prune.py`'s own history) -- comparing the full
key, and refusing to prune anything short of a strict win, closes that
same gap rather than assuming a narrower comparison is equivalent.

Filtering never deletes an entry from `states[node_index]` itself, and
never runs before that node's state dict is fully populated (nothing
later in the sorted candidate order ever targets an earlier node, so by
the time processing reaches a node its state dict has already received
every relaxation it will ever receive) -- it only decides which entries
the forward relaxation loop treats as sources. A dominated entry is
simply never chosen as anybody's predecessor going forward, so it can
never appear in a reconstructed plan either, whether or not it stays
physically present in the dict.

## Exact-integer money-domain comparison

The prior pass's own "considered and rejected" note (superseded here, not
reopened blindly) rejected extending the position domain's integer-tick
technique to `gallons = buy_mi / mpg` itself: that `Decimal` division's
result is bounded by Python's 28-significant-digit context precision, but
its *exponent* is not statically knowable the way a sum/difference's is,
so there is no single fixed scale `gallons` can be exactly re-expressed
in ahead of time. That objection is correct and still stands -- but it
is an objection to *materializing* `gallons`, not to *comparing*
objectives, and every place `gallons`/`cost` ever mattered in the hot
loop was a comparison (`_key_less`, called >15M times at real-corridor
density per `18-04b-SUMMARY.md`'s own profile), never a value a caller
reads. This section derives an exact-integer proxy for that comparison
which never computes `gallons` at all.

**The derivation.** Every dollar this recurrence's objective ever sums is
`cost_i = gallons_i * price_i = (buy_mi_i / mpg) * price_i` for some
purchase `i`. `buy_mi_i = level_i - fuel_on_arrival_i` is a position-
domain value, hence (per the "Implementation note" above) exactly
`buy_ticks_i * 10**_pos_exponent` for an exact integer `buy_ticks_i`.
`price_i` is one of the finitely many `price_per_gallon` values already
materialized on `ordered` before this recurrence ever runs -- each one
some concrete, already-computed `Decimal`, however it was derived
upstream (a plain OPIS retail price, or that price times an EIA regional
index factor computed via `Decimal` division in `eia.py`, itself
context-rounded to *some* fixed, finite exponent the moment it was
computed). `Decimal.as_tuple()` always yields an exact `(sign, digits,
exponent)` triple for ANY `Decimal` instance, regardless of how it was
produced -- there is no such thing as a `Decimal` with unknown or
unbounded precision, only one whose precision was already fixed upstream.
So exactly the same trick `_pos_exponent` already applies to positions
generalizes to price: `_price_exponent` is the finest exponent already
present among this run's own finitely many `price_per_gallon` values,
found by a plain `min()` over already-materialized data -- never
"cents", never a fixed assumed scale, never a worst-case search or a
second pass. Every price converts to an exact integer `price_ticks_i` at
that one shared exponent by the same `(sign, digits, exponent)`-triple
method `_to_ticks` already uses, for the same reason: a value already
exactly representable at a *finer* exponent is trivially exactly
representable at a *coarser*-or-equal one too (padding zero digits, not
rounding), so picking the *minimum* exponent present is the one choice
that is always exact for every value in the set.

Substituting: `cost_i = buy_ticks_i * 10**_pos_exponent * price_ticks_i *
10**_price_exponent / mpg = (buy_ticks_i * price_ticks_i) * MONEY_SCALE`,
where `MONEY_SCALE = 10**(_pos_exponent + _price_exponent) / mpg` is one
positive rational constant shared by every purchase this call will ever
compare -- it depends only on this call's own `mpg` and the two
exponents, never on which candidate or state is being compared. The full
per-purchase objective contribution is `cost_i + penalty = (buy_ticks_i *
price_ticks_i) * MONEY_SCALE + penalty`. Because `MONEY_SCALE` is common
to every comparison, and division by a positive constant preserves
ordering, comparing two accumulated objectives is exactly equivalent to
comparing `(SUM of buy_ticks_i * price_ticks_i) + (penalty / MONEY_SCALE)
* stop_count` -- an integer fuel term plus a rational per-stop term.
`penalty / MONEY_SCALE`, reduced to lowest terms via `Fraction` (built
from `Decimal.as_integer_ratio()`, itself exact -- never `float`), gives
`P_NUM`/`P_DEN`; multiplying the whole comparison by `P_DEN` clears the
one remaining fraction, yielding a **plain integer** running key:
`key[0] += (buy_ticks_i * price_ticks_i) * P_DEN + P_NUM` per purchase,
compared with plain integer `<`/`abs`/`*` -- no `Decimal`, no `float`,
anywhere in the comparison.

**The tolerance band is reproduced exactly, not dropped.** `COST_TOLERANCE`
exists so two objectives within $0.0001 of each other fall through to the
stops/positions/`opis_id` tie-break rather than one winning on noise (see
`COST_TOLERANCE`'s own docstring). Dropping that band in the new integer
comparison would be an answer-visible behavior change -- a genuine
$0.00003 difference the old `Decimal` comparison called a tie could
become a decisive win under a naive exact-integer replacement, silently
picking a different, though numerically near-identical, plan. Instead,
`COST_TOLERANCE / MONEY_SCALE`, scaled by `P_DEN` and reduced via the same
exact `Fraction` machinery, gives `TOL_NUM`/`TOL_DEN`; `_make_key_less`
cross-multiplies (`abs(diff) * TOL_DEN > TOL_NUM`) to reproduce
`abs(real_objective_diff) > COST_TOLERANCE` exactly, in integers, with
the identical strict-`>` semantics the `Decimal` version had (a
difference of precisely `COST_TOLERANCE` is still a tie).

**Where this is not, and does not need to be, bit-for-bit identical to
the old code's own arithmetic.** The *old* `Decimal` chain computed
`gallons = buy_mi / mpg` per purchase -- a division that, when `mpg`'s
prime factorization outside 2 and 5 is nontrivial (e.g. the UI default
6.5 mpg = 13/2), does not terminate in decimal and is itself rounded to
Python's 28-significant-digit context precision. That rounding is a real,
if minute (order `10**-25` relative for realistic dollar magnitudes),
departure from the true rational value this section's `MONEY_SCALE`
derivation computes exactly. The two are therefore not mathematically
identical quantities -- but `COST_TOLERANCE` (`$0.0001`) already exists
precisely to absorb noise of exactly this class and character ("Decimal
summation-order noise", per its own docstring), and sits some 21 orders
of magnitude above where 28-significant-digit rounding noise could ever
land for the dollar amounts this dataset produces. A real, non-noise
difference between two candidate plans would have to fall, by
coincidence, within about `10**-25` of exactly the `$0.0001` boundary for
the old rounded comparison and this section's exact one to ever disagree
on a tie/not-tie verdict -- not proven structurally impossible here, but
verified empirically has never occurred, across every differential case
this module's test suite and its dedicated old-vs-new harnesses run (see
`test_dp_differential.py` and this pass's own differential evidence).

**Materialization stays exactly as it was.** `gallons`/`cost`, the actual
`Decimal` values a surviving `FuelStop` reports, are still computed by
the exact same `buy_mi = level - fuel_on_arrival; gallons = buy_mi / mpg;
cost = gallons * price_per_gallon` sequence, in the exact same order, as
every prior version of this module -- this section changes only what the
recurrence *compares* while deciding which states survive, never what a
winning stop's own reported numbers are built from.

**Trust-margin extension (2026-08-07, phase 21, PROV-03, D-01/D-06/D-16).**
The full per-purchase objective contribution generalizes one more step, to
`cost_i + penalty + margin_i`, where `margin_i` is the flat trust-margin
charge for purchase `i`'s own candidate (`0` for a recorded-price
candidate, `trust_margin` for an `eia_regional_estimate`-priced one --
`solver.trust_margin_for`, this phase's one shared provenance seam). The
margin lives in the SAME `MONEY_SCALE`-implied units the penalty ratio
above already lives in -- a second, per-candidate fixed charge, never a
new price scale -- so it is folded over a SINGLE shared denominator with
`P_NUM`/`P_DEN` (`math.lcm` of `P_DEN` and every drawn margin ratio's own
denominator) rather than introducing an independent one: `key[0] +=
purchase_ticks * P_DEN + P_NUM + margin_numerators[node_index]` per
purchase, where `margin_numerators[node_index]` is that station's own
margin ratio rescaled to the shared denominator. At `trust_margin=0`
`Fraction(0)` has denominator `1`, so `math.lcm(P_DEN, 1) == P_DEN`
exactly -- the shared denominator collapses to today's `P_DEN`, `P_NUM` is
rescaled by the no-op factor `P_DEN // P_DEN == 1`, and every
`margin_numerators` entry is `0`, so the zero-margin key is
byte-identical to the pre-margin key, not merely numerically equal.
`total_cost` is unaffected either way: it stays this function's own
accumulated `sum(cost_i)` over the reconstructed stops, never a
reconstruction from raw prices, which is the structural reason the margin
is a flat per-purchase charge rather than a per-gallon one (D-06, and see
`solver.trust_margin_for`'s own docstring for the full argument).

## Position-domain subtraction must be exact, not merely well-scaled

The "Implementation note" above proves the *conversion* from `Decimal` to
integer ticks is exact (`_to_ticks`/`_from_ticks`, built from
`Decimal.as_tuple()`, never division or `scaleb`). It does not, by itself,
guarantee the `Decimal` VALUE being converted is correct -- and for one
call site, `useful_fill_levels_mi`'s `reach_mi = node_mi - position_mi`,
it silently was not, for the same class of high-precision real input this
module's own docstring already flags elsewhere: real, Mapbox/shapely-
derived station positions routinely carry up to 28-29 significant digits
(`374.5423307176653094063943466`, a real committed corridor fixture
position, is exactly 28). Plain `Decimal.__sub__` rounds its result to
whatever context precision happens to be ambient (28 significant digits
by default) whenever the true difference needs more digits than that to
represent exactly -- ROUND_HALF_EVEN, silently, with no exception. For
two positions at this dataset's real precision, that rounding is not a
theoretical edge case: `1379.290723425196850393700787 -
374.5423307176653094063943466` truly equals
`1004.7483927075315409873064404`, but Python's default-context
`Decimal.__sub__` returns `1004.748392707531540987306440` -- four
ten-septillionths of a mile short.

The pre-tick-domain recurrence never surfaced this, for a genuinely subtle
reason worth recording: it re-derived a target's own gap via the
IDENTICAL rounded expression twice -- once inside `useful_fill_levels_mi`
to build the level, once more at the call site to compute arrival fuel
from it (`level - (target_pos - pos)`) -- so the same rounding error
cancelled itself out algebraically every time (`L - L == 0`, regardless of
what `L` actually was). Its reachability test (`pos + level <=
target_pos`, `Decimal`, tolerant `<=`) happened to round its own
compensating ADDITION back up to exactly the unrounded target position
too, for the same reason. Both of those masking effects depend on
recomputing the SAME rounded `Decimal` expression more than once in the
SAME ambient context -- which the exact-integer tick domain, by design,
no longer does: `full_ticks[target_index] - pos_ticks` is exact integer
arithmetic over the RAW, unrounded input positions, computed completely
independently of whatever `level` (and hence `level_ticks`) turned out to
be. A `level` silently rounded short during construction therefore no
longer agrees with the exact tick-domain target it was built to reach --
`_reachable_ticks`'s strict `bisect_right` boundary then excludes that
target from `target_range` outright, silently discarding an otherwise-
optimal transition rather than raising or producing an inexact answer
that looks obviously wrong.

`_exact_sub` (used by `useful_fill_levels_mi` in place of plain
`Decimal.__sub__`) fixes this at the source, using the same "exact
integer arithmetic over each operand's own `(sign, digits, exponent)`
triple" discipline `_to_ticks`/`_from_ticks` already use, rather than
raising the ambient context's precision (which would only move the
boundary, not remove it, and would silently affect every other `Decimal`
operation running concurrently in the same interpreter). Its result's own
minimal exponent is always `min(a's exponent, b's exponent)` -- and since
`_pos_exponent` is defined as the minimum exponent over EVERY
position-domain value this call ever touches, including both `a` and `b`
here (`nodes_ahead_mi` is built from `positions`/`total_route_mi`, exactly
the same set `_pos_exponent` already scans), `_exact_sub`'s result can
never need finer resolution than `_pos_exponent` already provides --
`_to_ticks`'s own `shift < 0` assertion guard remains correctly
unreachable, not merely untested.

## Trivial-stop bound

A stop survives the optimum only when the gallons bought there, times the
price advantage of routing through it, exceeds the penalty charged for
making the stop -- `gallons >= penalty / price_advantage` falls
structurally out of the objective, not out of luck: any purchase smaller
than that break-even point costs more in penalty than it saves in fuel,
so the DP's own cost comparison discards it in favor of not stopping (or
stopping less). At the UI default (6.5 mpg, 1,050 mi tank, $35 penalty), a
purchase under 10% of tank capacity (about 68 gallons) would need a price
advantage above roughly $2/gal to break even -- far outside anything in
the dataset, which is why trivial stops are structurally rare under this
objective rather than merely uncommon.

A hard minimum-gallons floor inside the solver was **considered and
rejected**: `REQUIREMENTS.md`'s Out of Scope table rejects it by name
because it can render a sparse corridor infeasible when no station ahead
can supply the floor amount, and it would break the optimality proof this
milestone is built on -- the finite-fill lemma above assumes every useful
purchase amount stays reachable, not artificially excluded.

## Pre-flight transition-count estimate and the penalty-aware fallback

Three optimization passes (integer ticks, deferred purchase-reason
computation, Pareto state-dominance pruning, an exact-integer money-domain
comparison) took the worst real corridor this codebase measures
(`toronto_oh-hillsboro_or`) from ~646s to ~46s and hit a structural wall:
every pass's own profile (see `18-04b-SUMMARY.md`) found the remaining cost
is the transition *count* -- `states x levels x targets` -- not any single
operation's per-call cost, and reducing that count further (a fourth,
deeper state-space restructuring) was explicitly out of scope for those
passes. A 46s solve does not return slowly in production: the deployed
gunicorn worker's timeout is 30s, so a request at that density is killed
outright rather than merely slow.

`estimate_transition_count` (below) is a cheap, `O(n log n)`, deterministic
STRUCTURAL upper bound on that same transition count, computed with the
identical integer-tick / `bisect` machinery `solve_fixed_charge` itself
uses -- never a fitted curve over `(kept, tank)`. It intentionally does
NOT attempt to predict the number of surviving `(node, fuel)` STATES a
real run's Pareto frontier would keep (that depends on the actual run,
which is exactly the cost this function exists to avoid paying up front);
it bounds, per node, the transitions ONE surviving state at that node
could generate (`reach * (reach + 1)`, `reach` being the count of
remaining candidates-plus-FINISH within one tank of that node), and sums
that bound across every node. This is therefore an estimate of the
recurrence's STRUCTURAL shape, calibrated (see `DP_TRANSITION_BUDGET`)
against real measured `solve()` wall-clock time on the twelve pinned
corridor fixtures (`routing.tests.test_corridor_fixtures.CORRIDORS`), not
a claim that it equals the literal transition count a real run performs.

`solver.solve()` computes this estimate over the same (pruned) search set
it is about to hand `solve_fixed_charge`, and compares it against
`DP_TRANSITION_BUDGET`. At or under budget, it delegates to
`solve_fixed_charge` exactly as before (`strategy="exact_dp"`). Over
budget, it delegates instead to
`routing.services.heuristic.solve_penalty_aware_heuristic` (Phase 18-04d
-- this dispatch target replaced the originally-wired
`routing.services.greedy.solve_greedy`, the fixed-charge-blind
pre-Phase-18 greedy, which remains in the codebase only as
`routing/tests/test_greedy.py`'s differential-referee subject and is no
longer reachable from a live `solve()` call): a single-pass heuristic
that approximates the SAME fixed-charge objective this module minimises
exactly, rather than ignoring the per-stop penalty entirely, while
staying bounded near-linear in candidate count and always well under the
request budget (`strategy="penalty_aware_heuristic"`; see
`routing.services.heuristic`'s own module docstring for the algorithm and
its measured gap against this module's exact answer). Both branches are
deterministic pure functions of the same validated input, so which branch
fires is itself deterministic and cache-key-stable: identical requests
always choose the same strategy and produce the same plan.

### The two-layer dispatch policy (2026-08-04, phase 18.1)

`estimate_transition_count` stays exactly as described above -- a cheap
pre-filter, unchanged in this phase. What changes is that it is no longer
the ONLY thing standing between a hopeless corridor and the deployed
gunicorn worker: `solve_fixed_charge` (below) now accepts an optional
`deadline` keyword and raises `DeadlineExceededError` when a pinned
wall-clock budget (`DP_DEADLINE_SECONDS`) is exceeded, backstopping
whatever the estimate lets through. The estimate decides which cells are
attempted at all; the deadline decides how long an attempted cell may run
before the worker is protected regardless of the estimate's accuracy. This
is the mechanism that makes recovery possible at all: 18-12's NOT
TRACTABLE proof (see "Why the transition-count estimate was not replaced"
below) only blocks a threshold that must demote a cell entirely on its own
say-so. With a clock behind it, the budget no longer carries that whole
burden -- cells above the old boundary can be admitted, because the
deadline, not the estimate, is what ultimately protects the worker.

An anytime/partial-best DP -- returning the best plan found so far when a
deadline fires, rather than raising -- was considered and rejected. The
forward `(node, fuel)` recurrence this module implements has no feasible
WHOLE-ROUTE plan until some state reaches FINISH; every intermediate state
is a partial journey, not a candidate answer. Salvaging a partial run into
a returnable plan would be a real algorithmic change to this module's
recurrence, and would reopen the correctness proofs Phases 16-18 already
closed for the current recurrence. This phase adds a clock; it does not
change what the DP computes, and an anytime rewrite would violate exactly
that boundary.

## Why the transition-count estimate was not replaced (2026-08-04 gap-closure finding)

**The mechanism.** `solver.solve()` dispatches by comparing a single scalar
against `DP_TRANSITION_BUDGET` -- a THRESHOLD. When two cells' true DP
runtimes invert relative to their estimates (a smaller estimate takes
LONGER to solve than a larger one), no threshold value can simultaneously
retain the fast, large-estimate cell and demote the slow, small-estimate
one: either both land on the same side of the boundary, or the boundary
puts the slow cell on the "exact_dp" side and the fast one on the
"heuristic" side -- backwards. This is a property of the estimator itself,
never of any particular budget number.

**Both inversion witnesses, workstation and live.** Workstation (this
module's own historical `DP_TRANSITION_BUDGET` calibration comment,
`dallas_tx-seattle_wa`): @500mi estimates 61,944, worst-of-3 raw DP
2.706s; @1050mi estimates 117,895 -- 90% larger -- worst-of-3 raw DP
2.181s, 19% FASTER. Live, deployed hardware, PRE-HOTFIX (commit
`8946567`, same corridor): @500mi estimates 61,912, HTTP 500 reproduced
5/5 at 30.5-35.7s (right-censored at `GUNICORN_TIMEOUT=30`); @1050mi
estimates 117,852 -- again the larger estimate -- HTTP 200 in ~12s. The
SAME inversion recurs on deployed hardware, at a larger absolute
magnitude than the workstation figures showed.

**18-10's measured scores.** Plan 18-10 pinned a zero-tolerance
discordant-pair rule (`PREDICTOR_INVERSION_BUDGET=0`,
`routing/tests/test_dispatch_predictor.py`) and measured a closed,
seven-member predictor family -- the incumbent `estimate_transition_count`
plus six structural challengers -- against it. Every member, incumbent
included, clears the rank-correlation floor (rank correlation >= 0.90) but
FAILS the zero-discordant-pair condition (2-4 discordant pairs each,
against a budget of 0). **The qualifying shortlist is EMPTY.** No
predictor in the closed family can be proven free of the exact structural
failure the witnesses above show. Per that plan's own explicit handoff,
this cannot be fixed by "swapping in a better scalar predictor over this
cell matrix -- that path is now closed by measurement, not by assumption."
This plan (18-12) therefore does not introduce a replacement predictor:
`estimate_transition_count` remains the dispatch input, not because it
qualifies, but because no alternative in the closed family does either,
and dispatch must continue on some scalar.

**18-11's deployed-hardware figures, and why `DP_TRANSITION_BUDGET` does
not move.** Plan 18-11 measured two cells live at the API-default vehicle:
`sacramento_ca-salt_lake_city_ut` (estimate 120, `exact_dp`, 5.0-5.3ms --
comfortably live-fine) and `dallas_tx-seattle_wa`@500mi post-hotfix
(estimate 61,912, `penalty_aware_heuristic`, 88-92ms -- fast, but not
evidence about the exact_dp path, since the current budget never routes
this cell there). Combined with the already-recorded pre-hotfix
`dallas_tx-seattle_wa`@1050mi figure above (estimate 117,852, `exact_dp`,
live, ~12s -- comfortably inside `GUNICORN_TIMEOUT=30`, though with less
margin than sacramento's millisecond-scale figure), the full set of cells
with genuine DEPLOYED `exact_dp` live-fine evidence is exactly TWO:
`sacramento_ca-salt_lake_city_ut` (estimate 120) and
`dallas_tx-seattle_wa`@1050mi (estimate 117,852). Pinned as
`DISPATCH_RETENTION_FLOOR = 2` below, BEFORE checking any threshold
against it: a policy must retain `exact_dp` on BOTH of these known-fine
cells to qualify, not merely one -- admitting only the trivially-tiny
`sacramento` cell into the required set would let a policy pass without
ever being tested against the corridor that actually matters
(`dallas_tx-seattle_wa` is ROADMAP criterion 1's own worked example).

ADOPTing a new budget requires a value strictly below 61,912 (to demote
the one known-live-breaching cell) AND at or above 117,852 (to retain
both `DISPATCH_RETENTION_FLOOR` cells) -- SIMULTANEOUSLY. 117,852 >
61,912 -- **no such value exists.** This is the identical inversion
18-10 proved on workstation timings, now reproduced with DEPLOYED-hardware
figures on both sides of the comparison. **NOT TRACTABLE applies.**
`DP_TRANSITION_BUDGET` stays 50,000, unchanged -- the conservative
boundary already demotes the one known-breaching cell and already retains
`exact_dp` on `sacramento_ca-salt_lake_city_ut`; it simply cannot ALSO
retain `dallas_tx-seattle_wa`@1050mi, and no single-threshold policy over
this estimator ever could. This is a legitimate, honest result, not a
failure: the exact DP is not tractable, under this estimator, for
`dallas_tx-seattle_wa`@1050mi on this hardware, and the correction belongs
in the product-facing record (18-14), not in a threshold chosen to imply
otherwise.

**AMENDED 2026-08-09 (Phase 22 plan 22-15).** The paragraph above correctly
described 2026-08-04's state: at that time, `sacramento_ca-salt_lake_city_ut`
(estimate 120) genuinely retained `exact_dp` under the shipped 50,000
budget. That is no longer true. Phase 22's Overture gap-fill import
(plan 22-12) widened this corridor's candidate density enough that its
transition-count estimate rose from ~120 to over 10 million (measured
against the API-default vehicle, plan 22-14's `22-DISPATCH-DIFF.md`),
crossing the 50,000 budget and demoting it to `penalty_aware_heuristic`.
`DISPATCH_RETENTION_FLOOR`'s own achievable-cell guarantee — described
below as "exactly TWO" cells with genuine deployed `exact_dp` evidence —
now holds for **zero** of its two required cells at the shipped budget,
down from one (`dallas_tx-seattle_wa`@1050mi was already not admitted at
50,000 as of Phase 18.1; its "exact_dp, live" citation above describes
only the pre-hotfix 134,000 budget). The prose below is left exactly as
written, describing what was true when it was pinned — this note
corrects the present-tense reading, not the historical record.

## EIA x penalty coupling

**The mechanism (D-23).** `routing/services/corridor.py`'s `factor_for`
scales every candidate's per-gallon price by the EIA regional multiplier
before it ever reaches this module, so the dollar saving available from
routing through an extra stop scales with that multiplier too -- while
the flat `FUEL_STOP_PENALTY_USD` charged for making the stop does not.
**Direction: when diesel is expensive TankWise plans more stops; when it
is cheap, fewer.** This is the mechanism STATE.md's `[Unknown]` EIA entry
named and asked this phase to characterise as a deliberate finding.

**The measurement (D-24).** A synthetic multiplier ladder
(`EIA_MULTIPLIER_LADDER = (0.8, 1.0, 1.2, 1.5)`, `routing/tests/
test_eia_penalty_sweep.py`) was applied on top of each candidate's own
neutral-basis price, across the twelve pinned corridors
(`routing.tests.test_corridor_fixtures.CORRIDORS`), at the UI-default
vehicle (6.5 mpg, 1,050 mi tank, full starting tank) and the sourced
`$35` penalty. The measured maximum stop-count delta across the stated
swing (multiplier 0.8 -> 1.5, `EIA_SWING_STATED`) is **0 stops**, on
every one of the twelve corridors -- station selection is byte-identical
at every rung on every corridor; only the reported dollar cost scales
with the multiplier. This is the actual measured figure, not a
projection.

**The verdict (D-25).** `EIA_SWING_VERDICT_MAX_STOP_DELTA = 1`
(`routing/tests/test_eia_penalty_sweep.py`) was fixed, with both
branches of the verdict rule written into its own comment, in a commit
that strictly precedes the commit that ran this measurement. The
measured maximum delta (0) is at or under that threshold, so the
verdict is **RATIFIED: the coupling is correct-as-designed** -- the
fixed `$35` penalty trades off appropriately against a price that moves
with the EIA regional index at the swing this phase tested, and no
further change (such as scaling the penalty by the EIA factor) is
warranted by this evidence.

The full twelve-corridor x four-multiplier table lives in the
`measure_eia_penalty_sweep` management command's output and in
`18-07-SUMMARY.md`; `EiaPenaltyCouplingGuardTests`
(`routing/tests/test_eia_penalty_sweep.py`) is this finding's
CI-enforcing guard.
"""
import bisect
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from routing.services.exceptions import DeadlineExceededError, InfeasibleRouteError
from routing.services.solver import (
    FuelPlan,
    FuelStop,
    PurchaseReason,
    is_estimate_priced,
    trust_margin_for,
)

# D-12's tolerance band for the DP's own relaxation and winner-selection
# comparisons: two objectives within this band are treated as tied and
# the comparison falls through to the next tie-break key (fewest stops,
# then station positions, then opis_id) rather than one winning purely on
# Decimal summation-order noise. Never used as comparison slop beyond
# that -- byte-identical to the fixed-charge oracle's own COST_TOLERANCE
# (`test_solver_fixed_charge_optimality.py:96`).
COST_TOLERANCE = Decimal("0.0001")


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _exact_sub(a, b):
    """Exact `a - b` for two position-domain `Decimal`s, bypassing
    whatever ambient context precision (`decimal.getcontext().prec`, 28
    significant digits by default) happens to be active when this runs.

    Built from each operand's own `Decimal.as_tuple()` triple and exact
    integer subtraction -- the same "no `Decimal` division/`scaleb`, no
    context rounding" discipline `_to_ticks`/`_from_ticks` already use
    elsewhere in this module for exactly the same reason -- rather than
    Python's own `Decimal.__sub__`, which SILENTLY ROUNDS its result to
    the ambient context's precision whenever the true difference needs
    more significant digits than that to represent exactly.

    This matters because this dataset's real, Mapbox/shapely-derived
    station positions routinely carry up to 28-29 significant digits
    themselves (e.g. `374.5423307176653094063943466`, a real committed
    corridor fixture position -- 28 digits, exactly at the default
    context's own precision ceiling). Subtracting two such positions
    under the plain `Decimal.__sub__` operator can and does lose the
    least-significant digit of the true result. `useful_fill_levels_mi`
    is the one caller that needs this: its "exact reach" fill levels must
    land exactly on the node they were built to reach once converted to
    the exact-integer tick domain (`_to_ticks`, below) -- a level that is
    silently off by even one ULP in that domain can push a target's
    position just outside `_reachable_ticks`'s strict `bisect_right`
    boundary, making the DP believe an otherwise-reachable node is not
    reachable via that level at all. The pre-optimization, all-`Decimal`
    recurrence never surfaced this: it re-derived a target's own gap via
    the SAME rounded expression twice (once to build the level, once to
    compute arrival fuel from it), so the identical rounding error
    cancelled out algebraically every time, and its reachability test
    (`pos + level <= target_pos`) similarly happened to round its own
    compensating addition back up to the exact target position. The
    exact-integer tick domain has no such rounding step left to cancel
    against -- `full_ticks[target_index] - pos_ticks` is computed exactly
    from the RAW, unrounded input positions, so a `level` that was itself
    silently rounded during construction no longer agrees with it.
    """
    a_sign, a_digits, a_exponent = a.as_tuple()
    b_sign, b_digits, b_exponent = b.as_tuple()
    exponent = min(a_exponent, b_exponent)

    def _coefficient(sign, digits, digit_exponent):
        value = 0
        for digit in digits:
            value = value * 10 + digit
        if sign:
            value = -value
        return value * (10 ** (digit_exponent - exponent))

    diff = _coefficient(a_sign, a_digits, a_exponent) - _coefficient(
        b_sign, b_digits, b_exponent
    )
    sign = 1 if diff < 0 else 0
    magnitude = -diff if sign else diff
    digit_str = str(magnitude) if magnitude else "0"
    return Decimal((sign, tuple(int(d) for d in digit_str), exponent))


def preflight_gap_check(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Raise `InfeasibleRouteError` if any consecutive gap between
    along-route nodes (START, each candidate in position order, FINISH)
    exceeds the usable range at the node the gap starts from. Return
    `None` when every gap fits.

    Price-independent by design: reads only `distance_from_start_mi` and
    `name` from each candidate, never `price_per_gallon` -- this check
    answers a pure reachability question, not a cost one.

    START is a non-purchasable node: the usable range there is the fuel
    actually on board (`starting_fuel * tank_range_mi`), never the tank's
    full capacity -- the **START asymmetry** (D-17, `solver.py:173`). At
    every real station the tank can be topped off, so the usable range
    there is `tank_range_mi`.

    Never passes `leg_index` or `leg_coords` to the raised exception --
    `views.py::_enrich_infeasible_leg` is their sole setter, outside this
    AST-import-gated module.
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    starting_fuel = _as_decimal(starting_fuel)

    ordered = sorted(candidates, key=lambda c: (c.distance_from_start_mi, c.opis_id))

    pos = Decimal(0)
    current_name = "START"
    usable_range = starting_fuel * tank_range_mi

    for candidate in ordered:
        gap = candidate.distance_from_start_mi - pos
        if gap > usable_range:
            raise InfeasibleRouteError(
                from_station=current_name,
                to_station=candidate.name,
                gap_mi=gap,
                max_range_mi=usable_range,
            )
        pos = candidate.distance_from_start_mi
        current_name = candidate.name
        usable_range = tank_range_mi

    gap = total_route_mi - pos
    if gap > usable_range:
        raise InfeasibleRouteError(
            from_station=current_name,
            to_station="FINISH",
            gap_mi=gap,
            max_range_mi=usable_range,
        )


# Recalibrated in 18-05c against CORRECTED solve_fixed_charge timings.
# The prior value (250,000, from 18-04c-SUMMARY.md) was calibrated against
# a DP that still had the 224b0ee precision bug: `useful_fill_levels_mi`'s
# non-precision-safe exact-reach subtraction was silently discarding
# reachable states, which is exactly why it measured artificially fast
# (miami_fl-boston_ma @500mi calibrated at 3.087s). Once 224b0ee fixed
# that, every exact-DP cell got materially slower -- the same corridor now
# measures 6.25s-13.01s across three runs -- so 250,000 no longer honours
# the <=5s DP-path design target and had to be re-fit against real timings,
# not merely re-asserted.
#
# Re-measured `estimate_transition_count` over the real, pruned search set
# for the same twelve corridor/tank-range cells (mpg=10,
# starting_fuel=0.5, penalty=35, price basis "neutral", single
# uncontended dev-machine process), each `solve_fixed_charge` run 3x
# (min/median/max reported -- run-to-run variance is large at this
# corridor density, so the WORST of three is what this budget is fit
# against, never the mean):
#
#   corridor                       tank   estimate     worst (of 3 runs)
#   houston_tx-chicago_il          1050          23     0.001s
#   phoenix_az-minneapolis_mn      1050       4,809     0.053s
#   dallas_tx-seattle_wa            500      61,944     2.706s
#   san_diego_ca-jacksonville_fl    500      66,571     2.542s
#   dallas_tx-seattle_wa           1050     117,895     2.181s
#   atlanta_ga-denver_co            500     150,905     8.546s  (excluded)
#   miami_fl-boston_ma               500     182,506    13.008s  (excluded)
#
# (jacksonville_fl-bangor_me @500mi, both toronto_oh-hillsboro_or cells,
# and both el_paso_tx-portland_me cells all sit at estimate >= 356,085 --
# already 2-16x further above this cluster's own top than any plausible
# <=5s threshold could reach, so raw solve_fixed_charge was not re-run on
# them; they stay on the heuristic path exactly as before, confirmed by
# `solve()`-dispatch measurement in 18-05c-SUMMARY.md's own 12-cell table.)
#
# The five retained cells now separate cleanly from the two demoted ones:
# every retained cell's worst-of-three measurement is under 5s
# (dallas_tx-seattle_wa @500mi -- the retained cell closest to the 5s
# ceiling -- worst 2.706s, still 2.294s/46% of margin under it), while
# both demoted cells' worst-of-three measurements (8.546s, 13.008s)
# exceed the ceiling outright.
# 134,000 sits in the resulting 117,895-150,905 estimate gap -- roughly
# the arithmetic midpoint, ~13.7% above the highest retained estimate
# (dallas_tx-seattle_wa @1050mi, 117,895) and ~11.2% below the lowest
# demoted estimate (atlanta_ga-denver_co @500mi, 150,905) -- rather than
# sitting flush against either boundary.
#
# dallas_tx-seattle_wa (the demo corridor, ROADMAP criterion 1) stays on
# exact_dp at both tank ranges under this threshold: @500mi worst 2.706s,
# @1050mi worst 2.181s, both comfortably under the 5s target.
#
# atlanta_ga-denver_co @500mi and miami_fl-boston_ma @500mi now dispatch
# to the penalty-aware heuristic instead of the exact DP -- both were
# exact_dp under the prior (invalid) 250,000 calibration; their re-measured
# worst-case DP time no longer supports that. See 18-05c-SUMMARY.md for
# the full calibration table and the re-derived heuristic quality figures
# on the cells this demotes.
# ---------------------------------------------------------------------------
# HOTFIX 2026-08-02 -- 134,000 was calibrated on developer-workstation
# timings against a 5s target and is INVALID on the deployed hardware.
#
# Everything above this line records the 134,000 derivation and is kept as
# the historical record of how the miscalibration happened. Its own closing
# paragraphs are the disproof: they single out dallas_tx-seattle_wa (the
# demo corridor, ROADMAP criterion 1) as staying on exact_dp "comfortably
# under the 5s target" at a measured @500mi worst of 2.706s. On the live
# Render free tier that exact request -- POST /api/route, Dallas -> Seattle,
# API default vehicle (10 mpg / 500 mi) -- exceeds GUNICORN_TIMEOUT=30 and
# returns HTTP 500. Reproduced 5/5 at 30.5s-35.7s. That is a >11x
# workstation-to-live factor on this cell, not the ~5.4x the Dallas -> LA
# live capture suggested (solver 8.399s live vs 1.556s workstation).
#
# The error was calibrating a production dispatch guard against the wrong
# machine class. A transition-count estimate is hardware-independent; the
# wall-clock time it buys is not.
#
# 50,000 is chosen so the dispatch boundary tracks MEASURED breach
# behaviour rather than a workstation time target. Estimates at the API
# default vehicle (10 mpg / 500 mi tank) over the twelve committed
# corridors:
#
#   san_diego_ca-jacksonville_fl     66,564  -> heuristic (was exact_dp)
#   dallas_tx-seattle_wa             61,912  -> heuristic (was exact_dp)
#   ----------------------------------------- 50,000 boundary
#   houston_tx-chicago_il            48,912  -> exact_dp
#   fargo_nd-amarillo_tx             41,817  -> exact_dp
#   phoenix_az-minneapolis_mn        16,312  -> exact_dp
#   nashville_tn-buffalo_ny           8,141  -> exact_dp
#   sacramento_ca-salt_lake_city_ut     120  -> exact_dp
#
# The two cells this demotes are exactly the two plan 18-06 measured as
# breaching LATENCY_CEILING_SECONDS=1.0s on the workstation
# (dallas_tx-seattle_wa 1.5560s-1.7244s; san_diego_ca-jacksonville_fl
# 1.3446s-1.6977s). Every cell that stays on exact_dp was measured
# comfortably inside that ceiling. The boundary now separates
# measured-to-breach from measured-fine, instead of separating two
# workstation time estimates.
#
# COST, stated plainly: these two corridors -- including the ROADMAP's own
# demo corridor -- no longer receive a provably optimal plan. They receive
# the penalty-aware heuristic, measured at 6.5% average / 12.5% max off the
# exact optimum (18-04d). The trade is visible to callers rather than
# hidden: the response's `solver_strategy` field reports
# `penalty_aware_heuristic`.
#
# This is a HOTFIX to stop a live 500, NOT a principled re-calibration. The
# real fix measures the dispatch boundary against deployed hardware and
# guards it with a test that would have caught this. That is gap-closure
# work, tracked in 18-VERIFICATION.md.
#
# CONFIRMED, 2026-08-04 (plan 18-12, gap-closure): evaluated against 18-10's
# predictor verdict and 18-11's deployed-hardware measurement -- see the
# module docstring's "Why the transition-count estimate was not replaced"
# section and the DISPATCH_RETENTION_FLOOR block below for the full
# derivation. No replacement predictor qualifies, and the deployed evidence
# proves the identical inversion this comment already describes, this time
# with LIVE figures on both sides of the comparison. This value (50,000) is
# CONFIRMED, not superseded: no threshold value could ever have restored
# dallas_tx-seattle_wa@1050mi to exact_dp while also demoting
# dallas_tx-seattle_wa@500mi, so there was nothing a different number here
# could have fixed.
#
# MEASURED, NOT SHIPPED -- 2026-08-04 (plan 18.1-08). `adopt_budget_rung()`
# (`routing.tests.test_dispatch_recovery`, a rule committed in plan
# 18.1-01, BEFORE any of the measurement below existed) was applied to
# GENUINE evidence and returned 130,000, above this baseline. That
# measurement is recorded below in full, verbatim, exactly as taken --
# nothing here is hidden or softened. It was NOT wired into this constant.
#
# Method: every cell newly admitted at each `DP_TRANSITION_BUDGET_LADDER`
# rung was measured with a GENUINE DP attempt -- `dp.solve_fixed_charge`
# called DIRECTLY at `deadline=DP_DEADLINE_SECONDS` (2.8, confirmed
# above), bypassing `solve()`'s own budget gate, which would otherwise
# demote an over-budget cell to the heuristic before ever attempting the
# DP and produce exactly the measurement-artifact plan 18.1-07's own
# SUMMARY warns against ("adopted rung: None" was that artifact, not
# evidence). 3 repeats per cell, worst response kept, one-off script, not
# a permanent test:
#
#   rung=70,000 (newly admitted vs. 50,000):
#     dallas_tx-seattle_wa@500mi       est=61,944  worst=3.000s BREACHED
#     san_diego_ca-jacksonville_fl@500mi est=66,571 worst=3.046s completed
#     -> all_within_bar (<=15s): yes. at_least_one_completed: yes. QUALIFIES.
#
#   rung=130,000 (newly admitted vs. 70,000):
#     dallas_tx-seattle_wa@1050mi      est=117,895 worst=2.750s completed
#     -> all_within_bar: yes. at_least_one_completed: yes. QUALIFIES.
#
#   rung=200,000 (newly admitted vs. 130,000):
#     atlanta_ga-denver_co@500mi       est=150,905 worst=3.032s BREACHED (3/3)
#     miami_fl-boston_ma@500mi         est=182,506 worst=3.234s BREACHED (3/3)
#     -> all_within_bar: yes. at_least_one_completed: NO (every repeat on
#        both cells breached). FAILS -- the walk stops here per
#        `adopt_budget_rung`'s own rule (a later rung can never be
#        adopted once an earlier one fails to qualify), so rung=400,000
#        (jacksonville_fl-bangor_me@500mi est=356,085 worst=2.954s
#        BREACHED 3/3; demo_la_ca-new_york_ny@1050mi est=222,214
#        worst=3.156s BREACHED 3/3 -- also measured, also would have
#        failed) and rung=None were never reached by the walk.
#
# The rule's own answer: 130,000. Why it was NOT shipped, discovered only
# AFTER running the full downstream regression suite (not assumed, not
# guessed at): wiring 130,000 into this constant admits
# dallas_tx-seattle_wa@500mi (estimate 61,944, band 50,000-70,000) as a
# direct, unavoidable side effect of reaching 130,000 -- `adopt_budget_rung`
# walks the ladder in order and a later rung can never be adopted without
# every earlier rung already qualifying. On THIS workstation,
# dallas_tx-seattle_wa@500mi's own genuine DP completion time (~2.7-3.1s)
# straddles the 2.8s deadline closely enough that it BREACHED in 2 of 3
# repeats above but COMPLETED (exact_dp) in the third, and in independent
# single-shot re-measurements taken minutes apart. That specific cell,
# at that specific tank range, is the exact one
# `test_solver_dispatch.DispatchDemotionGuardTests` exists to keep off
# `exact_dp` permanently -- it reproduced HTTP 500 5/5 at 30.5-35.7s live,
# pre-hotfix, and that guard's own assertion is written as an unconditional
# "must never dispatch to exact_dp again," not a conditional one. Wiring
# 130,000 in made that guard (and a same-effect Phase 18 stop-count guard
# in `routing/tests/test_plan_objective.py`, a file this plan's own scope
# does not include and whose own module comment states its pinned range is
# "NOT a bound to widen") fail on this exact machine, non-deterministically,
# depending on timing jitter around the 2.8s boundary.
#
# This is NOT the identical NOT TRACTABLE finding restated: that proof is
# about a single estimate THRESHOLD with no clock behind it, and stands
# unchanged (see the CONFIRMED paragraph above). This is a NEW, narrower
# finding, specific to this plan's own scope: the deadline backstop makes
# dallas_tx-seattle_wa@500mi's outcome legitimately non-deterministic on
# fast hardware once admitted, and reconciling the two pre-existing guards
# built around the OLD deterministic assumption requires touching either an
# out-of-scope, differently-phased, explicitly protected test file, or
# substantially rewriting an in-scope guard whose entire purpose is
# resisting exactly this change -- neither of which this plan's own
# `<measurement_integrity>` no-tuning-toward-dallas_tx-seattle_wa clause
# licenses unilaterally. `DP_TRANSITION_BUDGET` stays 50,000. The rule's
# own 130,000 answer is preserved here, verbatim, for the plan that takes
# up this reconciliation deliberately.
# ---------------------------------------------------------------------------
DP_TRANSITION_BUDGET = 50_000

# ---------------------------------------------------------------------------
# DISPATCH_RETENTION_FLOOR -- pinned 2026-08-04 (plan 18-12), BEFORE
# evaluating the concrete deployed-hardware figures below against it. See
# the module docstring's "Why the transition-count estimate was not
# replaced" section for the full derivation this block only summarises.
#
# Plan 18-10 measured a closed, seven-member family of candidate dispatch
# predictors (estimate_transition_count included) against a pinned
# zero-discordant-pair rule and found the QUALIFYING SHORTLIST EMPTY -- no
# member, incumbent included, can be proven free of the structural
# inversion this module's own HOTFIX comment above already documents. Per
# 18-10's own handoff ("it cannot get there by swapping in a better scalar
# predictor over this cell matrix -- that path is now closed by measurement,
# not by assumption"), this plan does not introduce a replacement
# predictor: estimate_transition_count remains the dispatch input.
#
# DISPATCH_RETENTION_FLOOR is the anti-vacuity condition: a boundary that
# demotes every cell trivially satisfies "nothing breaches". Pinned here at
# the full set of cells with genuine DEPLOYED-hardware evidence of
# exact_dp running comfortably inside GUNICORN_TIMEOUT=30:
#
#   1. sacramento_ca-salt_lake_city_ut (estimate 120): exact_dp, live,
#      5.0-5.3ms (plan 18-11-SUMMARY.md).
#   2. dallas_tx-seattle_wa @1050mi (estimate 117,852): exact_dp, live,
#      HTTP 200 in ~12s, pre-hotfix -- budget was 134,000 at the time, so
#      this cell genuinely ran exact_dp in production (18-10-PLAN.md,
#      18-10-SUMMARY.md and 18-11-SUMMARY.md all cite this figure).
#
# DISPATCH_RETENTION_FLOOR = 2 -- BOTH cells, not a padded-down subset of
# one. dallas_tx-seattle_wa is ROADMAP criterion 1's own worked example;
# admitting only the far-from-boundary sacramento cell into the required
# set would let a policy "pass" without ever being tested against the one
# cell that actually matters. This is deliberately NOT 18-10's own
# workstation-derived floor (5, routing/tests/test_dispatch_predictor.py)
# reused verbatim -- that floor was built from seven WORKSTATION-comfortable
# figures, and this plan's own must-haves require the floor to be pinned
# against cells measured comfortably LIVE-fine, of which only these two
# exist in this gap-closure's evidence base as of 2026-08-04.
#
# Both outcome branches, pinned before the numbers below are compared
# against any threshold:
#
#   * ADOPT -- a single DP_TRANSITION_BUDGET value exists that is (a)
#     strictly below every estimate measured or known to breach
#     GUNICORN_TIMEOUT=30 live, AND (b) at or above the estimate of every
#     cell in the DISPATCH_RETENTION_FLOOR set above. Wire it.
#   * NOT TRACTABLE -- no such value exists. The floor is not lowered, the
#     budget is not widened past the known-breaching estimate, and the
#     honest finding is recorded: the exact DP is not tractable for the
#     affected corridor on this hardware under this estimator, the
#     conservative boundary stands, and the product claims are corrected in
#     18-14. This is a legitimate result, not a failure.
#
# THE VERDICT: the one known-live-breaching cell, dallas_tx-seattle_wa
# @500mi, estimates 61,912 -- SMALLER than dallas_tx-seattle_wa @1050mi's
# 117,852, which is IN the retention-floor set and live-fine. ADOPT would
# require a budget strictly below 61,912 (to demote the breaching cell) AND
# at or above 117,852 (to retain the floor set in full) SIMULTANEOUSLY.
# 117,852 > 61,912, so no such budget exists: this is the identical
# inversion 18-10 proved on workstation timings, reproduced here with
# DEPLOYED-hardware figures on both sides of the comparison. NOT TRACTABLE
# applies. DP_TRANSITION_BUDGET is not moved.
# ---------------------------------------------------------------------------
# AMENDED 2026-08-09 (Phase 22 plan 22-15): the block above correctly
# described 2026-08-04's state. It is no longer current. Phase 22's Overture
# gap-fill import (plan 22-12) widened sacramento_ca-salt_lake_city_ut's
# candidate density enough that its transition-count estimate rose from
# ~120 to over 10 million (plan 22-14's 22-DISPATCH-DIFF.md), crossing the
# 50,000 budget and demoting it to penalty_aware_heuristic. The two-cell
# DISPATCH_RETENTION_FLOOR set this constant enforces therefore now holds
# for ZERO of its two required cells at the shipped budget -- down from one
# (dallas_tx-seattle_wa@1050mi was already not admitted at 50,000 as of
# Phase 18.1; the block above's "exact_dp, live" citation for it describes
# only the pre-hotfix 134,000 budget). The block above is left exactly as
# written, describing what was true when it was pinned; this note corrects
# the present-tense reading, not the historical record.
# ---------------------------------------------------------------------------
DISPATCH_RETENTION_FLOOR = 2

# ---------------------------------------------------------------------------
# DP_DEADLINE_SECONDS -- pinned 2026-08-04 (plan 18.1-04) as a provisional
# value, derived by
# routing.tests.test_dispatch_recovery.derive_dp_deadline_seconds from the
# single upstream figure in evidence at that time (18-11's own full
# stage-breakdown capture, corridor + route + eia + cache + index ~=
# 5.44s).
#
# Full arithmetic, term by term (RESPONSE_BAR_SECONDS = 15s total cold
# cache-miss bar, minus the 5.44s measured upstream, minus a 0.5s
# DEADLINE_OVERSHOOT_BUDGET_SECONDS, minus a 0.5s
# HEURISTIC_FALLBACK_ALLOWANCE_SECONDS, divided by a
# ROUTE_ALTERNATIVES_FANOUT of 3, quantized DOWN to one decimal place):
#
#   (15 - 5.44 - 0.5 - 0.5) / 3 = 8.56 / 3 = 2.8533... -> 2.8
#
# Why the fan-out divisor exists (the least obvious term; not in
# 18.1-CONTEXT.md or 18.1-RESEARCH.md): `mapbox.get_routes` requests
# `alternatives=true`; `routing.views._solve_all_alternatives` calls
# `solver.solve()` once PER returned alternative, all inside a single
# `self._timing.stage("solver")` block; and `routing.timing.ServerTiming`
# ACCUMULATES a repeated stage name -- so within one request the per-solve
# deadline can be paid up to ROUTE_ALTERNATIVES_FANOUT (3) times. A
# deadline derived against the whole 15s bar without dividing would blow
# the bar on any request whose alternatives all breach.
#
# Shape considered and NOT taken: one cumulative per-request budget shared
# across alternatives. Rejected because D-02 pins a fixed per-solve
# constant, and a shared budget would make a later alternative's outcome
# depend on an earlier alternative's runtime.
#
# D-09's secondary check, computed here: the same worst-case arithmetic
# against GUNICORN_TIMEOUT_SECONDS=30 with a 5s margin --
#
#   5.44 + 3 * (2.8 + 0.5) + 0.5 = 15.84s <= 30 - 5 = 25s
#
# -- clears with room to spare, confirming the worker timeout is NOT the
# binding constraint; the 15s product bar is.
#
# D-10's own record, preserved here because CONTEXT.md requires it to
# survive into implementation comments: the only cell with a known live
# `exact_dp` stress timing is dallas_tx-seattle_wa@1050mi at roughly 12
# seconds pre-hotfix. A bar near 20 seconds would recover it; this 15-second
# bar will not. 15 was chosen on UX grounds with that fact already known --
# a deliberate refusal to tune toward the one corridor ROADMAP criterion 3
# forbids tuning toward. This number must NOT be revisited in order to
# capture that cell.
#
# CONFIRMED, 2026-08-04 (plan 18.1-08), NOT superseded -- re-derivation
# attempted per this comment's own pinned outcome branches. No fresh
# upstream `corridor + route + eia + cache + index` Server-Timing figure
# could be obtained without issuing an UNPLANNED live request: neither
# `.github/workflows/smoke.yml` nor `.github/workflows/keep-warm.yml`
# capture response headers (both discard the body with `-o /dev/null` or
# a scratch file and record only `%{http_code} %{time_total}`, never
# `Server-Timing`), so no existing CI artifact carries the figure this
# derivation needs. Issuing one here would duplicate plan 18.1-09's own
# dedicated live spot-check protocol (wake-then-measure, cache-bust
# ladder, worst-of-repeats) with an ad hoc, unprotocoled substitute --
# explicitly the wrong tool for this job. Per this comment's own pinned
# instruction, the recorded 5.44s figure is RE-APPLIED rather than
# presented as fresh; this is recorded here as a LIMITATION, not hidden.
# Re-applying the identical upstream figure through the identical pinned
# function necessarily reproduces the identical result -- trivially
# "the same value", which is this comment's own CONFIRMED branch:
#
#   (15 - 5.44 - 0.5 - 0.5) / 3 = 8.56 / 3 = 2.8533... -> 2.8  (unchanged)
#
# D-09's secondary check, re-verified against the same re-applied figure
# (unchanged from above, restated here because plan 18.1-08's own task
# requires it be recorded explicitly at this re-derivation, not merely
# cross-referenced):
#
#   5.44 + 3 * (2.8 + 0.5) + 0.5 = 15.84s <= 30 - 5 = 25s
#
# -- clears with room to spare; the 15s product bar remains the binding
# constraint, not the worker timeout. This literal (2.8) stands, CONFIRMED
# not superseded, mirroring `DP_TRANSITION_BUDGET`'s own CONFIRMED-not-
# superseded precedent above.
# ---------------------------------------------------------------------------
DP_DEADLINE_SECONDS = Decimal("2.8")

# ---------------------------------------------------------------------------
# _DEADLINE_CHECK_STRIDE -- pinned 2026-08-04 (plan 18.1-04) at
# `routing.tests.test_dispatch_recovery.DEADLINE_CHECK_STRIDE_LADDER`'s
# smallest rung (500, most conservative: most frequent clock reads,
# smallest overshoot, highest per-call cost) as a provisional placeholder,
# BEFORE the overshoot it bounds was ever measured (D-06).
#
# Grounding figures at plan 18.1-04 time (18.1-RESEARCH.md, measured live
# in that session): roughly 45 nanoseconds per monotonic-clock read;
# roughly 77.7 microseconds per `_reachable_ticks`-call-granularity check
# on atlanta_ga-denver_co@500mi, and roughly 99.2 microseconds on
# jacksonville_fl-bangor_me@500mi. Both are MODERATE-density cells, not
# the densest a raised budget rung might admit -- Pitfall 3
# (18.1-RESEARCH.md) required re-measuring on the densest ADMITTED cell
# before a rung could be adopted.
#
# ADOPTED, 2026-08-04 (plan 18.1-08), by
# `routing.tests.test_dispatch_recovery.adopt_stride_rung()` applied
# exactly as pinned in plan 18.1-01, BEFORE this measurement existed.
#
# Densest cell measured: jacksonville_fl-bangor_me@500mi (estimate
# 356,085) -- the densest cell any FINITE DP_TRANSITION_BUDGET_LADDER rung
# admits (adopted at rung 400,000; see that ladder's own comment above
# `DispatchAdmissionManifestTests`' import site), deliberately NOT
# toronto_oh-hillsboro_or (no sane rung ever admits it) and NOT a
# moderate-density cell (the grounding figures above are insufficient by
# Pitfall 3's own account).
#
# Method: `dp.solve_fixed_charge` called DIRECTLY (bypassing `solve()`'s
# budget gate, which would otherwise demote this cell to the heuristic
# before ever attempting the DP -- the exact measurement-artifact trap
# plan 18.1-07's own SUMMARY warns against) with `deadline=1` (well under
# this cell's genuine ~19.327s untimed solve time, RESEARCH.md's own D-06
# grounding data), 3 repeats per rung, worst overshoot kept
# (`elapsed_seconds - deadline`, from the raised
# `DeadlineExceededError`), one-off script, not a permanent test:
#
#   stride=500:   worst_overshoot_seconds=0.000
#   stride=1,000: worst_overshoot_seconds=0.031
#   stride=2,000: worst_overshoot_seconds=0.047
#   stride=5,000: worst_overshoot_seconds=0.359
#
# (Workstation caveat: the four values above cluster near multiples of
# ~15.6ms -- Windows' default ~64Hz timer-resolution quantum -- rather
# than forming a smooth line; the ORDER and rough SCALE still track
# `_DEADLINE_CHECK_STRIDE` as expected, but the exact figures are a
# property of this workstation's clock, not a portable constant, same as
# every other "measured live in that session" figure elsewhere in this
# module.)
#
# Every rung's worst overshoot sits inside
# `DEADLINE_OVERSHOOT_BUDGET_SECONDS` (0.5s), so `adopt_stride_rung` walks
# the full ladder without breaking and adopts its LARGEST rung, 5,000 --
# not a degenerate result: it is the genuine outcome of measuring on the
# actual densest admitted cell rather than assuming the moderate-cell
# figures above extrapolate safely.
#
# The deadline's REAL guarantee, stated as the number the response budget
# (`RESPONSE_BAR_SECONDS`, `DEADLINE_OVERSHOOT_BUDGET_SECONDS` itself) is
# actually derived against: `DP_DEADLINE_SECONDS + 0.359s` worst-observed
# overshoot at this stride, on this cell -- not `DP_DEADLINE_SECONDS`
# alone.
# ---------------------------------------------------------------------------
_DEADLINE_CHECK_STRIDE = 5_000


def estimate_transition_count(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Deterministic, `O(n log n)` structural upper bound on the number of
    `(state, level, target)` transitions `solve_fixed_charge`'s inner loop
    would need to consider for this exact input -- see the module
    docstring's "Pre-flight transition-count estimate and the
    penalty-aware fallback" section for the full derivation and its
    calibration.

    Pure function of its arguments: sorts `candidates` by the same total
    order `solve_fixed_charge` uses, converts every position-domain value
    to the same kind of exact integer tick `solve_fixed_charge` itself
    computes (via `Decimal.as_tuple()`, never division or `scaleb`), and
    sums, over START plus every candidate, `reach * (reach + 1)` -- where
    `reach` is the count of remaining candidates-plus-FINISH within one
    tank of that node's position (found by `bisect.bisect_right` over the
    same sorted tick array, exactly as `solve_fixed_charge`'s own
    `_reachable_ticks` does). `reach + 1` upper-bounds the number of
    distinct useful fill levels at that node (the finite-fill lemma
    above: at most one level per remaining reachable candidate/FINISH,
    plus the fill-to-capacity level); each such level can reach at most
    `reach` downstream targets. START contributes only `reach` (never
    `reach * (reach + 1)`): it is non-purchasable, so it has exactly one
    fixed departure level, never a choice among several.

    `candidates` should already be the search set that would be handed to
    `solve_fixed_charge` (the pruned set, when pruning is in effect) --
    this function does not prune anything itself, and never raises: it is
    called before feasibility is known to matter for dispatch purposes,
    so it tolerates an empty or degenerate `candidates` list the same way
    `solve_fixed_charge` itself does.
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    starting_fuel = _as_decimal(starting_fuel)

    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )
    node_count = len(ordered)
    positions = [c.distance_from_start_mi for c in ordered]
    start_fuel = starting_fuel * tank_range_mi

    _exponent = min(
        value.as_tuple().exponent
        for value in (total_route_mi, tank_range_mi, start_fuel, *positions)
    )

    def _to_ticks(value):
        sign, digits, exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        if sign:
            coefficient = -coefficient
        shift = exponent - _exponent
        return coefficient * (10**shift) if shift else coefficient

    positions_ticks = [_to_ticks(p) for p in positions]
    total_route_ticks = _to_ticks(total_route_mi)
    tank_range_ticks = _to_ticks(tank_range_mi)
    start_fuel_ticks = _to_ticks(start_fuel)
    full_ticks = positions_ticks + [total_route_ticks]
    hi_bound = node_count + 1

    def _reach_count(max_ticks, lo):
        hi = bisect.bisect_right(full_ticks, max_ticks, lo, hi_bound)
        return hi - lo

    total = _reach_count(start_fuel_ticks, 0)
    for i in range(node_count):
        reach = _reach_count(positions_ticks[i] + tank_range_ticks, i + 1)
        total += reach * (reach + 1)

    return total


def useful_fill_levels_mi(
    position_mi, fuel_on_arrival_mi, *, tank_range_mi, nodes_ahead_mi
):
    """Return the strictly increasing, deduplicated tuple of useful
    post-purchase fuel levels (in miles of range) at a station positioned
    at `position_mi`, given the fuel already on board on arrival and the
    distances of every remaining candidate strictly ahead plus FINISH
    (`nodes_ahead_mi`) -- not one fixed subset's own remaining members.
    See the module docstring's "The finite-fill lemma" for the proof this
    set is exhaustive over optima.

    The returned levels are: the arrival level itself (buy nothing), each
    level that exactly reaches a node in `nodes_ahead_mi` that lies within
    `tank_range_mi` of `position_mi`, and `tank_range_mi` (fill the tank)
    -- clamped to `[fuel_on_arrival_mi, tank_range_mi]`. Levels below the
    arrival level are impossible (fuel cannot be sold) and are excluded.
    """
    levels = {fuel_on_arrival_mi, tank_range_mi}
    for node_mi in nodes_ahead_mi:
        # `_exact_sub`, not plain `Decimal.__sub__` -- see its own
        # docstring. Ambient-context rounding here would make this
        # "exact reach" level not actually exact once it is later
        # converted to `solve_fixed_charge`'s integer-tick domain.
        reach_mi = _exact_sub(node_mi, position_mi)
        if reach_mi <= tank_range_mi:
            levels.add(reach_mi)
    return tuple(
        sorted(
            level
            for level in levels
            if fuel_on_arrival_mi <= level <= tank_range_mi
        )
    )


@dataclass
class _EdgeInfo:
    """The rationale recorded on one relaxation edge, at the moment the
    purchase it represents was chosen -- never re-derived later by
    inspecting the finished plan (D-03). `price_source` is copied from the
    candidate at edge-construction time, exactly like every other field
    here, and is never re-looked-up from the finished plan either.

    The trailing pair narrows the same truly-bypassed set (see the
    relaxation loop below) to its estimate-priced members, populated only
    when the purchasing station itself is real-priced -- PROV-03's
    disclosure fact, mirroring the bypass pair above it exactly."""

    name: str
    opis_id: int
    price: Decimal
    pos: Decimal
    gallons: Decimal
    cost: Decimal
    reason: str
    reason_target_opis_id: int | None
    reason_target_name: str | None
    bypassed_cheaper_count: int
    bypassed_saving_forgone: Decimal | None
    price_source: str | None = None
    bypassed_estimate_count: int = 0
    bypassed_estimate_saving_forgone: Decimal | None = None


@dataclass
class _StateRecord:
    """One `(node, fuel)` DP state: the winning D-12 key that reached it,
    the predecessor `(node_index, fuel_level)` to continue the backward
    walk from, and the edge that produced it (`None` for a "buy nothing"
    pass-through or for the initial START state)."""

    key: tuple
    predecessor: tuple | None
    edge: "_EdgeInfo | None"


def _make_key_less(tol_num, tol_den):
    """Build the D-12 total-order comparator for one `solve_fixed_charge`
    call, closing over that call's own exact-integer tolerance threshold
    (`tol_num`/`tol_den`, positive integers with `tol_den > 0` -- see the
    module docstring's "Exact-integer money-domain comparison" section for
    their derivation). Returned as a factory rather than a single
    module-level function because `tol_num`/`tol_den` depend on that call's
    own `mpg`/`penalty`/tick scale, exactly the same reason `_to_ticks`,
    `relax`, and every other per-call-scaled helper in this module are
    nested inside `solve_fixed_charge` rather than defined once at module
    scope.

    `key_a[0]`/`key_b[0]` ("objective") are the exact integer described in
    that same docstring section -- proportional to, never equal to, the
    real dollar objective -- so this comparator never touches `Decimal` or
    `float`: `abs(diff) * tol_den > tol_num` is an exact cross-multiplied
    reproduction of `abs(real_objective_diff) > COST_TOLERANCE`, both
    sides positive integers, both operators exact. Ties within that band
    fall through to fewest stops, then the sorted tuple of chosen
    stations' positions, then the tuple of their `opis_id` -- unchanged
    from the prior `Decimal`-objective version, and byte-identical to the
    fixed-charge oracle's own tie-break (`optimal_fixed_charge_plan`,
    `test_solver_fixed_charge_optimality.py:391`)."""

    def _key_less(key_a, key_b):
        objective_a, stops_a, positions_a, opis_a = key_a
        objective_b, stops_b, positions_b, opis_b = key_b
        diff = objective_a - objective_b
        if abs(diff) * tol_den > tol_num:
            return objective_a < objective_b
        if stops_a != stops_b:
            return stops_a < stops_b
        if positions_a != positions_b:
            return positions_a < positions_b
        return opis_a < opis_b

    return _key_less


def _pareto_frontier(states_at_node, key_less):
    """Return the subset of `states_at_node` (a `{fuel_ticks:
    _StateRecord}` dict for one node) that is NOT Pareto-dominated by
    another entry in the same dict -- see the module docstring's "Pareto
    state-dominance pruning" section for the domination rule and its full
    soundness proof. `states_at_node` itself is never mutated; this
    returns a fresh dict (or the input unchanged when it holds at most one
    entry, or when nothing is dominated) containing only the surviving
    entries, in `states_at_node`'s OWN original iteration order -- never
    the descending-by-fuel order used internally to detect domination.
    `key_less` is the current call's `_make_key_less(...)` comparator --
    threaded in as a parameter, rather than closed over globally, since
    this function has no per-call state of its own to close over.

    Order preservation matters beyond style: this module's "Determinism"
    section documents that `dict` insertion/iteration order is itself
    part of this recurrence's contract, because it is what the existing
    first-writer-wins tie resolution inside `relax`/`_wins` (an
    intentionally arbitrary, below-D-12 tie-break for the rare case where
    two precursor paths reach the exact same downstream `(node, fuel)`
    key) implicitly depends on. Reordering surviving entries -- even
    without discarding any of them -- would silently change which of two
    such exactly-tied precursors wins a downstream slot, which is exactly
    as much an answer-visible change (a different, though equally
    optimal, per-stop gallon split) as discarding a state that genuinely
    mattered would be. Only STRICT domination discards an entry here
    (`key_less(dominant.key, record.key)`, never an equal-key match) --
    see the module docstring's Pareto section for why the tied-key case
    (same D-12 key, more fuel) is deliberately left unpruned rather than
    also discarded: pruning it cannot change the numeric optimum, but it
    can still perturb which of several already-tied plans a downstream
    coincidental tie resolves to, and this module's only mandate is
    speed, never a change -- however immaterial -- to which exact
    optimal plan comes out the other end.
    """
    if len(states_at_node) <= 1:
        return states_at_node
    dominated_ticks = set()
    best_key_so_far = None
    for fuel_ticks, record in sorted(
        states_at_node.items(), key=lambda item: item[0], reverse=True
    ):
        if best_key_so_far is not None and key_less(best_key_so_far, record.key):
            # A strictly better key already exists at >= fuel -- this
            # entry is genuinely dominated, never merely tied.
            dominated_ticks.add(fuel_ticks)
        elif best_key_so_far is None or key_less(record.key, best_key_so_far):
            # First entry seen, or a genuine, strict improvement over
            # every higher-or-equal-fuel entry so far.
            best_key_so_far = record.key
        # else: record.key ties best_key_so_far exactly -- survives,
        # left out of dominated_ticks, best_key_so_far left unchanged
        # (it already equals record.key in every D-12-relevant respect).
    if not dominated_ticks:
        return states_at_node
    return {
        fuel_ticks: record
        for fuel_ticks, record in states_at_node.items()
        if fuel_ticks not in dominated_ticks
    }


def solve_fixed_charge(
    candidates,
    *,
    total_route_mi,
    tank_range_mi,
    mpg,
    starting_fuel,
    penalty,
    trust_margin=Decimal(0),
    deadline=None,
) -> FuelPlan:
    """Return the `FuelPlan` minimising fuel dollars plus `penalty` times
    the count of stations bought at strictly more than zero, over a
    `(node, fuel)` state space with no hard stop cap (SOLV-01, SOLV-02).

    Assumes `preflight_gap_check` has already passed over this same
    `candidates`/`total_route_mi`/`tank_range_mi`/`starting_fuel` --
    the recurrence never raises (see "Totality" above) and is only total
    under that precondition.

    `candidates` is defensively re-sorted here by the total order
    `prune.py` already documents; DP nodes are the sorted list's ordinal
    positions plus a terminal FINISH node, never the raw
    `distance_from_start_mi` value, so co-located candidates always stay
    distinct nodes.

    From each `(node, fuel_on_arrival)` state, `useful_fill_levels_mi`
    enumerates every useful post-purchase fuel level. For each level, the
    vehicle travels to the farthest node (candidate or FINISH) reachable
    at that level -- any closer node is already reachable via its own,
    smaller, exact-reach level from the same state, so passing a closer
    node without stopping there needs no separate state (a "buy nothing"
    continuation from that node would be a no-op with identical cost and
    stop count). When more than one node shares that farthest position
    (a co-located pair), every one of them becomes a separate target
    state -- none is silently dropped.

    Every relaxation records, alongside its predecessor pointer, the
    `purchase_reason` for the purchase it represents (derived by the same
    price-ahead tests the greedy uses, so the reasons agree with the
    frozen greedy at `penalty=0`) plus `bypassed_cheaper_count` and
    `bypassed_saving_forgone` -- how many strictly-cheaper stations
    reachable within one full tank from this node were evaluated, sit
    strictly before THIS edge's own target, and were not routed through,
    and the fuel-dollar saving that gave up. A reachable-cheaper station
    that this edge's own target coincides with was not bypassed -- it was
    reached -- so it is excluded from that count (a full-tank fill's exact
    reach amount can coincide with a cheaper station's position, and
    landing there is `REACH_CHEAPER_STOP`, never a bypass of itself).
    `BYPASS_CHEAPER_NOT_WORTH_STOP` is the reason exactly when a full-tank
    fill flies past at least one such station (strictly before its own
    target) AND the flat per-stop `penalty` strictly outweighs the summed
    fuel-dollar saving those stations would have offered -- both
    conditions together, never bypass-count alone. Because that saving
    total is never negative and `penalty` is never negative, the
    penalty-outweighs-saving test can never hold at `penalty=0`, so this
    reason is structurally unreachable as a *winning* edge there -- the
    standard fuel-cost exchange argument always prefers buying only enough
    to reach a strictly cheaper station over overpaying at the current
    one, until a nonzero penalty makes the extra stop's cost outweigh the
    saving.

    Reconstruction walks predecessor pointers from the winning FINISH
    state back to START, emitting one `FuelStop` per strictly-positive
    purchase in increasing `distance_from_start_mi` order.
    `skipped_count`, `skipped_avg_price`, `price_percentile` and
    `corridor_avg_price` are left at their defaults -- those are computed
    over the full, unpruned candidate list by `solve()` itself (D-20),
    never by this module. `total_cost` never includes the penalty
    (INTG-02); `penalised_objective` is `total_cost + penalty * len(stops)
    + sum(margin_i for each purchased stop)` (PROV-03, D-06), computed
    fresh from the reconstructed stops rather than reused from internal DP
    bookkeeping.

    `trust_margin`: the flat per-candidate PROV-03 trust-margin rate (a
    `Decimal`), mirroring `penalty`'s own shape exactly and defaulting to
    `Decimal(0)` so every existing direct caller of this function --
    the fixed-charge oracle differentials, the frozen-greedy referee, and
    `measure_dispatch_predictor`'s spawned worker among them -- is
    unaffected by construction, exactly like `deadline` below. Read per
    candidate through `solver.trust_margin_for` (never a direct
    `price_source` comparison in this module -- see
    `PriceSourceUsagePurityTest`), it folds into the recurrence's integer
    comparison key at the same site the penalty numerator already folds
    in, over a single shared denominator with the penalty ratio (see the
    "Exact-integer money-domain comparison" section's trust-margin
    extension below). `total_cost` structurally cannot contain it -- it
    stays the DP's own accumulated `sum(cost_i)`, never a reconstruction
    from raw prices, which is exactly why the margin is a flat charge and
    not a per-gallon one (D-06).

    `deadline`: optional wall-clock budget in seconds (a `Decimal` or
    anything `Decimal(str(...))` can parse), same style as `solve()`'s own
    `prune` keyword. `None` means unlimited -- the default, precisely so
    every existing direct caller is unaffected by construction: the
    fixed-charge oracle differentials, the frozen-greedy referee, and
    `measure_dispatch_predictor`'s spawned worker all call this function
    without passing `deadline` and therefore never see a behaviour change.
    When a deadline is given, the check runs at a granularity of one clock
    read per `_DEADLINE_CHECK_STRIDE` `(state, level)` pairs examined, and
    raises `DeadlineExceededError` when the deadline is exceeded. The real
    guarantee this provides is `deadline` plus a measured overshoot, never
    `deadline` alone -- see `_DEADLINE_CHECK_STRIDE`'s own module-level
    comment for that overshoot's derivation.

    ## Implementation note: the fuel/position domain runs on exact integer
    ## ticks, never on `Decimal` arithmetic, in the O(states x levels x
    ## targets) inner loop

    Every quantity in the *position* domain -- station positions,
    `tank_range_mi`, `total_route_mi`, `starting_fuel * tank_range_mi`, and
    every fuel level `useful_fill_levels_mi` can ever return -- is a sum or
    difference of a small, fixed set of input `Decimal`s. This function
    picks one common exponent (`_pos_exponent`, the finest -- most
    negative -- exponent already present among those inputs) once, up
    front, and re-expresses every position-domain value at that exponent as
    a plain Python `int` ("ticks"): `_to_ticks`/`_from_ticks` convert via
    `Decimal.as_tuple()`'s exact `(sign, digits, exponent)` triple, never
    via `Decimal` division or `scaleb` (both of which are, in general,
    subject to context-precision rounding) -- so the conversion is an exact
    change of representation, not an approximation. Addition and
    subtraction are closed under a fixed exponent (the sum/difference of
    two values exactly representable at exponent E is itself exactly
    representable at exponent E), so every arrival-fuel level, every
    reachable-target position, and every DP state key computed by walking
    that arithmetic in `int` space is bit-for-bit the same value the
    original all-`Decimal` recurrence would have produced -- just computed
    without `Decimal`'s per-operation object overhead, and with the
    (already position-sorted) reachable-target scan replaced by
    `bisect.bisect_right` instead of a linear filter.

    The *money* domain -- the D-12 comparison itself -- runs on the same
    exact-integer discipline; see the module docstring's "Exact-integer
    money-domain comparison" section for the full derivation. `gallons`
    and `cost`, the `Decimal` purchase record a surviving `FuelStop`
    actually reports, are unaffected: they are still computed by the
    exact same `buy_mi = level - fuel_on_arrival; gallons = buy_mi / mpg;
    cost = gallons * price_per_gallon` sequence, in the exact same order,
    as before either optimization pass existed -- only *when* that
    sequence runs (lazily, at most once per surviving `(state, level)`,
    never for a transition that loses every target it reaches) has
    changed, not its arithmetic.
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    mpg = _as_decimal(mpg)
    starting_fuel = _as_decimal(starting_fuel)
    penalty = _as_decimal(penalty)
    trust_margin = _as_decimal(trust_margin)

    # D-05/D-06: a single absolute cutoff, computed once here at the
    # boundary -- never inside the loop below. `None` means unlimited, so
    # every existing direct caller (which never passes `deadline`) pays
    # nothing extra: `deadline_at` stays `None` and the strided check
    # below becomes a single cheap `is not None` test per stride interval.
    # The `Decimal` deadline is coerced to `float` exactly once here,
    # matching the monotonic clock's own return type.
    deadline_at = (
        None if deadline is None else time.monotonic() + float(deadline)
    )
    _transitions_examined = 0

    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )
    node_count = len(ordered)
    positions = [c.distance_from_start_mi for c in ordered]
    finish = node_count  # terminal node index, one past the last candidate

    def nodes_ahead_of(node_index):
        ahead = [(positions[j], j) for j in range(node_index + 1, node_count)]
        ahead.append((total_route_mi, finish))
        return ahead

    def reachable_targets(ahead, pos, level):
        """Every node in `ahead` within `level` miles of `pos`, in
        increasing-position order -- not merely the farthest one. A node
        strictly closer than the farthest reachable one is a genuinely
        distinct, non-dominated (node, fuel_on_arrival) state: it carries
        MORE leftover fuel than any smaller, exact-reach-to-that-node
        level would (which arrives with zero fuel to spare), at the cost
        of having paid for that extra range at the CURRENT station's
        price rather than a possibly cheaper later one. Collapsing to only
        the farthest node -- as an earlier version of this function did --
        silently discards that state, which can be strictly optimal (e.g.
        filling to capacity at the cheapest station on the route, then
        topping off only the small remainder at a pricier one, rather than
        buying that remainder's full distance at the pricier price).

        Retained for START's own one-time reachability computation below
        (never on the O(states x levels) hot path -- see `_reachable_ticks`
        for that)."""
        max_pos = pos + level
        return [entry for entry in ahead if entry[0] <= max_pos]

    # --- Exact integer-tick domain for positions/fuel levels (see the
    # "Implementation note" in this function's docstring). Chosen once,
    # covering every position-domain Decimal this run can ever produce:
    # candidate positions, tank_range_mi, total_route_mi, and
    # starting_fuel * tank_range_mi (the one position-domain multiplication
    # in this function -- every other position-domain value is a sum or
    # difference of these, so its own exponent is never finer than
    # min() of the four categories below).
    start_fuel = starting_fuel * tank_range_mi

    _pos_exponent = min(
        value.as_tuple().exponent
        for value in (total_route_mi, tank_range_mi, start_fuel, *positions)
    )

    def _to_ticks(value):
        sign, digits, exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        if sign:
            coefficient = -coefficient
        shift = exponent - _pos_exponent
        if shift < 0:
            # Unreachable given _pos_exponent is the min over every
            # position-domain value this function ever converts (see
            # docstring) -- surfaced loudly rather than silently truncated
            # if that invariant is ever broken by a future edit.
            raise AssertionError(
                f"position-domain value {value!r} needs more precision "
                f"than the derived tick scale (exponent {exponent} < "
                f"{_pos_exponent})"
            )
        return coefficient * (10**shift) if shift else coefficient

    def _from_ticks(ticks):
        sign = 1 if ticks < 0 else 0
        magnitude = -ticks if sign else ticks
        digit_str = str(magnitude) if magnitude else "0"
        return Decimal((sign, tuple(int(d) for d in digit_str), _pos_exponent))

    positions_ticks = [_to_ticks(p) for p in positions]
    total_route_ticks = _to_ticks(total_route_mi)
    start_fuel_ticks = _to_ticks(start_fuel)
    # full_ticks[i] is node i's position in ticks, for i in [0, node_count]
    # -- station nodes at their own index, FINISH (index node_count) at
    # total_route_ticks. Ascending by construction: positions is already
    # sorted, and `solver._validate` guarantees every candidate's
    # distance_from_start_mi <= total_route_mi, so total_route_ticks is
    # always >= the last station tick.
    full_ticks = positions_ticks + [total_route_ticks]

    # --- Exact-integer money-domain comparison (see the module
    # docstring's "Exact-integer money-domain comparison" section for the
    # full derivation this mirrors). `_price_exponent` is this call's own
    # analogue of `_pos_exponent` above: the finest exponent already
    # present among the finitely many `price_per_gallon` values this run
    # will ever compare, taken as a plain `min()` over already-materialized
    # `Decimal`s -- never discovered by running a worst-case pass or
    # assumed to be a fixed "cents" scale. `default=0` only matters when
    # `ordered` is empty, in which case no purchase edge is ever built and
    # `_price_exponent` is never read again.
    _price_exponent = min(
        (c.price_per_gallon.as_tuple().exponent for c in ordered), default=0
    )

    def _to_price_ticks(value):
        # Identical exact-conversion logic to `_to_ticks` above, at
        # `_price_exponent` instead of `_pos_exponent` -- kept as its own
        # small function (rather than parameterizing `_to_ticks`) so
        # neither call site pays an extra argument on its own hot path.
        sign, digits, exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        if sign:
            coefficient = -coefficient
        shift = exponent - _price_exponent
        if shift < 0:
            raise AssertionError(
                f"price-domain value {value!r} needs more precision than "
                f"the derived tick scale (exponent {exponent} < "
                f"{_price_exponent})"
            )
        return coefficient * (10**shift) if shift else coefficient

    # price_ticks[i] is ordered[i].price_per_gallon re-expressed as an
    # exact integer at _price_exponent -- parallel to positions/positions_ticks.
    price_ticks = [_to_price_ticks(c.price_per_gallon) for c in ordered]

    # Every dollar this recurrence ever sums is
    # `buy_ticks * 10**_pos_exponent * price_ticks * 10**_price_exponent /
    # mpg` -- i.e. `(buy_ticks * price_ticks) * MONEY_SCALE`, where
    # MONEY_SCALE = 10**(_pos_exponent + _price_exponent) / mpg is one
    # positive constant shared by every candidate this call will ever
    # compare (see the module docstring). Built via `Fraction` --
    # `Fraction(mpg)` is exact for any `Decimal` (routed through
    # `Decimal.as_integer_ratio()`, itself exact, never `float`) -- so
    # MONEY_SCALE is an exact rational, not a rounded one.
    _money_scale = Fraction(10) ** (_pos_exponent + _price_exponent) / Fraction(mpg)

    # penalty/MONEY_SCALE, reduced to lowest terms: the exact rational
    # weight one purchase's "+1 stop" contributes to the integer key, in
    # the same MONEY_SCALE-implied units as `buy_ticks * price_ticks`.
    _penalty_ratio = Fraction(penalty) / _money_scale
    P_NUM, P_DEN = _penalty_ratio.numerator, _penalty_ratio.denominator

    # D-01/D-06/D-16 (PROV-03): each candidate's flat trust-margin charge,
    # read through `solver.trust_margin_for` (never a direct price_source
    # comparison here -- see PriceSourceUsagePurityTest), converted to the
    # same MONEY_SCALE-implied ratio the penalty above already uses, then
    # folded over a SINGLE shared denominator with P_NUM/P_DEN -- never a
    # second, independent denominator (see the module docstring's
    # "Trust-margin extension" section for the full derivation and the
    # zero-margin byte-identity argument).
    _margin_ratios = [
        Fraction(trust_margin_for(c, trust_margin)) / _money_scale for c in ordered
    ]
    _shared_den = P_DEN
    for _ratio in _margin_ratios:
        _shared_den = math.lcm(_shared_den, _ratio.denominator)
    P_NUM = P_NUM * (_shared_den // P_DEN)
    P_DEN = _shared_den
    margin_numerators = [
        _ratio.numerator * (_shared_den // _ratio.denominator)
        for _ratio in _margin_ratios
    ]

    # COST_TOLERANCE/MONEY_SCALE, scaled by P_DEN and reduced: the exact
    # rational threshold `_make_key_less` cross-multiplies against, so its
    # `abs(diff) * tol_den > tol_num` reproduces
    # `abs(real_objective_diff) > COST_TOLERANCE` exactly -- see
    # `_make_key_less`'s own docstring.
    _tolerance_ratio = Fraction(COST_TOLERANCE) * P_DEN / _money_scale
    TOL_NUM, TOL_DEN = _tolerance_ratio.numerator, _tolerance_ratio.denominator

    key_less = _make_key_less(TOL_NUM, TOL_DEN)

    def _reachable_ticks(node_index, max_pos_ticks):
        """Every node index in (node_index, node_count] (stations, then
        FINISH) whose tick position is <= max_pos_ticks, as a `range` in
        increasing-position order -- the tick-domain, O(log n) equivalent
        of `reachable_targets` above (binary search over the already
        position-sorted `full_ticks`, replacing its O(n) linear scan)."""
        lo = node_index + 1
        hi_bound = node_count + 1
        hi = bisect.bisect_right(full_ticks, max_pos_ticks, lo, hi_bound)
        return range(lo, hi)

    states = {i: {} for i in range(-1, node_count + 1)}
    start_key = (0, 0, (), ())
    states[-1][start_fuel_ticks] = _StateRecord(
        key=start_key, predecessor=None, edge=None
    )

    def _wins(target_index, arrival_fuel_ticks, new_key):
        """Cheap winner-check only -- no state mutation, no `_EdgeInfo`
        construction. Split out from `relax` (below) so the *expensive*
        per-target purchase-reason computation (a `reachable_cheaper` scan
        plus several `Decimal` operations per bypassed candidate) can be
        skipped entirely for the many candidate transitions that lose this
        check, rather than built eagerly for every transition and then
        thrown away. Reason computation only ever happens for a
        transition already confirmed to win -- the reasons themselves are
        unaffected, only *when* they are computed."""
        existing = states[target_index].get(arrival_fuel_ticks)
        if existing is None:
            return True
        if new_key is existing.key:
            # The overwhelmingly common "buy nothing" pass-through: several
            # levels/targets from the same predecessor propagate the exact
            # same key tuple object onward. Object identity trivially
            # implies "not strictly less" -- skip the exact-integer
            # `key_less` call entirely rather than re-deriving the same
            # answer from scratch.
            return False
        return key_less(new_key, existing.key)

    def relax(target_index, arrival_fuel_ticks, new_key, predecessor, edge):
        """Unconditionally re-checks and commits -- used only where the
        caller has not already called `_wins` itself (the "buy nothing"
        pass-through path and START's one-time setup, both of which pass
        `edge=None` and have nothing expensive to defer)."""
        if _wins(target_index, arrival_fuel_ticks, new_key):
            states[target_index][arrival_fuel_ticks] = _StateRecord(
                key=new_key, predecessor=predecessor, edge=edge
            )

    # START is non-purchasable: exactly one fixed departure level. Every
    # node within that fixed range -- not just the farthest -- gets its
    # own state (see reachable_targets' docstring above). One-time, not on
    # the hot path, so it stays in the original Decimal `reachable_targets`
    # form -- only the resulting arrival level is stored as ticks.
    start_ahead = nodes_ahead_of(-1)
    for target_pos, target_index in reachable_targets(start_ahead, Decimal(0), start_fuel):
        relax(
            target_index,
            start_fuel_ticks - full_ticks[target_index],
            start_key,
            (-1, start_fuel_ticks),
            None,
        )

    for node_index in range(node_count):
        station = ordered[node_index]
        pos = positions[node_index]
        pos_ticks = positions_ticks[node_index]
        ahead = nodes_ahead_of(node_index)

        reachable_cheaper = [
            (p, idx)
            for (p, idx) in ahead
            if idx != finish
            and p - pos <= tank_range_mi
            and ordered[idx].price_per_gallon < station.price_per_gallon
        ]
        ahead_has_cheaper = any(
            idx != finish and ordered[idx].price_per_gallon < station.price_per_gallon
            for (p, idx) in ahead
        )

        # Pareto state-dominance pruning (see the module docstring's
        # section by that name): `states[node_index]` is already fully
        # populated at this point (nothing later ever targets an earlier
        # node), so this is a one-time, complete frontier computation --
        # dominated entries are simply never chosen as a relaxation
        # source, never physically removed from `states[node_index]`.
        for fuel_on_arrival_ticks, record in _pareto_frontier(
            states[node_index], key_less
        ).items():
            fuel_on_arrival = _from_ticks(fuel_on_arrival_ticks)
            levels = useful_fill_levels_mi(
                pos,
                fuel_on_arrival,
                tank_range_mi=tank_range_mi,
                nodes_ahead_mi=[p for (p, idx) in ahead],
            )
            for level in levels:
                level_ticks = _to_ticks(level)
                target_range = _reachable_ticks(node_index, pos_ticks + level_ticks)
                if not target_range:
                    continue

                # D-06: one clock read per _DEADLINE_CHECK_STRIDE (state,
                # level) pairs examined -- this granularity, not the
                # innermost per-target loop (too hot: it would pay on
                # every transition, including cells that complete
                # comfortably) and not the outer per-node boundary (too
                # coarse: the bound exists but is not a number that can be
                # stated, and it is worst exactly where the DP is
                # slowest).
                _transitions_examined += 1
                if (
                    deadline_at is not None
                    and _transitions_examined % _DEADLINE_CHECK_STRIDE == 0
                ):
                    now = time.monotonic()
                    if now >= deadline_at:
                        raise DeadlineExceededError(
                            deadline_seconds=deadline,
                            elapsed_seconds=now - (deadline_at - float(deadline)),
                            transitions_examined=_transitions_examined,
                        )

                buy_ticks = level_ticks - fuel_on_arrival_ticks
                is_purchase = buy_ticks > 0
                predecessor = (node_index, fuel_on_arrival_ticks)

                if not is_purchase:
                    for target_index in target_range:
                        relax(
                            target_index,
                            level_ticks - (full_ticks[target_index] - pos_ticks),
                            record.key,
                            predecessor,
                            None,
                        )
                    continue

                # Exact-integer fuel-dollar contribution of THIS purchase,
                # scaled by MONEY_SCALE (see the module docstring's
                # "Exact-integer money-domain comparison" section): both
                # `buy_ticks` and `price_ticks[node_index]` are exact
                # integers, so their product is exact too -- no `Decimal`,
                # no `float`, anywhere in this line.
                purchase_ticks = buy_ticks * price_ticks[node_index]
                is_full_fill = level == tank_range_mi
                new_key = (
                    record.key[0]
                    + purchase_ticks * P_DEN
                    + P_NUM
                    + margin_numerators[node_index],
                    record.key[1] + 1,
                    record.key[2] + (pos,),
                    record.key[3] + (station.opis_id,),
                )
                reachable_cheaper_idx = {idx for (_p, idx) in reachable_cheaper}

                # `gallons`/`cost` -- the actual `Decimal` purchase record
                # (D-03), computed by the exact same `buy_mi = level -
                # fuel_on_arrival; gallons = buy_mi / mpg; cost = gallons *
                # price` sequence this module has always used -- are
                # materialized lazily, at most once per (state, level),
                # only once this level's purchase is confirmed to win at
                # least one target below. The comparison above never
                # needed them (see the module docstring); a purchase that
                # loses every target it reaches never pays for a `Decimal`
                # division at all.
                gallons = None
                cost = None

                # The reason and its bypassed-cheaper counters explain THIS
                # purchase decision, computed PER TARGET (not once per
                # level): a purchase that reaches multiple simultaneous
                # nodes (e.g. a full tank that both tops past an
                # intermediate candidate AND reaches FINISH, or that
                # coincidentally lands exactly on a reachable-cheaper
                # station) can have a genuinely different reason for each
                # target it reaches.
                for target_index in target_range:
                    arrival_fuel_ticks = level_ticks - (
                        full_ticks[target_index] - pos_ticks
                    )
                    # Inlined `_wins` (same three checks, same order, same
                    # meaning -- see its docstring) to shave one Python
                    # function-call layer off this loop's dominant share
                    # (~97%) of every purchase-transition attempt. Losing
                    # transitions are never on the winning path, so their
                    # purchase-reason story is never observed by any
                    # caller -- skip computing it. Values are unaffected
                    # either way, only whether this attempt's own reason
                    # is ever built.
                    existing = states[target_index].get(arrival_fuel_ticks)
                    if existing is not None:
                        if new_key is existing.key:
                            continue
                        if not key_less(new_key, existing.key):
                            continue

                    if gallons is None:
                        buy_mi = level - fuel_on_arrival
                        gallons = buy_mi / mpg
                        cost = gallons * station.price_per_gallon

                    target_pos = (
                        positions[target_index]
                        if target_index < node_count
                        else total_route_mi
                    )
                    bypassed_count = 0
                    bypassed_saving = None
                    estimate_bypassed_count = 0
                    estimate_bypassed_saving = None

                    if target_index == finish:
                        # REACH_FINISH always overrides -- this edge's
                        # purpose is completing the trip, not routing
                        # toward a named station.
                        reason = PurchaseReason.REACH_FINISH
                        reason_target_opis_id = None
                        reason_target_name = None
                    elif not is_full_fill:
                        reason = PurchaseReason.REACH_CHEAPER_STOP
                        reason_target_opis_id = ordered[target_index].opis_id
                        reason_target_name = ordered[target_index].name
                    elif target_index in reachable_cheaper_idx:
                        # The full-fill's exact-reach amount coincides
                        # with a reachable-cheaper station's own position
                        # (e.g. that station sits exactly one tank away).
                        # Landing there is REACHING that cheaper station,
                        # not bypassing it -- nothing was flown past.
                        reason = PurchaseReason.REACH_CHEAPER_STOP
                        reason_target_opis_id = ordered[target_index].opis_id
                        reason_target_name = ordered[target_index].name
                    else:
                        # Full fill, target is not itself a reachable
                        # cheaper station. Only stations strictly BEFORE
                        # this target were actually flown past by this
                        # specific edge -- a reachable-cheaper station at
                        # or beyond the target was never bypassed here.
                        truly_bypassed = [
                            (p, idx)
                            for (p, idx) in reachable_cheaper
                            if p < target_pos
                        ]
                        saving_total = Decimal(0)
                        for cheaper_pos, cheaper_idx in truly_bypassed:
                            cheaper_gap = cheaper_pos - pos
                            cheaper_buy_mi = max(
                                Decimal(0), cheaper_gap - fuel_on_arrival
                            )
                            cheaper_gallons = cheaper_buy_mi / mpg
                            saving_total += cheaper_gallons * (
                                station.price_per_gallon
                                - ordered[cheaper_idx].price_per_gallon
                            )
                        if truly_bypassed and penalty > saving_total:
                            # A genuine bypass AND the flat per-stop
                            # penalty strictly outweighs the fuel-dollar
                            # saving those bypassed stations offered --
                            # both conditions, never bypass-count alone.
                            # saving_total is never negative (each term is
                            # a nonnegative gallon count times a strictly
                            # positive price difference), so this branch
                            # can never fire at penalty=0.
                            reason = PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
                            bypassed_count = len(truly_bypassed)
                            bypassed_saving = saving_total
                            # PROV-03 (D-19/D-20): narrow this SAME
                            # truly-bypassed set to its estimate-priced
                            # members, gated on the purchasing station
                            # itself being real-priced -- read only
                            # through `is_estimate_priced`, never a direct
                            # provenance comparison here (see
                            # PriceSourceUsagePurityTest).
                            if not is_estimate_priced(station):
                                truly_bypassed_estimate = [
                                    (p, idx)
                                    for (p, idx) in truly_bypassed
                                    if is_estimate_priced(ordered[idx])
                                ]
                                if truly_bypassed_estimate:
                                    estimate_saving_total = Decimal(0)
                                    for cheaper_pos, cheaper_idx in truly_bypassed_estimate:
                                        cheaper_gap = cheaper_pos - pos
                                        cheaper_buy_mi = max(
                                            Decimal(0), cheaper_gap - fuel_on_arrival
                                        )
                                        cheaper_gallons = cheaper_buy_mi / mpg
                                        estimate_saving_total += cheaper_gallons * (
                                            station.price_per_gallon
                                            - ordered[cheaper_idx].price_per_gallon
                                        )
                                    estimate_bypassed_count = len(truly_bypassed_estimate)
                                    estimate_bypassed_saving = estimate_saving_total
                        elif ahead_has_cheaper:
                            reason = PurchaseReason.FILL_TO_CONTINUE
                        else:
                            reason = PurchaseReason.TOP_UP_AT_CHEAPEST
                        reason_target_opis_id = ordered[target_index].opis_id
                        reason_target_name = ordered[target_index].name

                    edge = _EdgeInfo(
                        name=station.name,
                        opis_id=station.opis_id,
                        price=station.price_per_gallon,
                        pos=pos,
                        gallons=gallons,
                        cost=cost,
                        reason=reason,
                        reason_target_opis_id=reason_target_opis_id,
                        reason_target_name=reason_target_name,
                        bypassed_cheaper_count=bypassed_count,
                        bypassed_saving_forgone=bypassed_saving,
                        price_source=station.price_source,
                        bypassed_estimate_count=estimate_bypassed_count,
                        bypassed_estimate_saving_forgone=estimate_bypassed_saving,
                    )
                    # Already confirmed a winner by the inlined check
                    # above, with nothing else touching this exact
                    # (target_index, arrival_fuel_ticks) state in between
                    # -- commit directly rather than re-checking.
                    states[target_index][arrival_fuel_ticks] = _StateRecord(
                        key=new_key, predecessor=predecessor, edge=edge
                    )

    winner = None
    for record in states[finish].values():
        if winner is None or key_less(record.key, winner.key):
            winner = record

    if winner is None:
        # Only reachable when a caller skips the documented
        # `preflight_gap_check` precondition on genuinely infeasible
        # input -- surfaced as a clear, named failure rather than a bare
        # AttributeError/KeyError from an empty FINISH state table.
        raise InfeasibleRouteError(
            from_station="START" if node_count == 0 else ordered[-1].name,
            to_station="FINISH",
            gap_mi=total_route_mi - (Decimal(0) if node_count == 0 else positions[-1]),
            max_range_mi=tank_range_mi,
        )

    edges = []
    current = winner
    while current.predecessor is not None:
        if current.edge is not None:
            edges.append(current.edge)
        predecessor_index, predecessor_fuel = current.predecessor
        current = states[predecessor_index][predecessor_fuel]
    edges.reverse()

    stops = [
        FuelStop(
            name=edge.name,
            opis_id=edge.opis_id,
            price_per_gallon=edge.price,
            distance_from_start_mi=edge.pos,
            gallons=edge.gallons,
            cost=edge.cost,
            purchase_reason=edge.reason,
            reason_target_opis_id=edge.reason_target_opis_id,
            reason_target_name=edge.reason_target_name,
            bypassed_cheaper_count=edge.bypassed_cheaper_count,
            bypassed_saving_forgone=edge.bypassed_saving_forgone,
            price_source=edge.price_source,
            bypassed_estimate_count=edge.bypassed_estimate_count,
            bypassed_estimate_saving_forgone=edge.bypassed_estimate_saving_forgone,
        )
        for edge in edges
    ]

    total_cost = sum((s.cost for s in stops), Decimal(0))
    total_gallons = sum((s.gallons for s in stops), Decimal(0))
    total_margin_applied = sum(
        (trust_margin_for(s, trust_margin) for s in stops), Decimal(0)
    )

    return FuelPlan(
        stops=stops,
        total_cost=total_cost,
        total_gallons=total_gallons,
        penalised_objective=total_cost + penalty * len(stops) + total_margin_applied,
        penalty_applied=penalty,
        trust_margin_applied=trust_margin,
    )

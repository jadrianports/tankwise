"""Pinned measurement parameters for plan 18-05's before/after objective
report (D-28/D-30/D-31/D-37), plus the two CI-enforcing guards that
protect the claims `measure_plan_objective` reports in full.

OBJECTIVE_PARAMS, TRIVIAL_STOP_TANK_FRACTION and DALLAS_SEATTLE_STOP_RANGE
were fixed here BEFORE any figure in this plan was measured -- neither the
command nor the guard classes below define a copy of their own; both
import from this module, the single shared source of truth, mirroring the
CORPUS_PARAMS/OBJECTIVE_PARAMS discipline Phases 16-18 have used
throughout.
"""
import io
from dataclasses import dataclass
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from routing.services import corridor, solver
from routing.services.exceptions import InfeasibleRouteError
from routing.services.solver import PurchaseReason
from routing.tests.test_corridor_fixtures import (
    PRICE_BASIS_NEUTRAL,
    factor_lookup_for_basis,
    load_corridor_route,
)


@dataclass(frozen=True)
class ObjectiveParams:
    """One pinned (vehicle, penalty, price basis) cell -- the single
    parameter set `measure_plan_objective` measures both arms at."""

    mpg: Decimal
    tank_range_mi: Decimal
    starting_fuel: Decimal
    penalty: Decimal
    price_basis: str


# Pinned before any measurement was taken (D-28/D-30). The UI-default
# vehicle (Semi, loaded: 6.5 mpg / 1,050 mi tank -- ROADMAP.md's own
# "evidence base" line), a full tank at the origin, the sourced $35
# fixed-charge penalty (18-04-SUMMARY.md's ATRI/TruckerPath derivation),
# and the neutral 1.0 price basis. Neutral is the headline because its
# meaning never drifts and is literally what `eia._frozen_table()`
# returns -- the codebase's own degradation mode, not an artificial
# choice. The `eia_fixture` basis is reported alongside, never as the
# headline. Never adjusted after seeing a measured figure.
OBJECTIVE_PARAMS = ObjectiveParams(
    mpg=Decimal("6.5"),
    tank_range_mi=Decimal("1050"),
    starting_fuel=Decimal(1),
    penalty=Decimal("35"),
    price_basis=PRICE_BASIS_NEUTRAL,
)

# ROADMAP Phase 18 criterion 1's own "under 10% of tank capacity"
# threshold, applied to gallons as a fraction of tank_range_mi / mpg.
TRIVIAL_STOP_TANK_FRACTION = Decimal("0.10")

# Criterion 1's stated Dallas -> Seattle stop-count range. Pinned here,
# before measurement, for PlanObjectiveGuardTests below. If the measured
# count falls outside this range, that is a finding to record in the
# plan's SUMMARY and hand to plan 18-08 for a ROADMAP amendment -- NOT a
# bound to widen.
DALLAS_SEATTLE_STOP_RANGE = (3, 4)

# D-05: the named, committed corridor pinning the real observed instance
# of the penalty-native reason. Chosen by running the measurement first
# (see 18-05-SUMMARY.md) and picking a corridor that genuinely exhibits
# it -- "real observed" points at a real corridor, not generated data. At
# the UI-default tank range, QUIKTRIP #667 (opis_id 71647) fills past
# CIRCLE K #4707605 rather than pay a second stop fee for it.
PENALTY_NATIVE_REASON_CORRIDOR_SLUG = "el_paso_tx-portland_me"


class PlanObjectiveMeasurementTestCase(TestCase):
    """Seeds the real, committed station dataset once per class, mirroring
    `routing/tests/test_solver_dispatch.py`'s own
    `RealCorridorDispatchTestCase` -- these guards exercise the real
    `corridor.candidates()` DB path against the real, committed corridor
    geometry fixtures, not a synthetic stand-in for either.

    A Django `TestCase` (DB-backed), not a bare `SimpleTestCase` --
    solving a real corridor geometry requires the corridor module's
    DB-backed STRtree over the Station table, so a `SimpleTestCase`
    (which forbids DB access by default) would raise on the first query.
    This mirrors the established pattern for exactly this kind of
    real-corridor/real-DB test already in this codebase
    (`RealCorridorDispatchTestCase`).
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def setUp(self):
        self.factor_for = factor_lookup_for_basis(OBJECTIVE_PARAMS.price_basis)

    def _solve(self, slug, *, penalty):
        route = load_corridor_route(slug)
        candidates = corridor.candidates(route, factor_for=self.factor_for)
        return solver.solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
            mpg=OBJECTIVE_PARAMS.mpg,
            starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
            penalty=penalty,
        )


class PlanObjectiveGuardTests(PlanObjectiveMeasurementTestCase):
    """D-31's loose corridor guard -- covers exactly one corridor
    (Dallas, TX -> Seattle, WA), not all twelve: guarding all twelve was
    considered and rejected, since it would run the full corridor pass
    twelve times per commit and couple the build to dataset specifics the
    v4.0 Overture expansion is planned to change. This mirrors how Phases
    16 and 17 each paired a report-only command with a small seeded
    in-suite guard, so a future change that silently reintroduces
    micro-stops fails the build rather than being noticed in a demo.
    """

    def test_dallas_seattle_zero_trivial_stops_at_ui_default(self):
        plan = self._solve("dallas_tx-seattle_wa", penalty=OBJECTIVE_PARAMS.penalty)
        tank_capacity_gal = OBJECTIVE_PARAMS.tank_range_mi / OBJECTIVE_PARAMS.mpg
        threshold_gal = tank_capacity_gal * TRIVIAL_STOP_TANK_FRACTION
        trivial_stops = [s for s in plan.stops if s.gallons < threshold_gal]
        self.assertEqual(
            trivial_stops,
            [],
            f"expected zero trivial stops (< {threshold_gal} gal), found "
            f"{len(trivial_stops)}: {[(s.name, s.gallons) for s in trivial_stops]}",
        )

    def test_dallas_seattle_does_not_raise_infeasible(self):
        try:
            self._solve("dallas_tx-seattle_wa", penalty=OBJECTIVE_PARAMS.penalty)
        except InfeasibleRouteError as exc:
            self.fail(f"dallas_tx-seattle_wa raised InfeasibleRouteError: {exc}")

    def test_dallas_seattle_stop_count_within_criterion_1_range(self):
        """CONFIRMED 2026-08-04 (plan 18-14, gap closure) -- this guard was
        reconciled against the DEPLOYED build, not merely the working
        tree. `estimate_transition_count` was checked in a pinned,
        pre-registered verdict (18-10): NOT SALVAGEABLE, and no member of
        a closed seven-predictor family (incumbent included) qualifies as
        a replacement either. The first deployed-hardware `Server-Timing`
        measurement on this exact corridor was then taken (18-11).
        `DISPATCH_RETENTION_FLOOR` was pinned and the outcome proven
        arithmetically NOT TRACTABLE (18-12): no single
        `DP_TRANSITION_BUDGET` value can simultaneously demote the known
        live-breaching cell (estimate 61,912, HTTP 500 pre-hotfix) and
        retain the cell this guard's own vehicle profile needs
        (estimate 117,895 at this mpg) -- 117,895 > 61,912. So
        `DP_TRANSITION_BUDGET` stays 50,000, `dallas_tx-seattle_wa`
        remains on `penalty_aware_heuristic` at BOTH pinned tank ranges,
        and this assertion is unchanged from the 2026-08-02 hotfix
        reconciliation below. Re-measured twice this plan -- once locally
        (this exact test, same `OBJECTIVE_PARAMS`) and once against the
        now-deployed live instance (`solver_strategy` in the live JSON
        response body, `https://tankwise.onrender.com/api/route`,
        Dallas/Seattle at the API-default vehicle) -- both confirm
        `strategy=penalty_aware_heuristic`. Nothing moved; the entire
        point of this reconciliation was to CHECK the deployed build
        before writing anything, not to assume it matched the working
        tree.

        **RE-DECORATING THIS TEST WITH `@unittest.expectedFailure` IS
        FORBIDDEN, and here is why.** A test wrapped in that decorator
        asserts something KNOWN FALSE and still reports green to CI --
        it is a debt marker wearing a green checkmark, not a guard. This
        phase already spent one such marker's worth of goodwill (the
        original D-31 decorator below, worn from 2026-07-31 until
        2026-08-02). If a future change ever makes this assertion false
        again, the correct response is the same one plan 18-08 already
        took once: reconcile ROADMAP.md criterion 1 against whatever the
        DP or heuristic now actually returns, rewrite this assertion (and
        this docstring) to match that reconciled truth, and only then --
        if the new value is still worth guarding -- land a new, honestly
        passing assertion. Never re-wrap a false assertion in a decorator
        and call the reconciliation done.

        **The four stop-count figures, side by side, never reconciled
        into one number:**

            1. ROADMAP criterion 1's ORIGINAL pinned claim
               (`DALLAS_SEATTLE_STOP_RANGE` below): "3-4 stops" -- D-31's
               literal reading of the criterion's own wording, fixed
               BEFORE any DP/heuristic figure existed for this corridor.
            2. The EXACT DP's answer (pre-hotfix,
               `DP_TRANSITION_BUDGET=134,000`): `strategy=exact_dp`,
               2 stops, $498.04 -- OUTSIDE the pinned range. This was
               criterion 1's real finding (plan 18-05/18-08): the
               original claim described a heuristic's answer, not the
               true optimum.
            3. The HOTFIX heuristic's answer
               (`DP_TRANSITION_BUDGET` 134,000 -> 50,000, commit
               `99aacdd`, applied 2026-08-02 after a live HTTP 500):
               `strategy=penalty_aware_heuristic`, 3 stops, $552.24
               (+$54.20 / +10.9% vs #2) -- INSIDE the pinned range,
               coincidentally.
            4. The SHIPPED, DEPLOYED dispatch's answer, CONFIRMED
               2026-08-04 after the full gap-closure sweep (18-10's
               predictor verdict, 18-11's deployed-hardware measurement,
               18-12's NOT TRACTABLE proof): IDENTICAL to #3 --
               `strategy=penalty_aware_heuristic`, 3 stops, $552.24. No
               budget value exists that restores #2 without reopening the
               live HTTP 500 -- the same inversion (117,895 > 61,912)
               reproduces on both workstation and deployed-hardware
               timings. This corridor is NOT provably optimal on this
               hardware, and `solver_strategy` reports that honestly on
               every response.

        --- earlier update, retained verbatim (2026-08-02) ---

        `@unittest.expectedFailure` REMOVED because
        this assertion now genuinely passes, and the docstring below called
        that out in advance as "worth investigating, not an assumed
        non-event, since it would mean the shipped solver's behaviour on
        this corridor changed." It did change, and here is exactly how.

        The `DP_TRANSITION_BUDGET` hotfix (134,000 -> 50,000; see
        `routing/services/dp.py`) demotes dallas_tx-seattle_wa from the
        exact DP to the penalty-aware heuristic at BOTH pinned tank ranges,
        because at the API default vehicle this corridor was returning
        HTTP 500 live against `GUNICORN_TIMEOUT=30`.

        Measured consequence on this corridor at OBJECTIVE_PARAMS
        (1,050 mi tank, 6.5 mpg, $35 penalty):

            before  strategy=exact_dp                stops=2  cost=$498.04
            after   strategy=penalty_aware_heuristic stops=3  cost=$552.24

        That is +$54.20 (+10.9%) -- inside the 12.5% max heuristic-vs-exact
        gap plan 18-04d measured, but well above its 6.5% average. The plan
        is no longer provably optimal on this corridor; `solver_strategy`
        reports that honestly on every response.

        Note the irony worth recording: 3 stops falls INSIDE
        DALLAS_SEATTLE_STOP_RANGE=(3, 4), the range D-31 pinned from
        ROADMAP criterion 1's literal wording and which the exact DP's own
        answer (2 stops) fell outside. Criterion 1's original claim
        described the heuristic's answer, not the exact optimum.

        THIS TEST IS NOW COUPLED TO A PROVISIONAL HOTFIX. If gap-closure
        work restores exact-DP dispatch for this corridor, the count
        returns to 2 and this assertion fails again -- at which point the
        correct move is to reconcile ROADMAP criterion 1 against the exact
        optimum, not to re-add `expectedFailure`.

        [2026-08-04 note: the gap-closure work this paragraph anticipated
        has now happened -- see the CONFIRMED section at the top of this
        docstring. It did NOT restore exact-DP dispatch (18-12 proved
        NOT TRACTABLE); the "provisional" hotfix is now a confirmed,
        deployed policy, and this test remains coupled to that confirmed
        policy rather than to a provisional one.]

        --- original D-31 rationale, retained verbatim ---

        D-31 pins DALLAS_SEATTLE_STOP_RANGE=(3, 4) BEFORE measurement,
        mirroring ROADMAP.md Phase 18 criterion 1's literal "3-4 stops"
        claim. This plan's own measurement (18-05-SUMMARY.md) found the
        real DP/heuristic dispatch returns 2 stops on this corridor at
        OBJECTIVE_PARAMS -- outside this range, a genuine criterion-1
        finding, not a bug. Per D-31's explicit instruction, the range is
        NOT widened to make this assertion pass. Marked
        `expectedFailure` instead: the assertion still genuinely runs
        (this is not a vacuous or skipped test) and its failure message
        stays visible in verbose test output, but a documented, tracked
        mismatch does not block CI on every subsequent commit until plan
        18-08 reconciles ROADMAP.md's wording against this measured
        evidence. If this assertion ever starts PASSING, Django's test
        runner reports it as an "unexpected success" (itself a
        non-zero-exit signal) -- worth investigating, not an assumed
        non-event, since it would mean the shipped solver's behaviour on
        this corridor changed.
        """
        plan = self._solve("dallas_tx-seattle_wa", penalty=OBJECTIVE_PARAMS.penalty)
        low, high = DALLAS_SEATTLE_STOP_RANGE
        self.assertTrue(
            low <= len(plan.stops) <= high,
            f"measured {len(plan.stops)} stops via strategy={plan.strategy}, "
            f"outside DALLAS_SEATTLE_STOP_RANGE={DALLAS_SEATTLE_STOP_RANGE}",
        )


class PenaltyNativeReasonCorridorTests(PlanObjectiveMeasurementTestCase):
    """D-05's pinned real observation: on the named, committed
    PENALTY_NATIVE_REASON_CORRIDOR_SLUG corridor, at least one stop
    carries `PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP` at
    OBJECTIVE_PARAMS's sourced $35 penalty, and the same corridor at
    `penalty=Decimal(0)` produces none. Deterministic and offline
    (committed geometry fixture, committed CSV-replayed station table) --
    a standing guard that the penalty path never silently no-ops.
    """

    def test_bypass_reason_fires_at_the_sourced_penalty(self):
        plan = self._solve(
            PENALTY_NATIVE_REASON_CORRIDOR_SLUG, penalty=OBJECTIVE_PARAMS.penalty
        )
        bypass_stops = [
            s
            for s in plan.stops
            if s.purchase_reason == PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
        ]
        self.assertGreaterEqual(
            len(bypass_stops),
            1,
            "expected at least one BYPASS_CHEAPER_NOT_WORTH_STOP stop on "
            f"{PENALTY_NATIVE_REASON_CORRIDOR_SLUG} at "
            f"penalty={OBJECTIVE_PARAMS.penalty}, found none",
        )

    def test_bypass_reason_absent_at_zero_penalty(self):
        plan = self._solve(PENALTY_NATIVE_REASON_CORRIDOR_SLUG, penalty=Decimal(0))
        bypass_stops = [
            s
            for s in plan.stops
            if s.purchase_reason == PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
        ]
        self.assertEqual(
            bypass_stops,
            [],
            "expected zero BYPASS_CHEAPER_NOT_WORTH_STOP stops at "
            "penalty=0 -- if this fails, the penalty path is silently "
            "no-oping",
        )

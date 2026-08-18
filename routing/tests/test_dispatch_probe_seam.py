"""Tests for `routing/probe_seam.py` -- D-06's gated dispatch-verdict probe
seam: the fail-soft current-RSS reader (`ProbeSeamRssReaderTests`), the
pure constant-time gate-resolution function (`ProbeSeamGateResolutionTests`),
`solve()`'s caller-supplied `transition_budget=` hatch
(`TransitionBudgetHatchTests`), and `RouteView`'s own wiring of all three
(`GatedProbeSeamViewTests`).
"""
import ast
import pathlib
import sys
import unittest
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from routing.probe_seam import (
    PROBE_BUDGET_HEADER,
    PROBE_KEY_HEADER,
    read_current_rss_kb,
    resolve_probe_budget,
    rss_delta_mb,
)
from routing.services import dp
from routing.services.corridor import reset_index
from routing.services.solver import SolverStrategy, solve
from routing.tests.test_solver_dispatch import (
    MPG,
    PENALTY,
    STARTING_FUEL,
    RealCorridorDispatchTestCase,
)
from routing.tests.test_views import (
    FINISH_COORD,
    MOCK_TARGET,
    ROUTE_URL,
    START_COORD,
    _directions_response,
    _long_directions_payload,
    _make_station,
)
from routing.throttles import RouteBurstThrottle, RouteSustainedThrottle
from routing.views import RouteView

# The same non-`None` ladder rungs `DP_TRANSITION_BUDGET_LADDER`
# (`routing/tests/test_dispatch_recovery.py`) carries -- the only legal set
# of rung values a probe may request. Transcribed here rather than imported
# to keep this test module's own gate-resolution assertions independent of
# that module's own pinned constant.
ALLOWED_RUNGS = (50_000, 70_000, 130_000, 200_000, 400_000)


class ProbeSeamRssReaderTests(SimpleTestCase):
    """`read_current_rss_kb()`/`rss_delta_mb()` -- Task 1."""

    def test_returns_none_off_linux(self):
        # This project's dev workstation is Windows -- asserted
        # unconditionally, no skip, since this branch is the one every
        # local `manage.py test` run actually exercises.
        if sys.platform != "linux":
            self.assertIsNone(read_current_rss_kb())

    @unittest.skipUnless(sys.platform == "linux", "Linux-only VmRSS read")
    def test_returns_a_positive_int_on_linux(self):
        value = read_current_rss_kb()
        self.assertIsInstance(value, int)
        self.assertGreater(value, 0)

    def test_never_raises_when_the_procfs_read_fails(self):
        # Force the platform branch open regardless of the host OS, then
        # make the read itself fail -- proves the fail-soft posture
        # without depending on this test actually running on Linux.
        with mock.patch("routing.probe_seam.sys.platform", "linux"), mock.patch(
            "builtins.open", side_effect=FileNotFoundError
        ):
            self.assertIsNone(read_current_rss_kb())

    def test_rss_delta_mb_none_propagation(self):
        self.assertIsNone(rss_delta_mb(None, 100))
        self.assertIsNone(rss_delta_mb(100, None))
        self.assertIsNone(rss_delta_mb(None, None))

    def test_rss_delta_mb_arithmetic(self):
        # 1024 KB -> 2048 KB is a 1024 KB = 1 MB delta.
        self.assertEqual(rss_delta_mb(1024, 2048), Decimal(1024) / Decimal(1024))

    def test_rss_delta_mb_negative_clamp(self):
        # after < before (the allocator returned pages mid-measurement) --
        # clamped to zero, never returned negative.
        result = rss_delta_mb(1024, 512)
        self.assertGreaterEqual(result, 0)
        self.assertEqual(result, Decimal(0))


class ProbeSeamGateResolutionTests(SimpleTestCase):
    """`resolve_probe_budget()` -- Task 2. Covers the default-inert posture
    (empty/None/unset secret always returns `None`, regardless of the
    header pair), the two accept paths (a permitted rung, drawn from
    `ALLOWED_RUNGS`), and every reject path (wrong secret, missing
    headers, a rung outside `allowed_rungs`, a non-numeric budget header)
    -- plus the structural AST assertion that the module actually uses a
    constant-time comparison and contains no plain equality against the
    secret parameter (T-26-06's mitigation).
    """

    def test_empty_secret_always_returns_none(self):
        headers = {PROBE_KEY_HEADER: "", PROBE_BUDGET_HEADER: "130000"}
        self.assertIsNone(
            resolve_probe_budget(headers, secret="", allowed_rungs=ALLOWED_RUNGS)
        )

    def test_none_secret_always_returns_none(self):
        headers = {PROBE_KEY_HEADER: "correct-secret", PROBE_BUDGET_HEADER: "130000"}
        self.assertIsNone(
            resolve_probe_budget(headers, secret=None, allowed_rungs=ALLOWED_RUNGS)
        )

    def test_unset_secret_returns_none_even_with_a_correct_looking_header(self):
        # A request carrying what LOOKS like a valid probe key, against an
        # unset (falsy) configured secret -- the default-inert posture the
        # whole seam rests on.
        headers = {
            PROBE_KEY_HEADER: "whatever-a-guessing-attacker-sends",
            PROBE_BUDGET_HEADER: "130000",
        }
        self.assertIsNone(
            resolve_probe_budget(headers, secret=None, allowed_rungs=ALLOWED_RUNGS)
        )

    def test_correct_secret_and_a_permitted_rung_returns_that_rung(self):
        headers = {PROBE_KEY_HEADER: "correct-secret", PROBE_BUDGET_HEADER: "130000"}
        result = resolve_probe_budget(
            headers, secret="correct-secret", allowed_rungs=ALLOWED_RUNGS
        )
        self.assertEqual(result, 130_000)
        self.assertIsInstance(result, int)

    def test_rung_not_in_allowed_rungs_returns_none(self):
        headers = {PROBE_KEY_HEADER: "correct-secret", PROBE_BUDGET_HEADER: "99999"}
        self.assertIsNone(
            resolve_probe_budget(
                headers, secret="correct-secret", allowed_rungs=ALLOWED_RUNGS
            )
        )

    def test_rung_larger_than_every_ladder_member_returns_none(self):
        headers = {
            PROBE_KEY_HEADER: "correct-secret",
            PROBE_BUDGET_HEADER: "999999999",
        }
        self.assertIsNone(
            resolve_probe_budget(
                headers, secret="correct-secret", allowed_rungs=ALLOWED_RUNGS
            )
        )

    def test_non_numeric_budget_header_returns_none(self):
        headers = {PROBE_KEY_HEADER: "correct-secret", PROBE_BUDGET_HEADER: "not-a-number"}
        self.assertIsNone(
            resolve_probe_budget(
                headers, secret="correct-secret", allowed_rungs=ALLOWED_RUNGS
            )
        )

    def test_wrong_secret_returns_none(self):
        headers = {PROBE_KEY_HEADER: "wrong-secret", PROBE_BUDGET_HEADER: "130000"}
        self.assertIsNone(
            resolve_probe_budget(
                headers, secret="correct-secret", allowed_rungs=ALLOWED_RUNGS
            )
        )

    def test_missing_key_header_returns_none(self):
        headers = {PROBE_BUDGET_HEADER: "130000"}
        self.assertIsNone(
            resolve_probe_budget(
                headers, secret="correct-secret", allowed_rungs=ALLOWED_RUNGS
            )
        )

    def test_missing_budget_header_returns_none(self):
        headers = {PROBE_KEY_HEADER: "correct-secret"}
        self.assertIsNone(
            resolve_probe_budget(
                headers, secret="correct-secret", allowed_rungs=ALLOWED_RUNGS
            )
        )

    def test_uses_constant_time_comparison_and_no_plain_equality_against_secret(self):
        """Structural AST assertion (T-26-06): `resolve_probe_budget` must
        call `hmac.compare_digest` at least once, and must contain no
        `ast.Compare` whose operator is `Eq`/`NotEq` with the `secret`
        parameter as one operand -- a plain `==`/`!=` against the secret
        is exactly the timing side channel `hmac.compare_digest` exists to
        close.
        """
        path = pathlib.Path(__file__).resolve().parent.parent / "probe_seam.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        uses_compare_digest = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compare_digest"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "hmac"
            for node in ast.walk(tree)
        )
        self.assertTrue(
            uses_compare_digest,
            "routing/probe_seam.py must call hmac.compare_digest at least once",
        )

        plain_equality_against_secret = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            names = {n.id for n in operands if isinstance(n, ast.Name)}
            if "secret" not in names:
                continue
            if any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                plain_equality_against_secret.append(node.lineno)

        self.assertEqual(
            plain_equality_against_secret,
            [],
            "routing/probe_seam.py must never compare 'secret' with a plain "
            f"==/!=: offending line(s) {plain_equality_against_secret}",
        )


class TransitionBudgetHatchTests(RealCorridorDispatchTestCase):
    """`solve()`'s caller-supplied `transition_budget=` hatch -- Task 3.
    Three directions, proving the hatch is non-vacuous: (1) omitting the
    keyword dispatches byte-identically to passing the production default
    explicitly; (2) a tiny value CLOSES the gate on a cell the manifest
    already admits; (3) a raised value OPENS the gate on a cell the
    manifest demotes. Direction 3 asserts on the ATTEMPT, not a latency
    figure -- the production `deadline=` hatch is left at its default
    throughout this class, so a slow box may legitimately breach it and
    fall back; that is still proof the transition_budget gate opened, not
    a test failure.
    """

    def test_omitting_the_keyword_matches_the_explicit_default(self):
        # houston_tx-chicago_il@1050mi: estimate 23 (ADMISSION_MANIFEST) --
        # trivially fast under either arm, so no deadline flakiness risk.
        route, candidates = self._route_and_candidates("houston_tx-chicago_il")
        common_kwargs = dict(
            tank_range_mi=Decimal(1050),
            mpg=MPG,
            starting_fuel=STARTING_FUEL,
            penalty=PENALTY,
        )
        plan_default = solve(candidates, route.total_route_mi, **common_kwargs)
        plan_explicit = solve(
            candidates,
            route.total_route_mi,
            transition_budget=dp.DP_TRANSITION_BUDGET,
            **common_kwargs,
        )
        self.assertEqual(plan_default.strategy, plan_explicit.strategy)
        self.assertEqual(
            tuple(s.opis_id for s in plan_default.stops),
            tuple(s.opis_id for s in plan_explicit.stops),
        )

    def test_a_tiny_budget_closes_the_gate_on_an_admitted_cell(self):
        # Same admitted cell as above (estimate 23) -- a budget of 1
        # forces the over-budget branch even though the production
        # default would have admitted it, proving the parameter is
        # actually consulted rather than merely accepted and ignored.
        route, candidates = self._route_and_candidates("houston_tx-chicago_il")
        plan = solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=Decimal(1050),
            mpg=MPG,
            starting_fuel=STARTING_FUEL,
            penalty=PENALTY,
            transition_budget=1,
        )
        self.assertEqual(plan.strategy, SolverStrategy.PENALTY_AWARE_HEURISTIC)
        self.assertFalse(
            plan.deadline_breached,
            "the over-budget branch never attempts the DP, so it must "
            "never set deadline_breached",
        )

    def test_a_raised_budget_opens_the_gate_on_a_demoted_cell(self):
        # dallas_tx-seattle_wa@1050mi: estimate 117,895 -- demoted under
        # the production 50,000 budget (ADMISSION_MANIFEST), but under the
        # 130,000 rung dp.py's own dated note names as the measured
        # (never-shipped) qualifying rung for this exact cell.
        route, candidates = self._route_and_candidates("dallas_tx-seattle_wa")
        plan = solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=Decimal(1050),
            mpg=MPG,
            starting_fuel=STARTING_FUEL,
            penalty=PENALTY,
            transition_budget=130_000,
        )
        # Assert on the ATTEMPT, not a latency figure: with the estimate
        # now clearing the raised budget, the DP is attempted either way
        # -- it either completes (strategy == EXACT_DP) or breaches the
        # still-in-force production deadline and falls back
        # (deadline_breached == True). Both are proof the gate opened;
        # neither is a test failure. The failure this guards against is
        # NEITHER happening -- the same non-attempt the tiny-budget
        # direction above exercises deliberately.
        attempted = plan.strategy == SolverStrategy.EXACT_DP or plan.deadline_breached
        self.assertTrue(
            attempted,
            "transition_budget=130_000 should have made the DP attempt "
            "dallas_tx-seattle_wa@1050mi (estimate 117,895); got "
            f"strategy={plan.strategy}, deadline_breached={plan.deadline_breached}",
        )


def _header_env_name(header_name):
    """Convert an HTTP header name (e.g. `PROBE_KEY_HEADER`) into the WSGI
    environ key Django's test client accepts as an `**extra` kwarg."""
    return "HTTP_" + header_name.upper().replace("-", "_")


_PROBE_KEY_KWARG = _header_env_name(PROBE_KEY_HEADER)
_PROBE_BUDGET_KWARG = _header_env_name(PROBE_BUDGET_HEADER)


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public-token")
class GatedProbeSeamViewTests(APITestCase):
    """`RouteView.post()`'s D-06 wiring -- Task 1: probe-budget resolution,
    the `transition_budget=` thread-through, and the cache-write bypass.

    Every request below goes through DRF's real test client and the real
    corridor/solve/serialize pipeline against a mocked Mapbox transport --
    `solver.solve` is only ever wrapped with a thin kwargs-recording
    passthrough that still delegates to the genuine implementation, so an
    assertion here is on WHAT was passed to `solve()`, never on a
    synthetic stand-in double.
    """

    def setUp(self):
        cache.clear()
        reset_index()
        _make_station(97_001)

    @staticmethod
    def _headers(key=None, budget=None):
        headers = {}
        if key is not None:
            headers[_PROBE_KEY_KWARG] = key
        if budget is not None:
            headers[_PROBE_BUDGET_KWARG] = str(budget)
        return headers

    @staticmethod
    def _mapbox_mock():
        return mock.patch(
            MOCK_TARGET, return_value=_directions_response(_long_directions_payload())
        )

    def _post(self, headers=None):
        return self.client.post(
            ROUTE_URL,
            {"start": START_COORD, "finish": FINISH_COORD},
            format="json",
            **(headers or {}),
        )

    @staticmethod
    def _solve_kwargs_recorder():
        """Return `(captured, patch)`: `captured` accumulates the kwargs of
        every `solve()` call made while `patch` is active, and each call
        still delegates to the real, already-imported `solve` -- so the
        request pipeline runs genuinely rather than against a fake plan.
        """
        captured = []
        real_solve = solve

        def _recording(*args, **kwargs):
            captured.append(kwargs)
            return real_solve(*args, **kwargs)

        return captured, mock.patch("routing.views.solver.solve", side_effect=_recording)

    def test_secret_unset_probe_headers_produce_identical_response_to_no_headers(self):
        # The default posture in every environment except a deliberately
        # provisioned probe window: settings.DISPATCH_PROBE_SECRET is
        # unset, so a request carrying a correct-looking header pair must
        # behave identically to one carrying neither. cache.clear() +
        # reset_index() between the two calls forces both through a fresh
        # dispatch rather than letting the second short-circuit on the
        # first's cached response (the cache key does not vary with the
        # probe headers, by design).
        with self._mapbox_mock():
            without_headers = self._post()
        cache.clear()
        reset_index()
        with self._mapbox_mock():
            with_headers = self._post(
                headers=self._headers(key="whatever-looks-correct", budget=130_000)
            )

        self.assertEqual(without_headers.status_code, with_headers.status_code)
        self.assertEqual(
            without_headers.data["solver_strategy"],
            with_headers.data["solver_strategy"],
        )
        self.assertNotIn("peak_rss_mb", without_headers["Server-Timing"])
        self.assertNotIn("peak_rss_mb", with_headers["Server-Timing"])

    @override_settings(DISPATCH_PROBE_SECRET="probe-secret")
    def test_correct_secret_and_permitted_rung_reaches_solve_with_transition_budget(
        self,
    ):
        captured, recorder = self._solve_kwargs_recorder()
        with self._mapbox_mock(), recorder:
            response = self._post(
                headers=self._headers(key="probe-secret", budget=130_000)
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(captured, "solve() was never called")
        self.assertEqual(captured[0]["transition_budget"], 130_000)

    @override_settings(DISPATCH_PROBE_SECRET="probe-secret")
    def test_wrong_key_header_does_not_pass_transition_budget(self):
        captured, recorder = self._solve_kwargs_recorder()
        with self._mapbox_mock(), recorder:
            response = self._post(
                headers=self._headers(key="wrong-secret", budget=130_000)
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(captured, "solve() was never called")
        self.assertNotIn("transition_budget", captured[0])

    @override_settings(DISPATCH_PROBE_SECRET="probe-secret")
    def test_rung_outside_ladder_does_not_pass_transition_budget(self):
        captured, recorder = self._solve_kwargs_recorder()
        with self._mapbox_mock(), recorder:
            response = self._post(
                headers=self._headers(key="probe-secret", budget=999_999_999)
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(captured, "solve() was never called")
        self.assertNotIn("transition_budget", captured[0])

    @staticmethod
    def _route_cache_set_calls(mock_cache_set):
        """Filter `cache.set` calls down to the RouteView response cache
        write alone -- `django.core.cache.cache` is one shared singleton,
        so patching its `.set` also captures the throttle classes' own
        rate-limit bookkeeping (`throttle_route_*`) and `eia`'s cooldown
        marker (`eia:cooldown`), neither of which this plan changes.
        `build_cache_key` (`routing/cache.py`) always prefixes a route
        cache key `route:...`, which none of those other keys share.
        """
        return [
            call
            for call in mock_cache_set.call_args_list
            if call.args and str(call.args[0]).startswith("route:")
        ]

    @override_settings(DISPATCH_PROBE_SECRET="probe-secret")
    def test_probe_gated_request_does_not_write_the_cache(self):
        with self._mapbox_mock(), mock.patch(
            "routing.views.cache.set"
        ) as mock_cache_set:
            response = self._post(
                headers=self._headers(key="probe-secret", budget=130_000)
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._route_cache_set_calls(mock_cache_set), [])

    def test_non_probe_request_still_writes_the_cache(self):
        # The inverse of the assertion above -- proves the bypass guard is
        # non-vacuous rather than accidentally skipping every cache write.
        with self._mapbox_mock(), mock.patch(
            "routing.views.cache.set"
        ) as mock_cache_set:
            response = self._post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(self._route_cache_set_calls(mock_cache_set)), 1)

    def test_throttle_classes_unchanged(self):
        self.assertEqual(
            RouteView.throttle_classes, [RouteBurstThrottle, RouteSustainedThrottle]
        )

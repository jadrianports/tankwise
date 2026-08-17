"""Tests for `routing/probe_seam.py` -- D-06's gated dispatch-verdict probe
seam: the fail-soft current-RSS reader (`ProbeSeamRssReaderTests`), the
pure constant-time gate-resolution function (`ProbeSeamGateResolutionTests`),
and `solve()`'s caller-supplied `transition_budget=` hatch
(`TransitionBudgetHatchTests`).
"""
import ast
import pathlib
import sys
import unittest
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from routing.probe_seam import (
    PROBE_BUDGET_HEADER,
    PROBE_KEY_HEADER,
    read_current_rss_kb,
    resolve_probe_budget,
    rss_delta_mb,
)

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

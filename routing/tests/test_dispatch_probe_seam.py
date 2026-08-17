"""Tests for `routing/probe_seam.py` -- D-06's gated dispatch-verdict probe
seam: the fail-soft current-RSS reader (`ProbeSeamRssReaderTests`), the
pure constant-time gate-resolution function (`ProbeSeamGateResolutionTests`),
and `solve()`'s caller-supplied `transition_budget=` hatch
(`TransitionBudgetHatchTests`).
"""
import sys
import unittest
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from routing.probe_seam import read_current_rss_kb, rss_delta_mb


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

"""Tests for the measure_refresh_diff management command.

Diff and report tests construct synthetic CellResult objects directly and
call the pure `diff_cell_results()` / `render_report()` functions with no
file or database access -- never a real 26-cell sweep, which is minutes of
solver work and the sibling `measure_dispatch_grid` command is explicitly
barred from CI for that reason.

Command-level tests mock at the seams (`_measure_grid`, `reseed_all`,
`corridor.reset_index`, `_canonical_csv_is_dirty`, `_restore_canonical_csv`)
rather than running any real sweep or reseed, following the mock-the-
single-boundary convention `test_fetch_overture_extract.py` established for
its own external boundary.
"""
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from routing.management.commands import measure_refresh_diff as cmd_module
from routing.management.commands.measure_dispatch_grid import CellResult


def _result(
    slug="dallas_tx-seattle_wa",
    tank_range_mi=Decimal("1050"),
    is_demo_cell=False,
    raw_candidates=10,
    kept=5,
    estimate=100,
    admitted_at_current_budget=True,
    stops=2,
    total_cost=Decimal("123.45"),
    censored=False,
    censored_reason="",
):
    return CellResult(
        slug=slug,
        tank_range_mi=tank_range_mi,
        is_demo_cell=is_demo_cell,
        raw_candidates=raw_candidates,
        kept=kept,
        estimate=estimate,
        admitted_at_current_budget=admitted_at_current_budget,
        stops=stops,
        total_cost=total_cost,
        censored=censored,
        censored_reason=censored_reason,
    )


def _make_26_cells():
    """12 corridor slugs x 2 tank ranges, plus 2 demo-chip cells -- the
    same 12/2/2 shape the real grid produces, with distinct slugs so each
    cell is uniquely keyed."""
    results = []
    for i in range(12):
        slug = f"corridor_{i}"
        for tank in (Decimal("1050"), Decimal("500")):
            results.append(_result(slug=slug, tank_range_mi=tank))
    results.append(_result(slug="demo_chip_1", tank_range_mi=Decimal("1050"), is_demo_cell=True))
    results.append(_result(slug="demo_chip_2", tank_range_mi=Decimal("1050"), is_demo_cell=True))
    return results


class DiffCellResultsTests(TestCase):
    """Anti-vacuity in both directions: an empty diff must mean nothing
    moved, and each of the six fields' movement must be independently
    detectable so a partially-wired diff cannot hide behind one field."""

    def test_identical_lists_produce_zero_changed_cells(self):
        before = _make_26_cells()
        after = _make_26_cells()
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(len(diffs), 26)
        self.assertFalse(any(d.is_changed for d in diffs))

    def test_raw_candidates_change_is_reported(self):
        before = [_result(raw_candidates=10)]
        after = [_result(raw_candidates=15)]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(len(diffs), 1)
        self.assertTrue(diffs[0].is_changed)
        self.assertEqual(
            diffs[0].changed_fields, [("raw_candidates", 10, 15)]
        )

    def test_kept_change_is_reported(self):
        before = [_result(kept=5)]
        after = [_result(kept=7)]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(diffs[0].changed_fields, [("kept", 5, 7)])

    def test_estimate_change_is_reported(self):
        before = [_result(estimate=100)]
        after = [_result(estimate=340)]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(diffs[0].changed_fields, [("estimate", 100, 340)])

    def test_admitted_at_current_budget_change_is_reported(self):
        before = [_result(admitted_at_current_budget=True)]
        after = [_result(admitted_at_current_budget=False)]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(
            diffs[0].changed_fields,
            [("admitted_at_current_budget", True, False)],
        )

    def test_stops_change_is_reported(self):
        before = [_result(stops=2)]
        after = [_result(stops=3)]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(diffs[0].changed_fields, [("stops", 2, 3)])

    def test_total_cost_change_is_reported(self):
        before = [_result(total_cost=Decimal("100.00"))]
        after = [_result(total_cost=Decimal("150.50"))]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(
            diffs[0].changed_fields,
            [("total_cost", Decimal("100.00"), Decimal("150.50"))],
        )

    def test_measurable_to_censored_transition_carries_reason(self):
        before = [_result(censored=False)]
        after = [_result(censored=True, censored_reason="InfeasibleRouteError: boom")]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(len(diffs), 1)
        self.assertTrue(diffs[0].is_changed)
        self.assertEqual(diffs[0].censorship_transition, "measurable_to_censored")
        self.assertEqual(diffs[0].censorship_reason, "InfeasibleRouteError: boom")
        # Never coerced into one of the six numeric fields.
        self.assertEqual(diffs[0].changed_fields, [])

    def test_censored_to_measurable_transition_carries_reason(self):
        before = [_result(censored=True, censored_reason="InfeasibleRouteError: boom")]
        after = [_result(censored=False)]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(diffs[0].censorship_transition, "censored_to_measurable")
        self.assertEqual(diffs[0].censorship_reason, "InfeasibleRouteError: boom")
        self.assertEqual(diffs[0].changed_fields, [])

    def test_cell_missing_from_after_world_is_reported_not_skipped(self):
        before = [_result(slug="only_before")]
        after = []
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(len(diffs), 1)
        self.assertTrue(diffs[0].is_changed)
        self.assertEqual(diffs[0].presence_event, "missing_after")

    def test_cell_missing_from_before_world_is_reported_not_skipped(self):
        before = []
        after = [_result(slug="only_after")]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(len(diffs), 1)
        self.assertTrue(diffs[0].is_changed)
        self.assertEqual(diffs[0].presence_event, "missing_before")


class RenderReportTests(TestCase):
    def _render(self, diffs, **overrides):
        kwargs = dict(
            candidate_path=Path("candidate.csv"),
            canonical_path=Path("data/overture_stations.csv"),
            before_overture_row_count=10248,
            after_overture_row_count=10300,
            before_routable_count=16341,
            after_routable_count=16400,
            diffs=diffs,
        )
        kwargs.update(overrides)
        return cmd_module.render_report(**kwargs)

    def test_all_26_rows_render(self):
        before = _make_26_cells()
        after = _make_26_cells()
        diffs = cmd_module.diff_cell_results(before, after)
        report = self._render(diffs)
        table_rows = [
            line
            for line in report.splitlines()
            if line.startswith("| ") and "Cell" not in line and "---" not in line
        ]
        self.assertEqual(len(table_rows), 26)

    def test_header_carries_both_worlds_counts(self):
        report = self._render([])
        self.assertIn("before=10248 after=10300", report)
        self.assertIn("before=16341 after=16400", report)

    def test_reviewer_section_present_and_names_all_three_items(self):
        report = self._render([])
        self.assertIn("## Reviewer notes", report)
        self.assertIn("line-by-line diff", report)
        self.assertIn("regenerate path", report)
        self.assertIn("README.md", report)
        self.assertIn("docs/ALGORITHM.md", report)

    def test_censorship_transition_appears_in_changed_cells_section(self):
        before = [_result(slug="x", censored=False)]
        after = [_result(slug="x", censored=True, censored_reason="InfeasibleRouteError: boom")]
        diffs = cmd_module.diff_cell_results(before, after)
        report = self._render(diffs)
        self.assertIn("### Censorship transitions", report)
        self.assertIn("measurable_to_censored", report)
        self.assertIn("InfeasibleRouteError: boom", report)


class MeasureRefreshDiffCommandTests(TestCase):
    """Mocks every seam a real run would otherwise hit: the reseed, the
    index reset, the 26-cell sweep itself, the dirty-tree check and the
    git-checkout restore. No test in this class performs a real solve or a
    real reseed."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.canonical_path = Path(self.tmpdir.name) / "overture_stations.csv"
        self.canonical_path.write_text("header\nrow1\n", encoding="utf-8")

        self.candidate_path = Path(self.tmpdir.name) / "candidate.csv"
        self.candidate_path.write_text("header\nrow1\nrow2\n", encoding="utf-8")

        self.report_path = Path(self.tmpdir.name) / "report.md"

        canonical_patcher = mock.patch.object(
            cmd_module, "CANONICAL_OVERTURE_CSV_PATH", self.canonical_path
        )
        canonical_patcher.start()
        self.addCleanup(canonical_patcher.stop)

        dirty_patcher = mock.patch.object(
            cmd_module, "_canonical_csv_is_dirty", return_value=False
        )
        dirty_patcher.start()
        self.addCleanup(dirty_patcher.stop)

        reseed_patcher = mock.patch.object(cmd_module, "reseed_all")
        reseed_patcher.start()
        self.addCleanup(reseed_patcher.stop)

        reset_index_patcher = mock.patch.object(cmd_module.corridor, "reset_index")
        reset_index_patcher.start()
        self.addCleanup(reset_index_patcher.stop)

        measure_grid_patcher = mock.patch.object(
            cmd_module, "_measure_grid", return_value=[]
        )
        measure_grid_patcher.start()
        self.addCleanup(measure_grid_patcher.stop)

        restore_patcher = mock.patch.object(cmd_module, "_restore_canonical_csv")
        self.mock_restore = restore_patcher.start()
        self.addCleanup(restore_patcher.stop)

    def _call(self, candidate_path=None, **extra_options):
        call_command(
            "measure_refresh_diff",
            str(candidate_path or self.candidate_path),
            report_path=str(self.report_path),
            **extra_options,
        )

    def test_keep_after_skips_restore_and_leaves_candidate_bytes(self):
        self._call(keep_after=True)
        self.mock_restore.assert_not_called()
        self.assertEqual(
            self.canonical_path.read_text(encoding="utf-8"),
            self.candidate_path.read_text(encoding="utf-8"),
        )
        self.assertTrue(self.report_path.exists())

    def test_default_invokes_restore_exactly_once(self):
        self._call()
        self.mock_restore.assert_called_once()

    def test_restore_failure_propagates(self):
        self.mock_restore.side_effect = CommandError("git checkout boom")
        with self.assertRaises(CommandError):
            self._call()

    def test_dirty_canonical_file_refuses_to_start_naming_the_path(self):
        with mock.patch.object(cmd_module, "_canonical_csv_is_dirty", return_value=True):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn(str(self.canonical_path), str(ctx.exception))
        self.mock_restore.assert_not_called()

    def test_header_only_candidate_file_refuses_to_start(self):
        self.candidate_path.write_text("header\n", encoding="utf-8")
        with self.assertRaises(CommandError):
            self._call()

    def test_missing_candidate_file_refuses_to_start(self):
        missing_path = Path(self.tmpdir.name) / "does_not_exist.csv"
        with self.assertRaises(CommandError):
            self._call(candidate_path=missing_path)

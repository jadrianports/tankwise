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
import ast
import inspect
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from routing.management.commands import measure_dispatch_grid as grid_cmd_module
from routing.management.commands import measure_refresh_diff as cmd_module
from routing.management.commands.measure_dispatch_grid import CellResult


class MeasureCellTrustMarginSignatureTests(SimpleTestCase):
    """D-06 -- guards the sibling `measure_dispatch_grid` command's own
    zero-margin baseline by testing its *signature* and its *own call
    site*, rather than by re-running a real 26-cell before/after
    inertness sweep. That sweep would require running a command whose own
    module docstring bars it from CI ("Must NOT run in CI -- the figures
    this command prints are evidence, not a pass/fail gate"). The
    regression that actually matters -- `_measure_cell`'s `trust_margin`
    default silently moving away from `Decimal(0)`, or the sibling
    command's own call site starting to pass `trust_margin` explicitly --
    is exactly what these two tests catch, and they run in CI on every
    push.
    """

    def test_measure_cell_trust_margin_default_is_decimal_zero(self):
        sig = inspect.signature(grid_cmd_module.Command._measure_cell)
        self.assertIn(
            "trust_margin",
            sig.parameters,
            "_measure_cell must keep a trust_margin parameter",
        )
        default = sig.parameters["trust_margin"].default
        self.assertIsInstance(
            default,
            Decimal,
            f"trust_margin's default must be a Decimal, found {default!r} "
            f"({type(default)!r})",
        )
        self.assertEqual(
            default,
            Decimal(0),
            f"trust_margin's default drifted from Decimal(0) to {default!r}",
        )

    def test_measure_dispatch_grid_own_call_passes_no_trust_margin(self):
        path = Path(grid_cmd_module.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_measure_cell"
        ]
        self.assertTrue(
            calls,
            f"expected at least one self._measure_cell(...) call in {path}, "
            "found none -- this test would otherwise pass vacuously "
            "against a file that no longer calls it",
        )
        offending_lines = [
            node.lineno
            for node in calls
            if any(kw.arg == "trust_margin" for kw in node.keywords)
        ]
        self.assertEqual(
            offending_lines,
            [],
            f"{path}: _measure_cell call(s) at line(s) {offending_lines} "
            "pass trust_margin= explicitly -- the sibling command's own "
            "invocation must stay byte-identical so every figure it pins "
            "stays unchanged",
        )


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
        # baseline_diffs defaults to the same list passed as
        # production_diffs, so these four existing tests keep their
        # original one-world intent; TwoMarginWorldRenderTests below
        # covers the genuinely-two-world cases.
        kwargs = dict(
            candidate_path=Path("candidate.csv"),
            canonical_path=Path("data/overture_stations.csv"),
            before_overture_row_count=10248,
            after_overture_row_count=10300,
            before_routable_count=16341,
            after_routable_count=16400,
            production_diffs=diffs,
            baseline_diffs=diffs,
            production_trust_margin=Decimal("5.47"),
            baseline_trust_margin=Decimal("0"),
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


class TwoMarginWorldRenderTests(TestCase):
    """D-01/D-09/D-10/D-11 -- both margin worlds render in the per-cell
    table, the report states its own measurement basis above that table,
    and render_report stays one unforked function returning one string."""

    def _render(self, production_diffs, baseline_diffs, **overrides):
        kwargs = dict(
            candidate_path=Path("candidate.csv"),
            canonical_path=Path("data/overture_stations.csv"),
            before_overture_row_count=10248,
            after_overture_row_count=10300,
            before_routable_count=16341,
            after_routable_count=16400,
            production_diffs=production_diffs,
            baseline_diffs=baseline_diffs,
            production_trust_margin=Decimal("5.47"),
            baseline_trust_margin=Decimal("0"),
        )
        kwargs.update(overrides)
        return cmd_module.render_report(**kwargs)

    def _measurement_basis_section(self, report):
        return report[
            report.index("## Measurement basis") : report.index("## Per-cell table")
        ]

    def test_omitting_baseline_diffs_raises_type_error(self):
        with self.assertRaises(TypeError):
            cmd_module.render_report(
                candidate_path=Path("candidate.csv"),
                canonical_path=Path("data/overture_stations.csv"),
                before_overture_row_count=10248,
                after_overture_row_count=10300,
                before_routable_count=16341,
                after_routable_count=16400,
                production_diffs=[],
                production_trust_margin=Decimal("5.47"),
                baseline_trust_margin=Decimal("0"),
            )

    def test_measurement_basis_renders_above_per_cell_table(self):
        report = self._render([], [])
        self.assertLess(
            report.index("## Measurement basis"),
            report.index("## Per-cell table"),
        )

    def test_measurement_basis_names_both_margin_values_not_hardcoded(self):
        report = self._render([], [], production_trust_margin=Decimal("9.99"))
        section = self._measurement_basis_section(report)
        self.assertIn("9.99", section)

    def test_measurement_basis_states_which_set_drives_the_verdict(self):
        report = self._render([], [])
        section = self._measurement_basis_section(report)
        self.assertIn("production column set alone", section)

    def test_measurement_basis_names_margin_independent_and_movable_fields(self):
        report = self._render([], [])
        section = self._measurement_basis_section(report)
        for field_name in (
            "raw_candidates",
            "kept",
            "estimate",
            "admitted_at_current_budget",
            "stops",
            "total_cost",
        ):
            self.assertIn(field_name, section)

    def test_measurement_basis_states_bias_direction(self):
        report = self._render([], [])
        section = self._measurement_basis_section(report)
        self.assertIn("eia_regional_estimate", section)
        self.assertIn("overstate", section)

    def test_table_header_has_fifteen_columns_with_margin_labels(self):
        report = self._render(
            [],
            [],
            production_trust_margin=Decimal("5.47"),
            baseline_trust_margin=Decimal("0"),
        )
        header_line = next(
            line for line in report.splitlines() if line.startswith("| Cell |")
        )
        columns = [c for c in header_line.split("|") if c.strip() != ""]
        self.assertEqual(len(columns), 15)
        self.assertIn("Stops @$5.47", header_line)
        self.assertIn("Stops @$0", header_line)

    def test_row_shows_both_worlds_and_changed_marker_follows_production_only(self):
        production_diffs = cmd_module.diff_cell_results(
            [_result(slug="a", tank_range_mi=Decimal("1050"), stops=2)],
            [_result(slug="a", tank_range_mi=Decimal("1050"), stops=2)],
        )
        baseline_diffs = cmd_module.diff_cell_results(
            [_result(slug="a", tank_range_mi=Decimal("1050"), stops=2)],
            [_result(slug="a", tank_range_mi=Decimal("1050"), stops=5)],
        )
        self.assertFalse(production_diffs[0].is_changed)
        self.assertTrue(baseline_diffs[0].is_changed)

        report = self._render(production_diffs, baseline_diffs)
        row_line = next(
            line for line in report.splitlines() if line.startswith("| a | 1050")
        )
        # The baseline world's stops movement is visible in the row...
        self.assertIn("2->5", row_line)
        # ...but the CHANGED marker follows the production world only,
        # which did not move.
        self.assertNotIn("CHANGED", row_line)

    def test_cell_present_in_one_world_and_absent_from_other_still_renders(self):
        production_diffs = cmd_module.diff_cell_results(
            [_result(slug="only_production", tank_range_mi=Decimal("1050"))],
            [_result(slug="only_production", tank_range_mi=Decimal("1050"))],
        )
        report = self._render(production_diffs, [])
        row_line = next(
            line
            for line in report.splitlines()
            if line.startswith("| only_production |")
        )
        self.assertIn("(absent)", row_line)


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
        self.mock_reseed_all = reseed_patcher.start()
        self.addCleanup(reseed_patcher.stop)

        reset_index_patcher = mock.patch.object(cmd_module.corridor, "reset_index")
        reset_index_patcher.start()
        self.addCleanup(reset_index_patcher.stop)

        measure_grid_patcher = mock.patch.object(
            cmd_module, "_measure_grid", return_value=[]
        )
        self.mock_measure_grid = measure_grid_patcher.start()
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


class MeasureGridMarginForwardingTests(TestCase):
    """Unit tests against the real `_measure_grid` function itself (no
    command-level mocking) -- `_build_grid(None)` touches no database, so
    a Mock `grid_command` is enough to prove the required-keyword and
    forwarding behaviour without a real 26-cell sweep."""

    def test_calling_without_trust_margin_raises_type_error(self):
        with self.assertRaises(TypeError):
            cmd_module._measure_grid(mock.Mock(), 1)

    def test_trust_margin_is_forwarded_to_every_cell_call(self):
        grid_command = mock.Mock()
        cmd_module._measure_grid(grid_command, 1, trust_margin=Decimal("3.21"))
        self.assertTrue(grid_command._measure_cell.call_args_list)
        for call in grid_command._measure_cell.call_args_list:
            self.assertEqual(call.kwargs.get("trust_margin"), Decimal("3.21"))


class FourSweepMarginWiringTests(MeasureRefreshDiffCommandTests):
    """D-02/D-03/D-04 -- handle()'s four-sweep flow against two margin
    worlds and two reseeds. Extends MeasureRefreshDiffCommandTests's own
    six-patcher setUp; assertions inspect `self.mock_measure_grid`'s
    `call_args_list` rather than `assert_called_with`, since the mock has
    no per-call return differentiation by default."""

    def test_handle_calls_measure_grid_four_times_at_two_distinct_margins(self):
        self._call()
        self.assertEqual(self.mock_measure_grid.call_count, 4)
        margins = [
            call.kwargs.get("trust_margin")
            for call in self.mock_measure_grid.call_args_list
        ]
        self.assertEqual(len(margins), 4)
        distinct = set(margins)
        self.assertEqual(len(distinct), 2)
        for margin in distinct:
            self.assertEqual(margins.count(margin), 2)

    def test_handle_calls_reseed_all_exactly_twice(self):
        self._call()
        self.assertEqual(self.mock_reseed_all.call_count, 2)

    def test_production_margin_is_read_live_from_settings(self):
        with override_settings(TRUST_MARGIN_USD=Decimal("9.99")):
            self._call()
        margins = [
            call.kwargs.get("trust_margin")
            for call in self.mock_measure_grid.call_args_list
        ]
        self.assertEqual(margins.count(Decimal("9.99")), 2)

    def test_baseline_margin_is_zero(self):
        self._call()
        margins = [
            call.kwargs.get("trust_margin")
            for call in self.mock_measure_grid.call_args_list
        ]
        self.assertEqual(margins.count(Decimal(0)), 2)

    def test_before_sweeps_run_before_swap_and_after_sweeps_run_after(self):
        call_order = []

        def _record_measure_grid(*args, **kwargs):
            call_order.append(("measure_grid", kwargs.get("trust_margin")))
            return []

        self.mock_measure_grid.side_effect = _record_measure_grid

        real_copyfile = cmd_module.shutil.copyfile

        def _record_copyfile(*args, **kwargs):
            call_order.append(("copyfile",))
            return real_copyfile(*args, **kwargs)

        with mock.patch.object(
            cmd_module.shutil, "copyfile", side_effect=_record_copyfile
        ):
            self._call()

        self.assertEqual(len(call_order), 5)  # 4 measure_grid calls + 1 copyfile
        copyfile_index = next(
            i for i, event in enumerate(call_order) if event[0] == "copyfile"
        )
        # Exactly two BEFORE sweeps precede the swap, two AFTER sweeps follow it.
        self.assertEqual(copyfile_index, 2)

    def test_changed_count_and_sections_come_from_production_diff_only(self):
        production_row = _result(slug="corridor_0", tank_range_mi=Decimal("1050"))
        baseline_before = _result(
            slug="corridor_0", tank_range_mi=Decimal("1050"), stops=2
        )
        baseline_after = _result(
            slug="corridor_0", tank_range_mi=Decimal("1050"), stops=3
        )
        baseline_calls = {"count": 0}

        def _side_effect(grid_command, repeats, *, trust_margin):
            if trust_margin == settings.TRUST_MARGIN_USD:
                return [production_row]
            baseline_calls["count"] += 1
            return [baseline_before] if baseline_calls["count"] == 1 else [baseline_after]

        self.mock_measure_grid.side_effect = _side_effect
        self._call()

        report_text = self.report_path.read_text(encoding="utf-8")
        self.assertIn("Changed cells: 0", report_text)
        self.assertIn("(none)", report_text)

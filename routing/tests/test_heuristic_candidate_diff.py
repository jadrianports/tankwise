"""Guard tests for `measure_heuristic_candidate_diff.py` (D-16/D-17, plan
26-02).

Five assertions, per `26-02-PLAN.md` Task 2:

1. Cell-set identity: `HEURISTIC_DIFF_CELLS` equals exactly
   `ADMISSION_MANIFEST`'s False-valued keys, length `DEMOTED_CELL_COUNT`.
2. Structural bypass: an AST walk proves the command module imports
   neither `routing.services.solver` nor the name `solve`, and calls
   `heuristic.solve_penalty_aware_heuristic` at least twice.
3. Objective arithmetic: `plan_objective` equals `total_cost +
   settings.FUEL_STOP_PENALTY_USD * stops` on hand-checked inputs,
   including a zero-stop plan.
4. Trust-margin discipline: both `handle()`'s own forwarding and the
   rendered report's basis line carry `settings.TRUST_MARGIN_USD`, never
   a zero.
5. Anti-vacuity (Pitfall 3): on a real demoted cell, World A's and World
   B's candidate-list lengths genuinely differ.
"""
import ast
import io
import pathlib
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from routing.management.commands import measure_heuristic_candidate_diff as cmd_module
from routing.services import corridor
from routing.tests.test_dispatch_recovery import DEMOTED_CELL_COUNT
from routing.tests.test_solver_dispatch import ADMISSION_MANIFEST

_COMMAND_PATH = pathlib.Path(cmd_module.__file__)

# The cheapest real demoted cell by raw candidate count (measured
# directly: 220 raw candidates, the smallest of the 14 -- see this
# module's own SUMMARY for the measured wall-clock), used for both the
# trust-margin-discipline test and the anti-vacuity test so the test
# module stays fast.
_CHEAP_CELL_SLUG = "atlanta_ga-denver_co"
_CHEAP_CELL_TANK = 500


# ---------------------------------------------------------------------------
# 1. Cell-set identity.
# ---------------------------------------------------------------------------
class CellSetIdentityTests(SimpleTestCase):
    def test_heuristic_diff_cells_matches_the_demoted_manifest_entries(self):
        expected = tuple(
            (slug, tank_range_mi)
            for (slug, tank_range_mi), admitted in ADMISSION_MANIFEST.items()
            if not admitted
        )
        self.assertEqual(cmd_module.HEURISTIC_DIFF_CELLS, expected)

    def test_heuristic_diff_cells_length_matches_demoted_cell_count(self):
        self.assertEqual(len(cmd_module.HEURISTIC_DIFF_CELLS), DEMOTED_CELL_COUNT)


# ---------------------------------------------------------------------------
# 2. Structural bypass of solve() -- AST walk, reusing test_boundaries.py's
# tree-based-core-plus-file-reading-wrapper shape so the mutation check
# below can exercise the core function on an in-memory violating source
# without ever writing to disk.
# ---------------------------------------------------------------------------
def _collect_import_names_from_tree(tree):
    """Every import this module makes, as FULLY QUALIFIED names -- for
    `ast.Import`, each `alias.name` as-is; for `ast.ImportFrom`, both the
    bare `module` (so a caller checking for a whole-package import still
    works) AND `f"{module}.{alias.name}"` for each imported name. The
    fully-qualified form is load-bearing: `test_boundaries.py`'s own
    `_collect_import_names` records only the bare `module` for
    `ImportFrom`, which would silently miss a mutation shaped
    `from routing.services import solver` (as opposed to `import
    routing.services.solver`) -- confirmed directly below by
    `test_mutation_check_the_walker_catches_a_genuine_solver_import`,
    which exercises exactly that shape.
    """
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
    return names


def _collect_import_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _collect_import_names_from_tree(tree)


def _collect_solve_call_hits_from_tree(tree, label):
    """Every bare `solve(...)` or `<anything>.solve(...)` call node --
    the same two shapes `test_boundaries.py`'s own
    `_collect_solve_calls_missing_kwarg` walker treats as a target
    `solve()` call (minus that walker's `naive_baseline` exemption,
    which does not apply here since this module never imports
    `naive_baseline` at all)."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "solve":
            hits.append(f"{label}:{node.lineno}: bare solve(...) call")
        elif isinstance(func, ast.Attribute) and func.attr == "solve":
            hits.append(f"{label}:{node.lineno}: <expr>.solve(...) call")
    return hits


def _collect_solve_call_hits(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _collect_solve_call_hits_from_tree(tree, str(path))


def _count_heuristic_calls_from_tree(tree):
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "solve_penalty_aware_heuristic"
        ):
            count += 1
    return count


class StructuralBypassTests(SimpleTestCase):
    def test_module_never_imports_routing_services_solver(self):
        imports = _collect_import_names(_COMMAND_PATH)
        self.assertNotIn(
            "routing.services.solver",
            imports,
            "measure_heuristic_candidate_diff.py must never import "
            "routing.services.solver -- solve() hands its heuristic call "
            "sites the FULL unpruned candidates in every case, so a "
            "harness composed through it would measure nothing "
            "(RESEARCH.md Pitfall 3).",
        )

    def test_module_never_calls_a_solve_shaped_function(self):
        hits = _collect_solve_call_hits(_COMMAND_PATH)
        self.assertEqual(
            hits,
            [],
            f"found solve()-shaped call(s), which must not exist here: {hits}",
        )

    def test_both_worlds_call_solve_penalty_aware_heuristic_at_least_twice(self):
        tree = ast.parse(
            _COMMAND_PATH.read_text(encoding="utf-8"), filename=str(_COMMAND_PATH)
        )
        count = _count_heuristic_calls_from_tree(tree)
        self.assertGreaterEqual(
            count,
            2,
            "expected at least 2 heuristic.solve_penalty_aware_heuristic "
            f"call sites (one per world), found {count}",
        )

    def test_mutation_check_the_walker_catches_a_genuine_solver_import(self):
        """Non-vacuity: parsing an in-memory copy of the module's own
        source with `from routing.services.solver import solve` prepended
        must make `_collect_import_names_from_tree` report the import AND
        `_collect_solve_call_hits_from_tree` remain silent about it (the
        import alone, with no call, is exactly what `test_module_never_
        imports_routing_services_solver` above exists to catch) -- proving
        that assertion is not vacuously passing on this module. Never
        written to disk; parsed directly from a string, mirroring
        `PruneInertnessGateTest.test_walker_reports_a_real_violation_and_
        nothing_on_clean_source`'s own in-memory mutation shape.
        """
        clean_source = _COMMAND_PATH.read_text(encoding="utf-8")
        # The `from routing.services import solver` shape, not `import
        # routing.services.solver` -- the harder case, since the bare
        # `node.module` alone ("routing.services") would NOT reveal it;
        # only the fully-qualified `module.alias` form
        # `_collect_import_names_from_tree` builds catches this.
        violating_source = (
            "from routing.services import solver\n" + clean_source
        )

        clean_tree = ast.parse(clean_source, filename="<clean>")
        violating_tree = ast.parse(violating_source, filename="<violating>")

        clean_imports = _collect_import_names_from_tree(clean_tree)
        violating_imports = _collect_import_names_from_tree(violating_tree)

        self.assertNotIn("routing.services.solver", clean_imports)
        self.assertIn("routing.services.solver", violating_imports)


# ---------------------------------------------------------------------------
# 3. Objective arithmetic.
# ---------------------------------------------------------------------------
class PlanObjectiveArithmeticTests(SimpleTestCase):
    def test_plan_objective_matches_the_hand_checked_formula(self):
        penalty = settings.FUEL_STOP_PENALTY_USD
        self.assertEqual(
            cmd_module.plan_objective(Decimal("241.97"), 3),
            Decimal("241.97") + penalty * 3,
        )

    def test_plan_objective_on_a_zero_stop_plan(self):
        penalty = settings.FUEL_STOP_PENALTY_USD
        self.assertEqual(
            cmd_module.plan_objective(Decimal("0.00"), 0),
            Decimal("0.00") + penalty * 0,
        )
        self.assertEqual(cmd_module.plan_objective(Decimal("0.00"), 0), Decimal("0.00"))

    @override_settings(FUEL_STOP_PENALTY_USD=Decimal("50"))
    def test_plan_objective_reads_the_penalty_live_from_settings(self):
        self.assertEqual(
            cmd_module.plan_objective(Decimal("100.00"), 2),
            Decimal("100.00") + Decimal("50") * 2,
        )
        self.assertEqual(cmd_module.plan_objective(Decimal("100.00"), 2), Decimal("200.00"))


# ---------------------------------------------------------------------------
# 4. Trust-margin discipline.
# ---------------------------------------------------------------------------
class TrustMarginForwardingTests(SimpleTestCase):
    """Confirms `handle()`'s own first lines
    (`trust_margin = settings.TRUST_MARGIN_USD`) actually reach
    `_measure_cell` -- mocked so this runs with no real DB/solve work at
    all."""

    def setUp(self):
        reseed_patcher = mock.patch.object(cmd_module, "reseed_all")
        self.mock_reseed_all = reseed_patcher.start()
        self.addCleanup(reseed_patcher.stop)

        reset_index_patcher = mock.patch.object(cmd_module.corridor, "reset_index")
        reset_index_patcher.start()
        self.addCleanup(reset_index_patcher.stop)

        measure_cell_patcher = mock.patch.object(cmd_module, "_measure_cell")
        self.mock_measure_cell = measure_cell_patcher.start()
        self.mock_measure_cell.return_value = cmd_module.CellDiff(
            slug="stub",
            tank_range_mi=Decimal(500),
            raw_candidates=1,
            world_b_search_set=1,
            stops_a=1,
            stops_b=1,
            total_cost_a=Decimal("1.00"),
            total_cost_b=Decimal("1.00"),
            objective_a=Decimal("36.00"),
            objective_b=Decimal("36.00"),
            objective_delta=Decimal("0.00"),
            verdict="SAME",
        )
        self.addCleanup(measure_cell_patcher.stop)

    @override_settings(TRUST_MARGIN_USD=Decimal("9.99"))
    def test_handle_forwards_the_live_trust_margin_to_every_measure_cell_call(self):
        call_command("measure_heuristic_candidate_diff")

        self.assertEqual(
            self.mock_measure_cell.call_count, len(cmd_module.HEURISTIC_DIFF_CELLS)
        )
        for call in self.mock_measure_cell.call_args_list:
            self.assertEqual(call.kwargs["trust_margin"], Decimal("9.99"))
            self.assertNotEqual(call.kwargs["trust_margin"], Decimal(0))


class RealCorridorTestCase(TestCase):
    """Seeds the real, committed station dataset once per class --
    mirrors `test_solver_dispatch.RealCorridorDispatchTestCase`'s own
    `setUpTestData` shape."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()


@override_settings(TRUST_MARGIN_USD=Decimal("9.99"))
class TrustMarginReportBasisTests(RealCorridorTestCase):
    def test_report_basis_carries_the_overridden_margin_not_zero(self):
        row = cmd_module._row_for_cell(_CHEAP_CELL_SLUG, _CHEAP_CELL_TANK)
        diff = cmd_module._measure_cell(
            row,
            penalty=settings.FUEL_STOP_PENALTY_USD,
            trust_margin=settings.TRUST_MARGIN_USD,
        )
        report = cmd_module.render_report(
            diffs=[diff],
            penalty=settings.FUEL_STOP_PENALTY_USD,
            trust_margin=settings.TRUST_MARGIN_USD,
            git_sha="test-sha",
        )
        basis_section = report[report.index("## Measurement basis") :]
        self.assertIn("9.99", basis_section)
        self.assertNotIn("$0)", basis_section)
        self.assertNotIn("$0.00)", basis_section)


# ---------------------------------------------------------------------------
# 5. Anti-vacuity (Pitfall 3): World A and World B genuinely differ on a
# real cell.
# ---------------------------------------------------------------------------
class AntiVacuityTests(RealCorridorTestCase):
    def test_world_a_and_world_b_candidate_counts_genuinely_differ(self):
        row = cmd_module._row_for_cell(_CHEAP_CELL_SLUG, _CHEAP_CELL_TANK)
        diff = cmd_module._measure_cell(
            row,
            penalty=settings.FUEL_STOP_PENALTY_USD,
            trust_margin=settings.TRUST_MARGIN_USD,
        )
        self.assertNotEqual(
            diff.raw_candidates,
            diff.world_b_search_set,
            f"{_CHEAP_CELL_SLUG}@{_CHEAP_CELL_TANK}mi: World A "
            f"({diff.raw_candidates} candidates) and World B "
            f"({diff.world_b_search_set} candidates) are identical -- "
            "the harness is varying nothing (Pitfall 3), so every "
            "downstream number would be meaningless.",
        )

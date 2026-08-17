"""Tests for the measure_prune_dispatch_diff management command.

Every test in this module is synthetic or mocked -- no real 26-cell sweep
ever runs, following the same convention `test_measure_refresh_diff.py`
established for its own sibling command: `_measure_sweep()` (both worlds)
and `_attribute_cell()` are the two seams command-level tests mock;
render/gate/diff tests construct synthetic `CellDiff`/`CellResult` objects
directly and call the pure `diff_cell_results()` / `_check_plan_identity()`
/ `render_report()` functions with no file or database access.

`plateau_verdict()` requires EXACTLY `DEMOTED_CELL_COUNT` (14)
`was_demoted=True` rows, computed from the real `ADMISSION_MANIFEST` --
not a synthetic 12/2/2 shape -- so every 26-cell fixture in this module is
built by iterating `ADMISSION_MANIFEST.items()` directly rather than
inventing placeholder slugs.
"""
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from routing.management.commands import measure_prune_dispatch_diff as cmd_module
from routing.management.commands.measure_dispatch_grid import CellResult, _build_grid
from routing.tests.test_dispatch_recovery import DEMOTED_CELL_COUNT, plateau_verdict
from routing.tests.test_solver_dispatch import ADMISSION_MANIFEST


def _result(
    slug="dallas_tx-seattle_wa",
    tank_range_mi=Decimal("500"),
    is_demo_cell=False,
    raw_candidates=10,
    kept=5,
    estimate=100,
    admitted_at_current_budget=True,
    untimed_strategy="exact_dp",
    timed_strategy="exact_dp",
    stops=2,
    total_cost=Decimal("123.45"),
    stop_opis_ids=(1, 2),
    censored=False,
    censored_reason="",
):
    """`untimed_strategy` defaults to the exact-DP arm string, mirroring
    `timed_strategy`'s existing default -- additive, so every pre-existing
    fixture becomes an explicit exact-arm cell rather than an implicit
    one; no existing assertion's grounds move."""
    return CellResult(
        slug=slug,
        tank_range_mi=tank_range_mi,
        is_demo_cell=is_demo_cell,
        raw_candidates=raw_candidates,
        kept=kept,
        estimate=estimate,
        admitted_at_current_budget=admitted_at_current_budget,
        untimed_strategy=untimed_strategy,
        timed_strategy=timed_strategy,
        stops=stops,
        total_cost=total_cost,
        stop_opis_ids=stop_opis_ids,
        censored=censored,
        censored_reason=censored_reason,
    )


def _diff(
    slug="x",
    tank_range_mi=Decimal("500"),
    before_kwargs=None,
    after_kwargs=None,
    penalty_dominated=0,
):
    before_kwargs = before_kwargs or {}
    after_kwargs = after_kwargs or {}
    before = _result(slug=slug, tank_range_mi=tank_range_mi, **before_kwargs)
    after = _result(slug=slug, tank_range_mi=tank_range_mi, **after_kwargs)
    return cmd_module.CellDiff(
        slug=slug,
        tank_range_mi=tank_range_mi,
        before=before,
        after=after,
        penalty_dominated=penalty_dominated,
    )


def _make_26_diffs(recovered_slugs=()):
    """Build a full 26-cell `CellDiff` list keyed on `ADMISSION_MANIFEST`'s
    own `(slug, tank_range_mi)` pairs -- required because `plateau_verdict`
    demands exactly `DEMOTED_CELL_COUNT` `was_demoted=True` rows, computed
    from the real manifest, not a synthetic shape.

    `before` always mirrors the real manifest's own admission state.
    `after` mirrors it too, EXCEPT for any `(slug, tank)` pair named in
    `recovered_slugs`, whose after-world is flipped to admitted -- a
    genuine arm change, correctly exempt from the criterion-4 identity
    gate regardless of cost movement."""
    diffs = []
    for (slug, tank_int), was_admitted in ADMISSION_MANIFEST.items():
        tank = Decimal(tank_int)
        before = _result(
            slug=slug,
            tank_range_mi=tank,
            estimate=100 if was_admitted else 999_999,
            admitted_at_current_budget=was_admitted,
            untimed_strategy="exact_dp" if was_admitted else "penalty_aware_heuristic",
            timed_strategy="exact_dp" if was_admitted else "penalty_aware_heuristic",
        )
        recovered = (slug, tank_int) in recovered_slugs
        after_admitted = was_admitted or recovered
        after = _result(
            slug=slug,
            tank_range_mi=tank,
            estimate=100 if after_admitted else 999_999,
            admitted_at_current_budget=after_admitted,
            untimed_strategy="exact_dp" if after_admitted else "penalty_aware_heuristic",
            timed_strategy="exact_dp" if after_admitted else "penalty_aware_heuristic",
        )
        diffs.append(cmd_module.CellDiff(slug=slug, tank_range_mi=tank, before=before, after=after))
    return diffs


# ---------------------------------------------------------------------------
# 1. Grid coverage -- D-18's coverage argument made checkable.
# ---------------------------------------------------------------------------
class MeasureSweepSeamTests(SimpleTestCase):
    """`_measure_sweep` is the ONE mockable seam command-level tests patch.
    These tests exercise the REAL function against a mocked grid_command,
    never a real sweep."""

    def test_build_grid_returns_26_rows(self):
        self.assertEqual(len(_build_grid(None)), 26)

    def test_measure_sweep_requires_trust_margin_and_strengthened_prune_keywords(self):
        grid_command = mock.Mock()
        with self.assertRaises(TypeError):
            cmd_module._measure_sweep(grid_command, 1)
        with self.assertRaises(TypeError):
            cmd_module._measure_sweep(grid_command, 1, trust_margin=Decimal(0))
        with self.assertRaises(TypeError):
            cmd_module._measure_sweep(grid_command, 1, strengthened_prune=False)

    def test_measure_sweep_covers_all_26_rows_per_world(self):
        grid_command = mock.Mock()
        grid_command._measure_cell.return_value = _result()

        before = cmd_module._measure_sweep(
            grid_command, 1, trust_margin=Decimal("5.47"), strengthened_prune=False
        )
        after = cmd_module._measure_sweep(
            grid_command, 1, trust_margin=Decimal("5.47"), strengthened_prune=True
        )

        self.assertEqual(len(before), 26)
        self.assertEqual(len(after), 26)
        self.assertEqual(grid_command._measure_cell.call_count, 52)

    def test_measure_sweep_forwards_strengthened_prune_and_trust_margin(self):
        grid_command = mock.Mock()
        grid_command._measure_cell.return_value = _result()

        cmd_module._measure_sweep(
            grid_command, 1, trust_margin=Decimal("5.47"), strengthened_prune=True
        )

        _, kwargs = grid_command._measure_cell.call_args
        self.assertEqual(kwargs["trust_margin"], Decimal("5.47"))
        self.assertIs(kwargs["strengthened_prune"], True)


# ---------------------------------------------------------------------------
# Command-level tests: everything mocked, no real DB/solve() work.
# ---------------------------------------------------------------------------
def _manifest_sweep_side_effect(grid_command, repeats, *, trust_margin, strengthened_prune):
    return [
        _result(
            slug=slug,
            tank_range_mi=Decimal(tank),
            estimate=100 if was_admitted else 999_999,
            admitted_at_current_budget=was_admitted,
            untimed_strategy="exact_dp" if was_admitted else "penalty_aware_heuristic",
            timed_strategy="exact_dp" if was_admitted else "penalty_aware_heuristic",
        )
        for (slug, tank), was_admitted in ADMISSION_MANIFEST.items()
    ]


class MeasurePruneDispatchDiffCommandTests(TestCase):
    """Shared setUp for every command-level (`call_command`) test: mocks
    `reseed_all`, `corridor.reset_index`, `_measure_sweep` (returning the
    SAME before/after CellResults for every cell by default, so
    `_check_plan_identity` passes cleanly) and `_attribute_cell` (returning
    an inert `(0, 0, 0, 0)` four-tuple)."""

    def setUp(self):
        reseed_patcher = mock.patch.object(cmd_module, "reseed_all")
        self.mock_reseed_all = reseed_patcher.start()
        self.addCleanup(reseed_patcher.stop)

        reset_index_patcher = mock.patch.object(cmd_module.corridor, "reset_index")
        self.mock_reset_index = reset_index_patcher.start()
        self.addCleanup(reset_index_patcher.stop)

        sweep_patcher = mock.patch.object(cmd_module, "_measure_sweep")
        self.mock_measure_sweep = sweep_patcher.start()
        self.mock_measure_sweep.side_effect = _manifest_sweep_side_effect
        self.addCleanup(sweep_patcher.stop)

        attribute_patcher = mock.patch.object(
            cmd_module, "_attribute_cell", return_value=(0, 0, 0, 0)
        )
        self.mock_attribute_cell = attribute_patcher.start()
        self.addCleanup(attribute_patcher.stop)


# ---------------------------------------------------------------------------
# 2. Production margin, both worlds.
# 3. Rule selection: before=False, after=True, never swapped.
# ---------------------------------------------------------------------------
@override_settings(TRUST_MARGIN_USD=Decimal("9.99"))
class MarginAndRuleWiringTests(MeasurePruneDispatchDiffCommandTests):
    def test_production_margin_is_forwarded_to_both_worlds_never_zero(self):
        call_command("measure_prune_dispatch_diff")

        self.assertEqual(self.mock_measure_sweep.call_count, 2)
        for call in self.mock_measure_sweep.call_args_list:
            self.assertEqual(call.kwargs["trust_margin"], Decimal("9.99"))
            self.assertNotEqual(call.kwargs["trust_margin"], Decimal(0))

    def test_rule_selection_is_before_false_then_after_true(self):
        call_command("measure_prune_dispatch_diff")

        calls = self.mock_measure_sweep.call_args_list
        self.assertIs(calls[0].kwargs["strengthened_prune"], False)
        self.assertIs(calls[1].kwargs["strengthened_prune"], True)


# ---------------------------------------------------------------------------
# 4. The basis section is honest.
# ---------------------------------------------------------------------------
class RenderReportMeasurementBasisTests(SimpleTestCase):
    def _basis_section(self, report):
        return report[report.index("## Measurement basis") : report.index("## Per-cell table")]

    def test_basis_section_names_the_margin_value(self):
        report = cmd_module.render_report(
            production_trust_margin=Decimal("7.77"), diffs=_make_26_diffs()
        )
        section = self._basis_section(report)
        self.assertIn("7.77", section)

    def test_basis_section_states_after_world_does_not_route_through_solve(self):
        report = cmd_module.render_report(
            production_trust_margin=Decimal("5.47"), diffs=_make_26_diffs()
        )
        section = self._basis_section(report)
        self.assertIn("NEVER by routing.services.solver.solve()", section)

    def test_basis_section_states_the_preflight_ordering_deviation(self):
        report = cmd_module.render_report(
            production_trust_margin=Decimal("5.47"), diffs=_make_26_diffs()
        )
        section = self._basis_section(report)
        self.assertIn("dp.preflight_gap_check", section)
        self.assertIn("prune=False", section)

    def test_basis_section_does_not_inherit_the_superseded_margin_independence_claim(self):
        report = cmd_module.render_report(
            production_trust_margin=Decimal("5.47"), diffs=_make_26_diffs()
        )
        section = self._basis_section(report)
        self.assertNotIn(
            "prune_dominated_candidates takes no trust-margin argument at all",
            section,
            "the measurement basis section must not inherit measure_refresh_"
            "diff.py's now-superseded margin-independence causal claim -- "
            "that is the specific drift this test exists to catch",
        )

    def test_basis_section_states_the_heuristic_arm_identity_limitation(self):
        """[Amended 2026-08-18, Phase 25 gap closure G1] Pins the
        correction into the rendered artifact itself, so a reader of the
        report -- who may never open the source -- cannot take criterion
        4 as having full coverage."""
        report = cmd_module.render_report(
            production_trust_margin=Decimal("5.47"), diffs=_make_26_diffs()
        )
        section = self._basis_section(report)
        self.assertIn(
            "Plan identity is NOT checkable on heuristic-arm cells by "
            "this method",
            section,
        )
        self.assertIn(
            "the heuristic arm receives the FULL unpruned candidate "
            "list, never the pruned search set, while this harness swaps "
            "that very argument between worlds",
            section,
        )
        self.assertIn("EXACT-ARM subset of the grid", section)
        # Additive, not a replacement -- the pre-existing production-
        # trust-margin claim from test_basis_section_names_the_margin_
        # value must still be present.
        self.assertIn("5.47", section)


# ---------------------------------------------------------------------------
# 5. Criterion 4's gate fires (cost differs; stop identity differs).
# 6. Criterion 4's gate is correctly scoped (arm change is exempt; a
#    penalty-dominated removal is exempt -- the empirically-discovered
#    scenario this plan's own deviation documents).
# ---------------------------------------------------------------------------
class CheckPlanIdentityGateTests(SimpleTestCase):
    def test_gate_fires_on_differing_total_cost_same_arm(self):
        d = _diff(
            before_kwargs=dict(total_cost=Decimal("100.00"), stop_opis_ids=(1, 2)),
            after_kwargs=dict(total_cost=Decimal("105.00"), stop_opis_ids=(1, 2)),
        )
        with self.assertRaises(CommandError) as ctx:
            cmd_module._check_plan_identity([d])
        self.assertIn("x @500mi", str(ctx.exception))
        self.assertIn("correctness bug, not an optimization", str(ctx.exception))

    def test_gate_fires_on_differing_stop_opis_ids_same_cost(self):
        d = _diff(
            before_kwargs=dict(total_cost=Decimal("100.00"), stop_opis_ids=(1, 2)),
            after_kwargs=dict(total_cost=Decimal("100.00"), stop_opis_ids=(1, 3)),
        )
        with self.assertRaises(CommandError) as ctx:
            cmd_module._check_plan_identity([d])
        self.assertIn("x @500mi", str(ctx.exception))

    def test_gate_does_not_fire_when_dispatch_arm_changed(self):
        d = _diff(
            before_kwargs=dict(
                admitted_at_current_budget=False,
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("100.00"),
                stop_opis_ids=(1, 2),
            ),
            after_kwargs=dict(
                admitted_at_current_budget=True,
                timed_strategy="exact_dp",
                total_cost=Decimal("50.00"),
                stop_opis_ids=(9,),
            ),
        )
        cmd_module._check_plan_identity([d])  # must not raise

    def test_gate_does_not_fire_when_penalty_dominated_removal_fired(self):
        """[Deviation, plan 25-05] Real committed data
        (houston_tx-chicago_il@500mi) proves condition 4's proven,
        BOUNDED regret genuinely moves cost/stops on an arm-unchanged,
        admitted cell -- $241.9747... -> $242.9016..., stop 66643 ->
        64617, both EXACT_DP in both worlds. Exempting cells whose
        attribution shows penalty_dominated > 0 is therefore load-bearing,
        not decorative: without it, this gate would flag the strengthened
        rule's own proven, accepted behaviour as a correctness bug on
        every real corridor where condition 4 actually fires."""
        d = _diff(
            slug="houston_tx-chicago_il",
            before_kwargs=dict(total_cost=Decimal("241.97"), stop_opis_ids=(72899, 66643)),
            after_kwargs=dict(total_cost=Decimal("242.90"), stop_opis_ids=(72899, 64617)),
            penalty_dominated=1,
        )
        cmd_module._check_plan_identity([d])  # must not raise

    def test_gate_still_fires_alongside_an_unrelated_exempt_cell(self):
        """A penalty-dominated cell's exemption must not swallow a genuine
        violation on a DIFFERENT cell in the same run."""
        exempt = _diff(
            slug="houston_tx-chicago_il",
            before_kwargs=dict(total_cost=Decimal("241.97"), stop_opis_ids=(72899, 66643)),
            after_kwargs=dict(total_cost=Decimal("242.90"), stop_opis_ids=(72899, 64617)),
            penalty_dominated=1,
        )
        violating = _diff(
            slug="nashville_tn-buffalo_ny",
            before_kwargs=dict(total_cost=Decimal("10.00"), stop_opis_ids=(5,)),
            after_kwargs=dict(total_cost=Decimal("11.00"), stop_opis_ids=(5,)),
            penalty_dominated=0,
        )
        with self.assertRaises(CommandError) as ctx:
            cmd_module._check_plan_identity([exempt, violating])
        self.assertIn("nashville_tn-buffalo_ny", str(ctx.exception))
        self.assertNotIn("houston_tx-chicago_il", str(ctx.exception))

    def test_gate_does_not_fire_when_both_worlds_ran_the_heuristic_arm(self):
        """[Amended 2026-08-18, Phase 25 gap closure G1] solve() runs the
        penalty-aware heuristic over the FULL unpruned candidate list,
        never over the pruned search_set -- both the over-budget branch
        and the deadline-breach fallback pass `candidates`
        (solver.py:555-565, 533-554). This harness swaps that very
        argument between worlds (measure_dispatch_grid.py:396-397), so on
        a cell where both worlds land on the heuristic arm the two worlds
        fed it genuinely different inputs -- dallas_tx-seattle_wa@1050mi's
        exact shape (243 vs. 71 candidates). A differing plan there is the
        expected consequence of the measurement path, not evidence about
        the prune rule."""
        d = _diff(
            slug="dallas_tx-seattle_wa",
            tank_range_mi=Decimal("1050"),
            before_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("500.04"),
                stop_opis_ids=(1, 2, 3),
            ),
            after_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("467.63"),
                stop_opis_ids=(1, 9),
            ),
            penalty_dominated=0,
        )
        cmd_module._check_plan_identity([d])  # must not raise

    def test_gate_still_fires_on_an_exact_arm_divergence_alongside_a_heuristic_arm_cell(self):
        """Non-vacuity: proves the new heuristic-arm exclusion narrowed
        the gate's domain rather than disarming it -- a genuine exact-arm
        violation must still raise even in the presence of an excluded
        heuristic-arm cell in the same run."""
        heuristic_cell = _diff(
            slug="dallas_tx-seattle_wa",
            tank_range_mi=Decimal("1050"),
            before_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("500.04"),
                stop_opis_ids=(1, 2, 3),
            ),
            after_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("467.63"),
                stop_opis_ids=(1, 9),
            ),
            penalty_dominated=0,
        )
        exact_arm_cell = _diff(
            slug="nashville_tn-buffalo_ny",
            before_kwargs=dict(total_cost=Decimal("10.00"), stop_opis_ids=(5,)),
            after_kwargs=dict(total_cost=Decimal("11.00"), stop_opis_ids=(5,)),
            penalty_dominated=0,
        )
        with self.assertRaises(CommandError) as ctx:
            cmd_module._check_plan_identity([heuristic_cell, exact_arm_cell])
        self.assertIn("nashville_tn-buffalo_ny", str(ctx.exception))
        self.assertNotIn("dallas_tx-seattle_wa", str(ctx.exception))

    def test_exclusion_reason_is_none_exactly_when_the_gate_checks_the_cell(self):
        """Pins `_identity_check_exclusion_reason` as the single shared
        definition both the gate and the excluded-cells report section
        read -- covers all five exclusion branches plus one genuinely-
        checked cell, and asserts the five reasons are distinct."""
        censored = _diff(
            slug="censored-cell",
            before_kwargs=dict(censored=True, censored_reason="x"),
        )
        arm_changed = _diff(
            slug="arm-changed",
            before_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
            ),
            after_kwargs=dict(
                admitted_at_current_budget=True,
                untimed_strategy="exact_dp",
                timed_strategy="exact_dp",
            ),
        )
        timed_strategy_diverged = _diff(
            slug="timed-diverged",
            before_kwargs=dict(untimed_strategy="exact_dp", timed_strategy="exact_dp"),
            after_kwargs=dict(
                untimed_strategy="exact_dp", timed_strategy="penalty_aware_heuristic"
            ),
        )
        both_heuristic = _diff(
            slug="both-heuristic",
            before_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
            ),
            after_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
            ),
        )
        penalty_dominated_cell = _diff(
            slug="penalty-dominated",
            before_kwargs=dict(untimed_strategy="exact_dp", timed_strategy="exact_dp"),
            after_kwargs=dict(untimed_strategy="exact_dp", timed_strategy="exact_dp"),
            penalty_dominated=1,
        )
        genuinely_checked_cell = _diff(
            slug="genuinely-checked",
            before_kwargs=dict(untimed_strategy="exact_dp", timed_strategy="exact_dp"),
            after_kwargs=dict(untimed_strategy="exact_dp", timed_strategy="exact_dp"),
            penalty_dominated=0,
        )
        excluded = [
            censored,
            arm_changed,
            timed_strategy_diverged,
            both_heuristic,
            penalty_dominated_cell,
        ]

        reasons = [cmd_module._identity_check_exclusion_reason(d) for d in excluded]
        for reason in reasons:
            self.assertIsNotNone(reason)
        self.assertEqual(len(set(reasons)), 5, "all five reasons must be distinct")
        self.assertIsNone(
            cmd_module._identity_check_exclusion_reason(genuinely_checked_cell)
        )

    def test_gate_and_report_agree_on_which_cells_were_checked(self):
        """Anti-drift: the only test exercising both call sites against
        the same input. Neither
        test_exclusion_reason_is_none_exactly_when_the_gate_checks_the_cell
        (which pins the helper's own return values) nor
        test_unchecked_cells_section_names_every_excluded_cell_with_its_
        reason_and_movement (which pins that the section prints an
        excluded cell) would fail if the renderer stopped calling the
        helper and re-derived exclusion from the fields itself -- that
        regression would still pass both of those tests, yet let a cell
        be skipped by the gate and never appear in the excluded-cells
        report, silently converting a narrowed guard into a disarmed one.
        This test builds one diff list covering all five exclusion
        branches plus a genuinely-checked cell, gives every excluded cell
        a total_cost that differs between worlds (so each would be a gate
        violation were it not excluded), and asserts the GATE's checked
        set (read off the raised CommandError's own message) and the
        REPORT's checked set (cells the excluded-cells section does NOT
        list) are cell-by-cell identical."""
        censored = _diff(
            slug="censored-cell",
            before_kwargs=dict(censored=True, censored_reason="x", total_cost=Decimal("1.00")),
            after_kwargs=dict(total_cost=Decimal("2.00")),
        )
        arm_changed = _diff(
            slug="arm-changed",
            before_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("1.00"),
            ),
            after_kwargs=dict(
                admitted_at_current_budget=True,
                untimed_strategy="exact_dp",
                timed_strategy="exact_dp",
                total_cost=Decimal("2.00"),
            ),
        )
        timed_strategy_diverged = _diff(
            slug="timed-diverged",
            before_kwargs=dict(
                untimed_strategy="exact_dp", timed_strategy="exact_dp", total_cost=Decimal("1.00")
            ),
            after_kwargs=dict(
                untimed_strategy="exact_dp",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("2.00"),
            ),
        )
        both_heuristic = _diff(
            slug="both-heuristic",
            before_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("1.00"),
            ),
            after_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("2.00"),
            ),
        )
        penalty_dominated_cell = _diff(
            slug="penalty-dominated",
            before_kwargs=dict(
                untimed_strategy="exact_dp", timed_strategy="exact_dp", total_cost=Decimal("1.00")
            ),
            after_kwargs=dict(
                untimed_strategy="exact_dp", timed_strategy="exact_dp", total_cost=Decimal("2.00")
            ),
            penalty_dominated=1,
        )
        genuinely_checked_cell = _diff(
            slug="genuinely-checked",
            before_kwargs=dict(
                untimed_strategy="exact_dp", timed_strategy="exact_dp", total_cost=Decimal("1.00")
            ),
            after_kwargs=dict(
                untimed_strategy="exact_dp", timed_strategy="exact_dp", total_cost=Decimal("2.00")
            ),
            penalty_dominated=0,
        )

        diffs = [
            censored,
            arm_changed,
            timed_strategy_diverged,
            both_heuristic,
            penalty_dominated_cell,
            genuinely_checked_cell,
        ]
        all_slugs = {d.slug for d in diffs}

        with self.assertRaises(CommandError) as ctx:
            cmd_module._check_plan_identity(diffs)
        message = str(ctx.exception)
        gate_checked_slugs = {slug for slug in all_slugs if slug in message}
        self.assertEqual(gate_checked_slugs, {"genuinely-checked"})

        section = "\n".join(cmd_module._render_unchecked_cells_section(diffs))
        report_checked_slugs = {slug for slug in all_slugs if slug not in section}

        self.assertEqual(gate_checked_slugs, report_checked_slugs)
        gate_excluded_slugs = all_slugs - gate_checked_slugs
        self.assertEqual(gate_checked_slugs | gate_excluded_slugs, all_slugs)
        self.assertEqual(gate_checked_slugs & gate_excluded_slugs, set())
        self.assertIn("Checked 1 of 6 cell(s); excluded 5.", section)


# ---------------------------------------------------------------------------
# The excluded-cells report section is the visible half of the narrowing --
# the gate no longer speaks for these cells, so the report must.
# ---------------------------------------------------------------------------
class RenderUncheckedCellsSectionTests(SimpleTestCase):
    def test_unchecked_cells_section_names_every_excluded_cell_with_its_reason_and_movement(
        self,
    ):
        heuristic_excluded = _diff(
            slug="dallas_tx-seattle_wa",
            tank_range_mi=Decimal("1050"),
            before_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("500.04"),
                stops=3,
                stop_opis_ids=(1, 2, 3),
            ),
            after_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
                total_cost=Decimal("467.63"),
                stops=2,
                stop_opis_ids=(1, 9),
            ),
        )
        admission_excluded = _diff(
            slug="atlanta_ga-denver_co",
            tank_range_mi=Decimal("500"),
            before_kwargs=dict(
                admitted_at_current_budget=False,
                untimed_strategy="penalty_aware_heuristic",
                timed_strategy="penalty_aware_heuristic",
            ),
            after_kwargs=dict(
                admitted_at_current_budget=True,
                untimed_strategy="exact_dp",
                timed_strategy="exact_dp",
            ),
        )
        genuinely_checked_cell = _diff(
            slug="houston_tx-chicago_il",
            tank_range_mi=Decimal("1050"),
            before_kwargs=dict(
                untimed_strategy="exact_dp",
                timed_strategy="exact_dp",
                total_cost=Decimal("99.00"),
                stop_opis_ids=(1,),
            ),
            after_kwargs=dict(
                untimed_strategy="exact_dp",
                timed_strategy="exact_dp",
                total_cost=Decimal("99.00"),
                stop_opis_ids=(1,),
            ),
        )

        section = "\n".join(
            cmd_module._render_unchecked_cells_section(
                [heuristic_excluded, admission_excluded, genuinely_checked_cell]
            )
        )

        self.assertIn("## Cells excluded from the plan-identity gate", section)
        self.assertIn("dallas_tx-seattle_wa", section)
        self.assertIn("1050", section)
        self.assertIn(
            "solve() runs that arm over the FULL unpruned candidate", section
        )
        self.assertIn("$500.04", section)
        self.assertIn("$467.63", section)
        self.assertIn("3->2", section)
        self.assertIn("(1, 2, 3)->(1, 9)", section)
        self.assertIn("atlanta_ga-denver_co", section)
        self.assertIn("dispatch arm changed", section)
        self.assertIn("Checked 1 of 3 cell(s); excluded 2.", section)
        self.assertNotIn("houston_tx-chicago_il", section)
        self.assertIn(
            "Exclusion means this method did not check the cell", section
        )


# ---------------------------------------------------------------------------
# 7. The named lines render, in both a recovered and a not-recovered
#    fixture -- neither line is conditional on a happy outcome.
# ---------------------------------------------------------------------------
class NamedCellLinesRenderTests(SimpleTestCase):
    def test_dallas_and_demo_lines_render_when_nothing_recovers(self):
        report = cmd_module.render_report(
            production_trust_margin=Decimal("5.47"), diffs=_make_26_diffs()
        )
        self.assertIn("dallas_tx-seattle_wa@500mi", report)
        self.assertIn("demo_la_ca-new_york_ny@1050mi", report)
        self.assertIn("houston_tx-chicago_il@500mi", report)
        self.assertIn("D-15", report)

    def test_dallas_and_demo_lines_render_when_cells_recover(self):
        recovered = {
            ("atlanta_ga-denver_co", 500),
            ("miami_fl-boston_ma", 500),
            ("jacksonville_fl-bangor_me", 500),
        }
        report = cmd_module.render_report(
            production_trust_margin=Decimal("5.47"),
            diffs=_make_26_diffs(recovered_slugs=recovered),
        )
        self.assertIn("dallas_tx-seattle_wa@500mi", report)
        self.assertIn("demo_la_ca-new_york_ny@1050mi", report)
        self.assertIn("houston_tx-chicago_il@500mi", report)


# ---------------------------------------------------------------------------
# 8. The plateau verdict is imported, not reimplemented.
# ---------------------------------------------------------------------------
class PlateauVerdictImportTests(SimpleTestCase):
    def test_command_module_plateau_verdict_is_the_same_object(self):
        self.assertIs(cmd_module.plateau_verdict, plateau_verdict)

    def test_command_modules_verdict_matches_a_direct_call_on_the_same_rows(self):
        diffs = _make_26_diffs()
        rows = cmd_module._plateau_rows(diffs)
        self.assertEqual(cmd_module.plateau_verdict(rows), plateau_verdict(rows))

    def test_plateau_rows_carries_exactly_demoted_cell_count_demoted_rows(self):
        diffs = _make_26_diffs()
        rows = cmd_module._plateau_rows(diffs)
        demoted = [r for r in rows if r["was_demoted"]]
        self.assertEqual(len(demoted), DEMOTED_CELL_COUNT)


# ---------------------------------------------------------------------------
# diff_cell_results: anti-vacuity for the one pure function everything else
# in this module builds on.
# ---------------------------------------------------------------------------
class DiffCellResultsTests(SimpleTestCase):
    def test_identical_lists_produce_no_changed_cells(self):
        before = [_result(slug="a")]
        after = [_result(slug="a")]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertEqual(len(diffs), 1)
        self.assertFalse(diffs[0].is_changed)

    def test_cost_movement_is_reported_as_changed(self):
        before = [_result(slug="a", total_cost=Decimal("100.00"))]
        after = [_result(slug="a", total_cost=Decimal("150.00"))]
        diffs = cmd_module.diff_cell_results(before, after)
        self.assertTrue(diffs[0].is_changed)

    def test_mismatched_key_sets_raise_command_error(self):
        before = [_result(slug="a")]
        after = [_result(slug="b")]
        with self.assertRaises(CommandError):
            cmd_module.diff_cell_results(before, after)

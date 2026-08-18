import ast
import pathlib

from django.test import SimpleTestCase

ROUTING_DIR = pathlib.Path(__file__).resolve().parent.parent
SERVICES_DIR = ROUTING_DIR / "services"
FORBIDDEN_PREFIX = "routing.pipeline"

# `naive_baseline.solve()` is a completely different function (the
# deliberately price-blind baseline solver, unchanged this phase per
# STATE.md's v3.1 decision log) that has no `penalty` parameter at all and
# must never be flagged by `SolvePenaltyKwargGateTest` below; only the
# fixed-charge solver's own `solve()` (called as bare `solve(...)` or
# `solver.solve(...)`) is in scope for that gate.
_NON_TARGET_SOLVE_BASES = {"naive_baseline"}

SOLVER_FILES = [
    SERVICES_DIR / "solver.py",
    SERVICES_DIR / "exceptions.py",
    SERVICES_DIR / "prune.py",
    SERVICES_DIR / "dp.py",
    SERVICES_DIR / "greedy.py",
    SERVICES_DIR / "heuristic.py",
]
SOLVER_FORBIDDEN_PREFIXES = (
    "django",
    "routing.models",
    "routing.pipeline",
    "requests",
    "httpx",
    "urllib.request",
    "http.client",
)


def _collect_import_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


class ImportBoundaryTest(SimpleTestCase):
    """Statically enforces that routing/services/ (the request-path layer)
    never imports routing/pipeline/ (the offline-only geocoding layer).
    Vacuously true while services/ is empty; load-bearing the moment a
    later addition introduces a bad import.
    """

    def test_services_never_import_pipeline(self):
        violations = []
        for path in SERVICES_DIR.rglob("*.py"):
            for name in _collect_import_names(path):
                if name.startswith(FORBIDDEN_PREFIX):
                    violations.append(f"{path}: imports {name}")

        self.assertEqual(
            violations,
            [],
            f"routing/services/ must never import routing/pipeline/: {violations}",
        )

    def test_mapbox_and_corridor_modules_are_scanned_and_pipeline_free(self):
        """Regression test: mapbox.py and corridor.py legitimately
        import django/routing.models/requests (deliberately NOT added to
        SOLVER_FILES below -- they would trip SolverPurityTest's stricter
        gate), but the broader ImportBoundaryTest scan above must still
        cover them and confirm neither imports the offline geocoding
        pipeline package.
        """
        scanned = set(SERVICES_DIR.rglob("*.py"))
        mapbox_path = SERVICES_DIR / "mapbox.py"
        corridor_path = SERVICES_DIR / "corridor.py"

        self.assertIn(mapbox_path, scanned)
        self.assertIn(corridor_path, scanned)

        for path in (mapbox_path, corridor_path):
            violations = [
                name
                for name in _collect_import_names(path)
                if name.startswith(FORBIDDEN_PREFIX)
            ]
            self.assertEqual(violations, [], f"{path}: imports {violations}")


class ProbeSeamIsolationTest(SimpleTestCase):
    """D-06 (Phase 26 plan 26-03, Task 3): `routing/probe_seam.py` holds
    the dispatch-verdict probe seam's OS/proc instrumentation
    (`read_current_rss_kb`) and secret-gated resolution
    (`resolve_probe_budget`) -- neither belongs inside the AST-gated pure
    solver boundary `SolverPurityTest` below enforces. This gate asserts
    both directions: `routing/probe_seam.py` is not itself a member of
    `SOLVER_FILES`, and no `SOLVER_FILES` module imports it.

    Mutation check performed manually at authoring time (recorded verbatim
    in 26-03-SUMMARY.md, this file's own convention -- see
    `PruneInertnessGateTest`'s non-vacuity test for the analogous
    in-repository precedent): a temporary `import routing.probe_seam` line
    added to `routing/services/dp.py`, this test class run, the failure
    message confirmed to name `dp.py` and `routing.probe_seam` exactly,
    then reverted.
    """

    def test_probe_seam_is_not_a_solver_file(self):
        probe_seam_path = ROUTING_DIR / "probe_seam.py"
        self.assertNotIn(
            probe_seam_path,
            SOLVER_FILES,
            "routing/probe_seam.py must never be added to SOLVER_FILES -- "
            "it holds OS/proc instrumentation, header-name contract "
            "constants, and gate resolution, none of which belong inside "
            "the AST-gated pure solver boundary.",
        )

    def test_no_solver_file_imports_probe_seam(self):
        violations = []
        for path in SOLVER_FILES:
            for name in _collect_import_names(path):
                if name == "routing.probe_seam" or name.startswith(
                    "routing.probe_seam."
                ):
                    violations.append(f"{path}: imports {name}")

        self.assertEqual(
            violations,
            [],
            f"no SOLVER_FILES module may import routing.probe_seam: {violations}",
        )


class SolverPurityTest(SimpleTestCase):
    """Statically enforces that the solver (routing/services/solver.py,
    routing/services/exceptions.py, routing/services/prune.py,
    routing/services/dp.py, and routing/services/greedy.py -- the
    fixed-charge DP's production fallback, added Phase 18-04c) must stay
    free of Django, the ORM, the offline geocoding pipeline, and any HTTP
    client. Scoped to just these five files -- not all of services/ -- so
    a later Station -> Candidate adapter is free to import routing.models
    elsewhere in services/ without tripping this gate.
    """

    def test_solver_files_never_import_django_orm_pipeline_or_http(self):
        violations = []
        for path in SOLVER_FILES:
            for name in _collect_import_names(path):
                if any(name.startswith(prefix) for prefix in SOLVER_FORBIDDEN_PREFIXES):
                    violations.append(f"{path}: imports {name}")

        self.assertEqual(
            violations,
            [],
            f"solver.py/exceptions.py/prune.py/dp.py must never import django/ORM/pipeline/HTTP: {violations}",
        )


def _collect_solve_calls_missing_kwarg(path, kwarg_name):
    """Find every fixed-charge `solve(...)` call site in `path` missing an
    explicit `{kwarg_name}=` keyword. Treats a node as a `solve()` call
    when `func` is a bare `ast.Name` with `id == "solve"`, or an
    `ast.Attribute` with `attr == "solve"` whose base object name is not
    in `_NON_TARGET_SOLVE_BASES` -- `naive_baseline.solve()` has no
    `penalty`/`trust_margin` parameter at all (it is a different function
    entirely, the deliberately price-blind baseline) and must never be
    flagged here.

    Generalized from the plan's own two faithful options (clone the
    walker, or parameterize it and run it twice) -- this file picks the
    latter: `_collect_solve_calls_missing_penalty` and
    `_collect_solve_calls_missing_trust_margin` below are both thin,
    single-line wrappers over this one walker, so the AST-matching logic
    (which node counts as a target `solve()` call) is defined exactly
    once rather than duplicated and risking the two copies drifting apart
    on what counts as a target call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_target = False
        if isinstance(func, ast.Name) and func.id == "solve":
            is_target = True
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "solve"
            and not (
                isinstance(func.value, ast.Name)
                and func.value.id in _NON_TARGET_SOLVE_BASES
            )
        ):
            is_target = True
        if not is_target:
            continue
        has_kwarg = any(kw.arg == kwarg_name for kw in node.keywords)
        if not has_kwarg:
            violations.append(
                f"{path}:{node.lineno}: solve() call missing {kwarg_name}="
            )
    return violations


def _collect_solve_calls_missing_penalty(path):
    """`penalty=` specialization of `_collect_solve_calls_missing_kwarg`
    -- see that function's own docstring."""
    return _collect_solve_calls_missing_kwarg(path, "penalty")


def _collect_solve_calls_missing_trust_margin(path):
    """`trust_margin=` specialization of
    `_collect_solve_calls_missing_kwarg` (PROV-03, D-16) -- see that
    function's own docstring."""
    return _collect_solve_calls_missing_kwarg(path, "trust_margin")


_PRICE_SOURCE_DECISION_TYPES = (ast.Compare, ast.BoolOp, ast.IfExp)


def _build_parent_index(tree):
    """Map `id(child) -> (parent_node, field_name)` for every node in
    `tree`. The stdlib gives no parent pointer, so this is built by hand
    once per parse via `ast.iter_fields`, the same idiom the file's other
    two walkers already use for `ast.walk`.
    """
    index = {}
    for node in ast.walk(tree):
        for field_name, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                index[id(value)] = (node, field_name)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        index[id(item)] = (node, field_name)
    return index


def _price_source_reference_name(node):
    """Return the matched identifier if `node` is a reference to
    something whose name mentions `price_source` -- an `ast.Attribute`
    whose `attr` contains the substring, an `ast.Name` whose `id`
    contains it (this is what catches a walk-local tracking variable such
    as `heuristic.py`'s `current_price_source`), or the `arg` of an
    `ast.keyword`. Substring matching, not exact equality, is deliberate:
    the field is threaded through prefixed locals. Returns `None` when
    `node` is not such a reference.
    """
    if isinstance(node, ast.Attribute) and "price_source" in node.attr:
        return node.attr
    if isinstance(node, ast.Name) and "price_source" in node.id:
        return node.id
    if isinstance(node, ast.keyword) and node.arg and "price_source" in node.arg:
        return node.arg
    return None


def _is_price_source_decision_position(node, parent_index):
    """Walk `node`'s ancestor chain upward until the nearest enclosing
    `ast.stmt`, and decide whether any edge on that chain places the
    reference in a comparison, conditional, boolean, or sort-key
    position rather than a plain assignment/construction position.

    Flagged positions: an `ast.Compare`, `ast.BoolOp`, or `ast.IfExp`
    ancestor; a `lambda` (`ast.Lambda`) whose own parent is an
    `ast.keyword` named `key`; the `test` field of an `ast.If`/
    `ast.While`; the `ifs` field of an `ast.comprehension`; the `value`
    of an `ast.keyword` named `key` on any `ast.Call`; an `ast.Assert`;
    or an `ast.Match` subject.

    Everything else -- a keyword argument name or value in a
    constructor/function call, the target or value of an
    `ast.Assign`/`ast.AnnAssign`, an annotation, a dataclass field
    declaration, a returned attribute access, or an element of a plain
    tuple/list/dict literal -- is left unflagged by simply never
    matching one of the rules above.
    """
    current = node
    while id(current) in parent_index:
        parent, field_name = parent_index[id(current)]

        if isinstance(parent, _PRICE_SOURCE_DECISION_TYPES):
            return True
        if isinstance(parent, ast.Lambda):
            grandparent_info = parent_index.get(id(parent))
            if grandparent_info is not None:
                grandparent, _ = grandparent_info
                if isinstance(grandparent, ast.keyword) and grandparent.arg == "key":
                    return True
        if isinstance(parent, (ast.If, ast.While)) and field_name == "test":
            return True
        if isinstance(parent, ast.comprehension) and field_name == "ifs":
            return True
        if isinstance(parent, ast.keyword) and parent.arg == "key":
            return True
        if isinstance(parent, ast.Assert):
            return True
        if isinstance(parent, ast.Match) and field_name == "subject":
            return True

        if isinstance(parent, ast.stmt):
            break
        current = parent
    return False


def _enclosing_function_name(node, parent_index):
    """Walk `node`'s ancestor chain, via `parent_index`, up to the module
    root, and return the `name` of the nearest enclosing
    `ast.FunctionDef`/`ast.AsyncFunctionDef`. A reference with no
    enclosing function at all -- sitting at plain module scope -- is
    attributed to the sentinel `"<module>"` rather than silently
    skipped, so a module-scope provenance decision (exactly the shape
    this guard exists to catch) cannot evade attribution and therefore
    cannot evade the allowlist below.
    """
    current = node
    while id(current) in parent_index:
        parent, _field_name = parent_index[id(current)]
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent.name
        current = parent
    return "<module>"


def _collect_price_source_decision_reads(path):
    """Find every reference to `price_source` (or a name mentioning it)
    in `path` that sits in a comparison, conditional, boolean, or
    sort-key decision position. Returns a list of
    `(file_stem, enclosing_function_name, message)` tuples -- the first
    two fields are the `(file stem, enclosing function name)` key
    `PriceSourceUsagePurityTest`'s allowlist is keyed by; the third is a
    human-readable `"{path}:{lineno}: ..."` string, the same shape
    `_collect_solve_calls_missing_penalty` already uses for its own
    violation messages.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parent_index = _build_parent_index(tree)

    violations = []
    for node in ast.walk(tree):
        identifier = _price_source_reference_name(node)
        if identifier is None:
            continue
        if _is_price_source_decision_position(node, parent_index):
            function_name = _enclosing_function_name(node, parent_index)
            violations.append(
                (
                    path.stem,
                    function_name,
                    f"{path}:{node.lineno}: price_source ({identifier}) read in "
                    f"a comparison/conditional/boolean/sort-key position "
                    f"(function={function_name!r})",
                )
            )
    return violations


# D-23: the Phase 21 (PROV-03) inversion target. Exactly one entry --
# `solver.py` / `is_estimate_priced` -- the trust margin's single shared
# decision-position provenance read (see that function's own docstring in
# `routing/services/solver.py`). A future phase adding a second
# decision-position provenance read anywhere in `SOLVER_FILES` must extend
# this allowlist consciously, in the same commit as the read; a stale entry
# (naming a function that no longer contains such a read) must also fail,
# which is why the guard below uses exact set equality rather than a subset
# check -- a subset check would let this allowlist silently outgrow the
# code, the same slack that makes `prune(x) -> x` pass every soundness
# property.
_PRICE_SOURCE_DECISION_READ_ALLOWLIST = frozenset(
    {
        ("solver", "is_estimate_priced"),
    }
)


class PriceSourceUsagePurityTest(SimpleTestCase):
    """Statically enforces that `price_source` is read in a decision
    position (a comparison, a conditional, a boolean operand, or a sort
    key) inside the pure solver files (`SOLVER_FILES` above) at ONLY the
    exact `(file stem, enclosing function name)` pairs pinned in
    `_PRICE_SOURCE_DECISION_READ_ALLOWLIST` -- never zero, never more, and
    never a different one.

    History: Phase 20 shipped `price_source` threaded through the solver
    but functionally inert, and this guard originally forbade EVERY
    decision-position read outright (`assertEqual(violations, [])`) to
    convert that inertness from an observation about one measured run
    into a property of the source itself. Phase 21 (PROV-03, the trust
    margin) deliberately makes exactly one such read -- pricing an
    estimate-priced candidate's provenance into the DP's internal
    objective -- so the guard is narrowed here to an allowlist of that one
    read, per this project's standing rule that guards get inverted, never
    deleted, when the behaviour they forbade becomes deliberately
    sanctioned. `assertEqual` on sorted collections (not a subset check)
    is what makes this an inversion rather than a widening: it fails in
    BOTH directions -- an unexpected new decision-position read anywhere
    in `SOLVER_FILES` (including a second one inside `solver.py` itself),
    and a stale allowlist entry naming a function that no longer contains
    such a read. A future phase introducing a genuinely new
    decision-position provenance read must extend this allowlist
    consciously, in the same commit as the read that requires it.
    """

    def test_price_source_decision_reads_match_the_pinned_allowlist_exactly(self):
        found_keys = set()
        messages_by_key = {}
        for path in SOLVER_FILES:
            for file_stem, function_name, message in _collect_price_source_decision_reads(path):
                key = (file_stem, function_name)
                found_keys.add(key)
                messages_by_key.setdefault(key, []).append(message)

        self.assertEqual(
            sorted(found_keys),
            sorted(_PRICE_SOURCE_DECISION_READ_ALLOWLIST),
            "Decision-position price_source reads in SOLVER_FILES must match "
            "the pinned allowlist exactly (D-23) -- an unexpected new read is "
            "as much a failure here as a stale allowlist entry. "
            f"found={sorted(found_keys)}; "
            f"allowlist={sorted(_PRICE_SOURCE_DECISION_READ_ALLOWLIST)}; "
            f"messages={messages_by_key}",
        )


class SolvePenaltyKwargGateTest(SimpleTestCase):
    """Statically enforces that every production call to the fixed-charge
    solver's `solve()` (`routing/services/solver.py`) passes an explicit
    `penalty=` keyword. A future production code path calling `solve()`
    without an explicit `penalty=` would silently default to `Decimal(0)`
    and revert to pre-v3.1 behaviour undetected -- this gate recovers the
    only property a required keyword-only parameter would have bought,
    without forcing 27 mechanical edits to `routing/tests/test_solver.py`
    (which deliberately omits `penalty=` on every call to exercise the
    documented penalty=0 default, greedy-identical behaviour).

    Scoped to every `.py` file under `routing/` excluding `routing/tests/`
    -- a fixed `SOLVER_FILES`-style list would need updating every time a
    new caller file appears, defeating the gate's purpose.
    """

    def test_every_production_solve_call_passes_penalty(self):
        violations = []
        for path in ROUTING_DIR.rglob("*.py"):
            if "tests" in path.relative_to(ROUTING_DIR).parts:
                continue
            violations.extend(_collect_solve_calls_missing_penalty(path))

        self.assertEqual(
            violations, [], f"solve() call(s) missing penalty=: {violations}"
        )


# D-18: every production `solve()` call site, classified by `ast`. Recorded
# here (and in 21-06-SUMMARY.md in full) rather than left as a plain count
# so a future reader can see the total/production/test split at a glance
# without re-running the walk -- 18.1-05 did the identical thing for the
# `deadline=` kwarg and found 57 sites; this headline count is that same
# discipline applied to `trust_margin=`.
#
# Updated in plan 21-08: `routing/management/commands/measure_trust_margin.py`
# adds two new production call sites (the attribution and realism sweeps'
# own `solver.solve(...)` calls, both explicit `trust_margin=`) -- a real,
# legitimate classification drift this guard's own docstring exists to
# catch, not something to route around. 9 -> 11 production, 72 test
# unchanged, 81 -> 83 total.
#
# Updated again in plan 21-09: `routing/tests/test_price_provenance.py`
# gained one new TEST `solve()` call site
# (`FuelStopRebuildHopTests.test_hop5_estimate_bypass_pair_survives_the_solve_rebuild`,
# the regression test for the rebuild-loop bug this plan found and fixed
# in `routing/services/solver.py`) -- 11 production unchanged, 72 -> 73
# test, 83 -> 84 total.
#
# Updated again in plan 21-11: `routing/tests/test_price_provenance.py`
# gained one more TEST `solve()` call site
# (`RealCorridorEstimateBypassWitnessTests`, the corridor-replay
# regression test pinning ROADMAP criterion 5's real
# el_paso_tx-portland_me/QUIKTRIP #667 witness) -- 11 production
# unchanged, 73 -> 74 test, 84 -> 85 total.
#
# Updated again in plan 22-13: `routing/tests/test_west_coast_feasibility.py`
# gained one new TEST `solve()` call site (`WestCoastFeasibilityRegressionTests
# ._solve_probe`, the single shared helper every West Coast feasibility test
# in that module calls) -- 11 production unchanged, 76 -> 77 test, 87 -> 88
# total.
#
# Updated again in Phase 26 plan 26-03: `routing/tests/test_dispatch_probe_seam.py`
# gained four new TEST `solve()` call sites (`TransitionBudgetHatchTests` --
# two calls in `test_omitting_the_keyword_matches_the_explicit_default`, one
# each in `test_a_tiny_budget_closes_the_gate_on_an_admitted_cell` and
# `test_a_raised_budget_opens_the_gate_on_a_demoted_cell`), proving D-06's new
# `transition_budget=` hatch is non-vacuous in both directions. 11 production
# unchanged, 77 -> 81 test, 88 -> 92 total.
#
# Updated again in Phase 26 plan 26-06: `routing/views.py`'s
# `RouteView._solve_all_alternatives` gained a SECOND literal `solve()` call
# site (the `probe_budget is not None` branch, which adds `transition_budget=`
# alongside the same literal `penalty=`/`trust_margin=`) rather than building
# one call from a shared kwargs dict -- an `**kwargs`-unpacked call would not
# satisfy this file's own `SolvePenaltyKwargGateTest`/
# `SolveTrustMarginKwargGateTest`, which require the literal keyword at the
# call site. 11 -> 12 production, 81 test unchanged, 92 -> 93 total.
TRUST_MARGIN_CALL_SITE_TOTAL_COUNT = 93
TRUST_MARGIN_CALL_SITE_PRODUCTION_COUNT = 12
TRUST_MARGIN_CALL_SITE_TEST_COUNT = 81


class SolveTrustMarginKwargGateTest(SimpleTestCase):
    """Statically enforces that every production call to the fixed-charge
    solver's `solve()` (`routing/services/solver.py`) passes an explicit
    `trust_margin=` keyword -- PROV-03, D-16, cloned from
    `SolvePenaltyKwargGateTest` above via the shared
    `_collect_solve_calls_missing_kwarg` walker (see that function's own
    docstring for why a shared walker was chosen over a hand-duplicated
    second copy). A future production code path calling `solve()` without
    an explicit `trust_margin=` would silently default to `Decimal(0)` and
    never price an `eia_regional_estimate`-priced candidate's provenance
    into the objective, undetected -- this gate recovers the only property
    a required keyword-only parameter would have bought, without forcing
    mechanical edits to every test module that deliberately omits
    `trust_margin=` to exercise the documented `trust_margin=0` default
    (byte-identical-to-pre-margin behaviour).

    Scoped identically to `SolvePenaltyKwargGateTest`: every `.py` file
    under `routing/` excluding `routing/tests/`, with the same
    `_NON_TARGET_SOLVE_BASES` exclusion for `naive_baseline.solve()` (a
    different function entirely, with no `trust_margin` parameter).

    `TRUST_MARGIN_CALL_SITE_TOTAL_COUNT` /
    `..._PRODUCTION_COUNT` / `..._TEST_COUNT` above are the D-18
    call-site classification headline counts, walked once by
    `test_call_site_classification_matches_pinned_counts` below and cross-
    checked to sum correctly -- the same non-vacuity discipline every
    other pinned-count guard in this codebase uses.
    """

    def test_every_production_solve_call_passes_trust_margin(self):
        violations = []
        for path in ROUTING_DIR.rglob("*.py"):
            if "tests" in path.relative_to(ROUTING_DIR).parts:
                continue
            violations.extend(_collect_solve_calls_missing_trust_margin(path))

        self.assertEqual(
            violations, [], f"solve() call(s) missing trust_margin=: {violations}"
        )

    def test_call_site_classification_matches_pinned_counts(self):
        production_count = 0
        test_count = 0
        for path in ROUTING_DIR.rglob("*.py"):
            is_test_path = "tests" in path.relative_to(ROUTING_DIR).parts
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_target = False
                if isinstance(func, ast.Name) and func.id == "solve":
                    is_target = True
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == "solve"
                    and not (
                        isinstance(func.value, ast.Name)
                        and func.value.id in _NON_TARGET_SOLVE_BASES
                    )
                ):
                    is_target = True
                if not is_target:
                    continue
                if is_test_path:
                    test_count += 1
                else:
                    production_count += 1

        self.assertEqual(
            production_count,
            TRUST_MARGIN_CALL_SITE_PRODUCTION_COUNT,
            "production solve() call-site count drifted from the pinned "
            f"D-18 classification: found {production_count}, "
            f"pinned {TRUST_MARGIN_CALL_SITE_PRODUCTION_COUNT}",
        )
        self.assertEqual(
            test_count,
            TRUST_MARGIN_CALL_SITE_TEST_COUNT,
            "test solve() call-site count drifted from the pinned D-18 "
            f"classification: found {test_count}, "
            f"pinned {TRUST_MARGIN_CALL_SITE_TEST_COUNT}",
        )
        self.assertEqual(
            production_count + test_count,
            TRUST_MARGIN_CALL_SITE_TOTAL_COUNT,
            "production + test solve() call-site counts do not sum to the "
            "pinned D-18 total",
        )


def _is_call_command_call(node):
    """True when `node` is an `ast.Call` whose `func` resolves to
    `call_command` -- a bare `ast.Name` (`call_command(...)`) or an
    `ast.Attribute` (`management.call_command(...)`)."""
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "call_command") or (
        isinstance(func, ast.Attribute) and func.attr == "call_command"
    )


def _is_seed_stations_call(node):
    """True when `node` is a `call_command(...)` call whose first
    positional argument is the string literal `"seed_stations"`."""
    if not _is_call_command_call(node) or not node.args:
        return False
    first_arg = node.args[0]
    return (
        isinstance(first_arg, ast.Constant)
        and isinstance(first_arg.value, str)
        and first_arg.value == "seed_stations"
    )


def _collect_seed_stations_calls_with_literal_path(path):
    """Find every `call_command("seed_stations", ...)` call site in `path`
    whose SUBSEQUENT positional arguments -- everything after the command
    name itself -- include a hand-written string-literal or f-string path,
    rather than either no path at all (inheriting `seed_stations`' own
    canonical default) or an unpacked/starred name expression (the shape
    `reseed_all`'s own call takes: `*[str(p) for p in
    CANONICAL_STATION_CSV_PATHS]`). A command that reseeds only one
    hand-picked file would report a pre-import result as post-import, with
    no error and no log line -- this is the third application of the same
    AST call-site gate pattern this codebase already uses twice
    (`SolvePenaltyKwargGateTest`, `SolveTrustMarginKwargGateTest`), applied
    here to a different failure mode (D-29).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_seed_stations_call(node):
            continue
        for extra_arg in node.args[1:]:
            is_literal_path = isinstance(extra_arg, ast.Constant) and isinstance(
                extra_arg.value, str
            )
            is_fstring_path = isinstance(extra_arg, ast.JoinedStr)
            if is_literal_path or is_fstring_path:
                violations.append(
                    f"{path}:{node.lineno}: call_command('seed_stations', ...) "
                    "passes a hand-written positional path literal instead of "
                    "routing through reseed_all() or the canonical default"
                )
                break
    return violations


# D-29 call-site classification, walked once by
# `test_call_site_count_matches_pinned_total` below and cross-checked to
# sum correctly -- the same non-vacuity discipline
# `SolveTrustMarginKwargGateTest` already applies to its own pinned counts.
# `station_csv_paths.py`'s own `reseed_all()` is production call site #1
# (its `call_command("seed_stations", *[...])` call is itself scanned and
# found compliant, not exempted).
#
# [Amended 2026-08-18, Phase 26 plan 26-02] Re-walked after plan 26-02
# added `routing/tests/test_heuristic_candidate_diff.py`. Its
# `RealCorridorTestCase.setUpTestData` calls `call_command("seed_stations",
# stdout=io.StringIO())` once, with no extra positional path argument --
# one new TEST call site, compliant with D-29 (no literal path).
# Production count untouched -- this plan's own production command
# (`measure_heuristic_candidate_diff.py`) reseeds via `reseed_all()`, not
# a direct `call_command("seed_stations", ...)` call. 8 production, 37
# test, 45 total.
SEED_STATIONS_CALL_SITE_PRODUCTION_COUNT = 8
SEED_STATIONS_CALL_SITE_TEST_COUNT = 37
SEED_STATIONS_CALL_SITE_TOTAL_COUNT = 45


class SeedStationsCallSiteGateTest(SimpleTestCase):
    """Statically enforces that no production `call_command("seed_stations",
    ...)` call site anywhere under `routing/` (excluding `routing/tests/`)
    passes a hand-written positional path literal (D-29). Every reseed must
    route through `routing.services.station_csv_paths.reseed_all()` (which
    unpacks `CANONICAL_STATION_CSV_PATHS` via `*[...]`, never a literal) or
    rely on `seed_stations`' own canonical default (no extra positional
    arguments at all) -- a command that reseeds only a subset of the
    canonical CSV list would report a pre-import result as post-import,
    with no error and no log line, the same vacuity class as a check aimed
    at a file that does not exist.

    Third application of the AST call-site gate pattern
    (`SolvePenaltyKwargGateTest`, `SolveTrustMarginKwargGateTest`), scoped
    identically: every `.py` file under `routing/` excluding
    `routing/tests/`.
    """

    def test_no_seed_stations_call_site_passes_a_literal_path(self):
        violations = []
        for path in ROUTING_DIR.rglob("*.py"):
            if "tests" in path.relative_to(ROUTING_DIR).parts:
                continue
            violations.extend(_collect_seed_stations_calls_with_literal_path(path))

        self.assertEqual(
            violations,
            [],
            f"seed_stations call site(s) with a hand-written path literal: {violations}",
        )

    def test_call_site_count_matches_pinned_total(self):
        production_count = 0
        test_count = 0
        for path in ROUTING_DIR.rglob("*.py"):
            is_test_path = "tests" in path.relative_to(ROUTING_DIR).parts
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_seed_stations_call(node):
                    continue
                if is_test_path:
                    test_count += 1
                else:
                    production_count += 1

        self.assertEqual(
            production_count,
            SEED_STATIONS_CALL_SITE_PRODUCTION_COUNT,
            "production seed_stations call-site count drifted from the "
            f"pinned D-29 classification: found {production_count}, "
            f"pinned {SEED_STATIONS_CALL_SITE_PRODUCTION_COUNT}",
        )
        self.assertEqual(
            test_count,
            SEED_STATIONS_CALL_SITE_TEST_COUNT,
            "test seed_stations call-site count drifted from the pinned "
            f"D-29 classification: found {test_count}, "
            f"pinned {SEED_STATIONS_CALL_SITE_TEST_COUNT}",
        )
        self.assertEqual(
            production_count + test_count,
            SEED_STATIONS_CALL_SITE_TOTAL_COUNT,
            "production + test seed_stations call-site counts do not sum "
            "to the pinned D-29 total",
        )


# D-23: `source` and `gers_id` are licence and audit metadata with no
# solver meaning at all -- unlike `price_source`, which legitimately
# crosses into the pure boundary as a plain string with exactly one
# allowlisted decision-position read (the trust-margin term computed by
# `solver.is_estimate_priced`), these two fields have no reason to be read
# inside SOLVER_FILES in ANY position, not merely outside decision
# position. The cost of a stricter, zero-tolerance guard here is justified
# by `opis_id`'s own history: a field whose contract quietly expanded into
# a load-bearing tie-break sort key without anyone revisiting it. This
# walker is deliberately a SEPARATE, parallel machine from the
# `price_source` walker above, not a widening of it -- narrowing an
# existing decision-position-only walker to also catch plain-position
# reads would blur what each guard is actually enforcing.
_SOLVER_FORBIDDEN_FIELD_NAMES = frozenset({"source", "gers_id"})


def _is_subscript_key_constant(node, parent_index):
    """True when `node` is an `ast.Constant` string sitting in subscript
    key position -- the `slice` field of an `ast.Subscript`
    (`row["source"]` or `row['gers_id']`)."""
    parent_info = parent_index.get(id(node))
    if parent_info is None:
        return False
    parent, field_name = parent_info
    return isinstance(parent, ast.Subscript) and field_name == "slice"


def _is_getattr_key_constant(node, parent_index):
    """True when `node` is an `ast.Constant` string sitting as the second
    positional argument to a `getattr(...)` call (`getattr(x, "source")`)."""
    parent_info = parent_index.get(id(node))
    if parent_info is None:
        return False
    parent, field_name = parent_info
    if not (isinstance(parent, ast.Call) and field_name == "args"):
        return False
    func = parent.func
    if not (isinstance(func, ast.Name) and func.id == "getattr"):
        return False
    return len(parent.args) >= 2 and parent.args[1] is node


def _collect_forbidden_field_references(path):
    """Find every reference to a forbidden solver-purity field name
    (`_SOLVER_FORBIDDEN_FIELD_NAMES`) in `path`, in ATTRIBUTE ACCESS or
    SUBSCRIPT/GETATTR KEY position only -- never a bare `ast.Name`, and
    never a docstring or comment (a docstring is one large `ast.Constant`
    string, not one equal to exactly `"source"` or `"gers_id"`, so it never
    matches the equality check below).

    Deliberately narrow: the bare word `source` is common in this codebase
    (`inspect.getsource`, local variables, prose in docstrings such as
    "Infeasibility has exactly one source..."), and `price_source` itself
    contains `source` as a substring but is a completely different,
    legitimate field. Attribute access is matched by exact `attr`
    equality (so `.price_source` never matches `.source`), and
    subscript/`getattr` key matching requires an exact string-constant
    equality (so a `price_source` key string never matches either).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parent_index = _build_parent_index(tree)

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _SOLVER_FORBIDDEN_FIELD_NAMES:
            violations.append(
                f"{path}:{node.lineno}: attribute access .{node.attr} "
                f"(function={_enclosing_function_name(node, parent_index)!r})"
            )
            continue
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _SOLVER_FORBIDDEN_FIELD_NAMES
            and (
                _is_subscript_key_constant(node, parent_index)
                or _is_getattr_key_constant(node, parent_index)
            )
        ):
            violations.append(
                f"{path}:{node.lineno}: key reference {node.value!r} "
                f"(function={_enclosing_function_name(node, parent_index)!r})"
            )
    return violations


# Empty and asserted empty (D-23) -- a future entry is a deliberate,
# visible edit to this constant AND to the test below, rather than a quiet
# widening. Unlike `_PRICE_SOURCE_DECISION_READ_ALLOWLIST`, no entry is
# ever expected here.
_SOURCE_GERS_ID_READ_ALLOWLIST = frozenset()


class SourceGersIdPurityTest(SimpleTestCase):
    """Statically enforces that `source` and `gers_id` -- Phase 22's two
    new station-provenance metadata fields -- are never referenced, in any
    syntactic position, inside any of the six `SOLVER_FILES`. Stricter
    than `PriceSourceUsagePurityTest` above: that guard only forbids
    DECISION-position reads of `price_source` (a field that legitimately
    has one allowlisted such read); this guard forbids ANY attribute or
    subscript/`getattr`-key reference to `source`/`gers_id` at all, because
    these two fields carry no solver meaning whatsoever -- they are
    licence and audit metadata, not pricing or routing inputs.

    `_PRICE_SOURCE_DECISION_READ_ALLOWLIST`, `PriceSourceUsagePurityTest`,
    `SOLVER_FILES`, and `SOLVER_FORBIDDEN_PREFIXES` are all untouched by
    this class -- it is a new, parallel walker, not an extension of the
    existing `price_source` machinery.
    """

    def test_source_and_gers_id_never_referenced_in_solver_files(self):
        violations = []
        for path in SOLVER_FILES:
            violations.extend(_collect_forbidden_field_references(path))

        self.assertEqual(
            violations,
            [],
            f"source/gers_id referenced inside SOLVER_FILES: {violations}",
        )

    def test_allowlist_is_empty(self):
        self.assertEqual(
            len(_SOURCE_GERS_ID_READ_ALLOWLIST),
            0,
            "D-23's allowlist must stay empty -- a future widening must "
            "edit this assertion consciously, not pass it silently.",
        )


_PRUNE_CALL_FORBIDDEN_KWARGS = frozenset({"mpg", "penalty"})


def _is_prune_dominated_candidates_call(node):
    """True when `node` is an `ast.Call` whose `func` resolves to
    `prune_dominated_candidates` -- a bare `ast.Name`
    (`prune_dominated_candidates(...)`) or an `ast.Attribute`
    (`prune.prune_dominated_candidates(...)`), matching the same
    shape-matching idiom `_collect_solve_calls_missing_kwarg` above already
    uses for `solve()`. The module-level `def prune_dominated_candidates`
    statement itself is an `ast.FunctionDef`, never an `ast.Call`, so it is
    never matched here -- only actual invocations are in scope.
    """
    func = node.func
    if isinstance(func, ast.Name) and func.id == "prune_dominated_candidates":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "prune_dominated_candidates":
        return True
    return False


def _collect_prune_inertness_violations_from_tree(tree, label):
    """Find every `prune_dominated_candidates(...)` call in `tree` that
    supplies an `mpg=` or `penalty=` keyword, and return
    `f"{label}:{lineno}: ..."` violation strings -- the shared walker both
    `_collect_prune_inertness_violations` (real files) and the D-14 gate's
    own non-vacuity test (an in-test source string, never written to disk)
    call, so the AST-matching logic is defined exactly once.
    """
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_prune_dominated_candidates_call(node):
            continue
        offending = sorted(
            kw.arg for kw in node.keywords if kw.arg in _PRUNE_CALL_FORBIDDEN_KWARGS
        )
        if offending:
            violations.append(
                f"{label}:{node.lineno}: prune_dominated_candidates() call "
                f"supplies forbidden kwarg(s) {offending}"
            )
    return violations


def _collect_prune_inertness_violations(path):
    """`path`-reading specialization of
    `_collect_prune_inertness_violations_from_tree` -- see that function's
    own docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _collect_prune_inertness_violations_from_tree(tree, str(path))


# D-14 call-site inventory (Phase 25, 2026-08-17): every
# `prune_dominated_candidates` CALL site (never the `def` statement itself)
# found repo-wide under `routing/` by a fresh AST walk at write time --
# verified against the working tree directly, not transcribed from
# 25-RESEARCH.md's own "~35 call sites across 15 files" estimate, which
# undercounts because it approximated rather than walked the tree after
# this plan's own test additions. 53 total: 5 production (the only solver
# call, `routing/services/solver.py:505`, plus the four command sites
# `routing/management/commands/measure_dispatch_grid.py:288`,
# `measure_prune_reduction.py:406`, `measure_dispatch_predictor.py:296`,
# and `measure_solver_latency.py:189`) and 48 test, across 7 files
# (`test_prune_soundness.py`, `test_solver_dispatch.py`,
# `test_dispatch_predictor.py`, `test_dp_differential.py`,
# `test_dp_real_corridor_regression.py`, `test_dp_deadline.py`,
# `test_heuristic.py`).
#
# [Amended 2026-08-17, plan 25-03] Re-walked after plan 25-03 added
# `PenaltyDominationSoundnessTests` to `test_prune_soundness.py` -- its
# oracle-differential assertion, its closed-form-retention assertion, and
# its permanent boundary witness each call `prune_dominated_candidates`
# once, for 3 new test call sites (test_prune_soundness.py's own AST
# call-node count rises from 37 to 40 in this same walk; the file-count
# above still names 7 files, unchanged). Production count is untouched --
# this plan adds no production code. 51 test, 56 total.
#
# [Amended 2026-08-17, plan 25-05] Re-walked after plan 25-05 added
# `routing/management/commands/measure_prune_dispatch_diff.py`. Its
# `_attribute_cell` helper calls `prune_dominated_candidates` directly
# (with `mpg=`/`penalty=` supplied) to independently rebuild each grid
# row's retained set for `classify_removals`' attribution -- a call
# `_measure_cell` itself does not expose, since it returns only counts.
# One new production call site; test count untouched -- this plan adds no
# new test-module call to `prune_dominated_candidates` (its own test
# module mocks the seams instead of running a real sweep). 6 production,
# 51 test, 57 total.
#
# [Amended 2026-08-18, Phase 26 plan 26-02] Re-walked after plan 26-02
# added `routing/management/commands/measure_heuristic_candidate_diff.py`.
# Its `_measure_cell` function calls `prune_dominated_candidates` once
# per cell (World B's SHIPPED, unstrengthened search set -- `mpg=`/
# `penalty=` both omitted) -- one new production call site. Test count
# untouched -- `test_heuristic_candidate_diff.py` calls the command
# module's own `_measure_cell`/`_row_for_cell` helpers rather than
# `prune_dominated_candidates` directly. 7 production, 51 test, 58 total.
PRUNE_CALL_SITE_PRODUCTION_COUNT = 7
PRUNE_CALL_SITE_TEST_COUNT = 51
PRUNE_CALL_SITE_TOTAL_COUNT = 58


class PruneInertnessGateTest(SimpleTestCase):
    """D-14: `solve()` must never supply `mpg=` or `penalty=` to
    `prune_dominated_candidates`. D-04 made those two parameters keyword-
    only and defaulting to `None` specifically so all existing call sites
    (`PRUNE_CALL_SITE_TOTAL_COUNT` of them) keep compiling untouched --
    which means Phase 25's inertness is by CONVENTION, not by
    CONSTRUCTION: nothing structurally stops a future edit from wiring the
    strengthened rule into production and silently flipping
    `ADMISSION_MANIFEST` cells that DISP-03 (Phase 26) exists to ratify.
    This gate is the structural backstop for that convention -- Phase 26
    is the phase that will deliberately remove or invert this gate when it
    wires the strengthened rule into `solve()`.
    """

    def test_solver_prune_call_never_supplies_mpg_or_penalty(self):
        path = SERVICES_DIR / "solver.py"
        violations = _collect_prune_inertness_violations(path)
        self.assertEqual(
            violations,
            [],
            f"solver.py's prune_dominated_candidates() call must never "
            f"supply mpg= or penalty= (D-14): {violations}",
        )

    def test_walker_reports_a_real_violation_and_nothing_on_clean_source(self):
        """Proves the gate non-vacuous: the walker must actually report a
        violation on an in-test source string containing a
        `prune_dominated_candidates(..., penalty=...)` call, and report
        nothing on an otherwise-identical clean source -- never written to
        disk, exercising `_collect_prune_inertness_violations_from_tree`
        directly rather than `_collect_prune_inertness_violations`'s
        file-reading wrapper.
        """
        violating_source = (
            "from routing.services.prune import prune_dominated_candidates\n"
            "def f(candidates, tank_range_mi, total_route_mi, penalty):\n"
            "    return prune_dominated_candidates(\n"
            "        candidates,\n"
            "        tank_range_mi=tank_range_mi,\n"
            "        total_route_mi=total_route_mi,\n"
            "        penalty=penalty,\n"
            "    )\n"
        )
        clean_source = (
            "from routing.services.prune import prune_dominated_candidates\n"
            "def f(candidates, tank_range_mi, total_route_mi):\n"
            "    return prune_dominated_candidates(\n"
            "        candidates,\n"
            "        tank_range_mi=tank_range_mi,\n"
            "        total_route_mi=total_route_mi,\n"
            "    )\n"
        )
        violating_tree = ast.parse(violating_source, filename="<violating>")
        clean_tree = ast.parse(clean_source, filename="<clean>")

        violations = _collect_prune_inertness_violations_from_tree(
            violating_tree, "<violating>"
        )
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("penalty", violations[0])

        self.assertEqual(
            _collect_prune_inertness_violations_from_tree(clean_tree, "<clean>"),
            [],
        )

    def test_call_site_counts_match_pinned_totals(self):
        production_count = 0
        test_count = 0
        for path in ROUTING_DIR.rglob("*.py"):
            is_test_path = "tests" in path.relative_to(ROUTING_DIR).parts
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _is_prune_dominated_candidates_call(node):
                    if is_test_path:
                        test_count += 1
                    else:
                        production_count += 1

        self.assertEqual(
            production_count,
            PRUNE_CALL_SITE_PRODUCTION_COUNT,
            "production prune_dominated_candidates call-site count drifted "
            f"from the pinned D-14 classification: found {production_count}, "
            f"pinned {PRUNE_CALL_SITE_PRODUCTION_COUNT}",
        )
        self.assertEqual(
            test_count,
            PRUNE_CALL_SITE_TEST_COUNT,
            "test prune_dominated_candidates call-site count drifted from "
            f"the pinned D-14 classification: found {test_count}, pinned "
            f"{PRUNE_CALL_SITE_TEST_COUNT}",
        )
        self.assertEqual(
            production_count + test_count,
            PRUNE_CALL_SITE_TOTAL_COUNT,
            "production + test prune_dominated_candidates call-site counts "
            "do not sum to the pinned D-14 total",
        )

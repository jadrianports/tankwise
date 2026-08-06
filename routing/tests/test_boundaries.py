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


def _collect_solve_calls_missing_penalty(path):
    """Find every fixed-charge `solve(...)` call site in `path` missing an
    explicit `penalty=` keyword. Treats a node as a `solve()` call when
    `func` is a bare `ast.Name` with `id == "solve"`, or an
    `ast.Attribute` with `attr == "solve"` whose base object name is not
    in `_NON_TARGET_SOLVE_BASES` -- `naive_baseline.solve()` has no
    `penalty` parameter at all (it is a different function entirely, the
    deliberately price-blind baseline) and must never be flagged here.
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
        has_penalty_kwarg = any(kw.arg == "penalty" for kw in node.keywords)
        if not has_penalty_kwarg:
            violations.append(f"{path}:{node.lineno}: solve() call missing penalty=")
    return violations


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


def _collect_price_source_decision_reads(path):
    """Find every reference to `price_source` (or a name mentioning it)
    in `path` that sits in a comparison, conditional, boolean, or
    sort-key decision position. Returns `"{path}:{lineno}: ..."`
    violation strings, the same shape
    `_collect_solve_calls_missing_penalty` already uses.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parent_index = _build_parent_index(tree)

    violations = []
    for node in ast.walk(tree):
        identifier = _price_source_reference_name(node)
        if identifier is None:
            continue
        if _is_price_source_decision_position(node, parent_index):
            violations.append(
                f"{path}:{node.lineno}: price_source ({identifier}) read in "
                f"a comparison/conditional/boolean/sort-key position"
            )
    return violations


class PriceSourceUsagePurityTest(SimpleTestCase):
    """Statically enforces that `price_source` may appear in the pure
    solver files (`SOLVER_FILES` above) only in assignment and
    construction positions -- never as the operand of a comparison, the
    test of a conditional, an operand of a boolean operator, or a sort
    key. Phase 20 ships provenance data threaded through the solver but
    functionally inert: this guard converts that inertness from an
    observation about one measured run into a property of the source
    itself, so a future edit that starts deciding on `price_source`
    cannot land silently.

    Phase 21 (PROV-03, the trust margin) is expected and sanctioned to
    make exactly the kind of decision-position read this guard forbids,
    once it prices provenance into the DP's internal objective. When
    that lands, the correct action is to invert this guard -- narrow or
    re-target it to the specific reads Phase 21 introduces -- and never
    delete it outright, per this project's standing rule that guards get
    inverted rather than removed. A green result here is therefore
    evidence about Phase 20's scope, not a prohibition binding Phase 21's
    planner: it says provenance is inert *today*, not that the DP may
    never read it.
    """

    def test_price_source_never_read_in_comparison_conditional_or_sort_key(self):
        violations = []
        for path in SOLVER_FILES:
            violations.extend(_collect_price_source_decision_reads(path))

        self.assertEqual(
            violations,
            [],
            f"price_source read in a decision position inside the pure solver: {violations}",
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

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

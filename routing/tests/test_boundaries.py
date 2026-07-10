import ast
import pathlib

from django.test import SimpleTestCase

SERVICES_DIR = pathlib.Path(__file__).resolve().parent.parent / "services"
FORBIDDEN_PREFIX = "routing.pipeline"

SOLVER_FILES = [
    SERVICES_DIR / "solver.py",
    SERVICES_DIR / "exceptions.py",
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
    never imports routing/pipeline/ (the offline-only geocoding layer),
    per D-23 / DATA-05. Vacuously true while services/ is empty (Phase 1);
    load-bearing the moment a later phase adds a bad import.
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


class SolverPurityTest(SimpleTestCase):
    """Statically enforces FUEL-05: the solver (routing/services/solver.py
    and routing/services/exceptions.py) must stay free of Django, the ORM,
    the offline geocoding pipeline, and any HTTP client. Scoped to just
    these two files -- not all of services/ -- so Phase 4's Station ->
    Candidate adapter is free to import routing.models elsewhere in
    services/ without tripping this gate.
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
            f"solver.py/exceptions.py must never import django/ORM/pipeline/HTTP: {violations}",
        )

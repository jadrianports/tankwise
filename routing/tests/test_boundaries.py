import ast
import pathlib

from django.test import SimpleTestCase

SERVICES_DIR = pathlib.Path(__file__).resolve().parent.parent / "services"
FORBIDDEN_PREFIX = "routing.pipeline"


class ImportBoundaryTest(SimpleTestCase):
    """Statically enforces that routing/services/ (the request-path layer)
    never imports routing/pipeline/ (the offline-only geocoding layer),
    per D-23 / DATA-05. Vacuously true while services/ is empty (Phase 1);
    load-bearing the moment a later phase adds a bad import.
    """

    def test_services_never_import_pipeline(self):
        violations = []
        for path in SERVICES_DIR.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name.startswith(FORBIDDEN_PREFIX):
                        violations.append(f"{path}: imports {name}")

        self.assertEqual(
            violations,
            [],
            f"routing/services/ must never import routing/pipeline/: {violations}",
        )

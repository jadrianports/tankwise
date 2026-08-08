"""Tests for the fetch_overture_extract management command.

The DuckDB query layer is mocked at exactly one boundary,
`_run_extract_query` -- the same mock-the-query-layer precedent
test_geocode_stations.py uses for its own external call boundary. Because
`handle()` imports the Parquet toolchain as its own first statement (by
design -- see the command's module docstring), `duckdb` is stubbed into
`sys.modules` for the duration of each test so `import duckdb` resolves
without the real package being installed. This is what lets this whole
module -- and therefore the full backend suite -- pass with the Parquet
toolchain absent from the project virtualenv, which is the actual proof
that production never needs it.

`DuckdbModuleScopeImportGuardTests` is the static (AST-only) guard that no
module reachable from wsgi.py/manage.py startup imports the toolchain at
module scope -- it never executes any of the scanned files, so it needs no
stubbing at all.
"""
import csv
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from django.core.management import call_command

from routing.management.commands import fetch_overture_extract as cmd_module
from routing.pipeline import overture_scope
from routing.tests.test_boundaries import _collect_import_names

BASE_DIR = Path(settings.BASE_DIR)
ROUTING_DIR = BASE_DIR / "routing"
CONFIG_DIR = BASE_DIR / "config"
FETCH_COMMAND_PATH = ROUTING_DIR / "management" / "commands" / "fetch_overture_extract.py"

_DUCKDB_PREFIX = "duckdb"


def _row(
    gers_id="id-1",
    name="Pilot",
    brand_name="PILOT",
    freeform="123 Main St",
    locality="Redding",
    region="CA",
    postcode="96001",
    category="gas_station",
    confidence=0.9,
    operating_status="open",
    longitude=-122.1,
    latitude=40.5,
):
    return (
        gers_id,
        name,
        brand_name,
        freeform,
        locality,
        region,
        postcode,
        category,
        confidence,
        operating_status,
        longitude,
        latitude,
    )


class FetchOvertureExtractCommandTests(SimpleTestCase):
    """Query boundary mocked at `_run_extract_query`; `duckdb` itself is
    stubbed into `sys.modules` so `handle()`'s lazy `import duckdb` (and the
    connection setup that follows it) resolve against a harmless mock
    rather than requiring the real package."""

    def setUp(self):
        duckdb_stub_patcher = mock.patch.dict(sys.modules, {"duckdb": mock.MagicMock()})
        duckdb_stub_patcher.start()
        self.addCleanup(duckdb_stub_patcher.stop)

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "raw.csv"
        self.report_path = Path(self.tmpdir.name) / "report.md"

    def _call(self):
        call_command(
            "fetch_overture_extract",
            f"--output-path={self.output_path}",
            f"--report-path={self.report_path}",
        )

    def test_zero_rows_writes_header_only_csv_and_zero_count_report(self):
        with mock.patch.object(cmd_module, "_run_extract_query", return_value=[]):
            self._call()

        with open(self.output_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [cmd_module.RAW_EXTRACT_HEADER])

        report_text = self.report_path.read_text(encoding="utf-8")
        self.assertIn("Raw rows returned: 0", report_text)
        self.assertIn("Rows written: 0", report_text)

    def test_well_formed_row_lands_with_correct_column_order(self):
        with mock.patch.object(cmd_module, "_run_extract_query", return_value=[_row()]):
            self._call()

        with open(self.output_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        self.assertEqual(rows[0], cmd_module.RAW_EXTRACT_HEADER)
        self.assertEqual(
            rows[1],
            [
                "id-1",
                "Pilot",
                "PILOT",
                "123 Main St",
                "Redding",
                "CA",
                "96001",
                "gas_station",
                "0.9",
                "open",
                "-122.1",
                "40.5",
            ],
        )

    def test_malformed_coordinate_row_is_skipped_logged_and_counted(self):
        bad_row = _row(gers_id="id-bad", longitude="not-a-number")
        good_row = _row(gers_id="id-good")

        with mock.patch.object(
            cmd_module, "_run_extract_query", return_value=[bad_row, good_row]
        ):
            with self.assertLogs(cmd_module.logger.name, level="WARNING") as captured:
                self._call()

        self.assertTrue(any("id-bad" in message for message in captured.output))

        with open(self.output_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        # header + exactly the one good row -- the malformed row never
        # aborted the run and was never written.
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "id-good")

        report_text = self.report_path.read_text(encoding="utf-8")
        self.assertIn("malformed_coordinate: 1", report_text)
        self.assertIn("Rows written: 1", report_text)

    def test_transport_style_error_propagates(self):
        with mock.patch.object(
            cmd_module, "_run_extract_query", side_effect=RuntimeError("s3 boom")
        ):
            with self.assertRaises(RuntimeError):
                self._call()

    def test_resolved_s3_path_contains_the_pinned_release_literal(self):
        sql = cmd_module._extract_sql(overture_scope.overture_s3_path())
        self.assertIn(overture_scope.OVERTURE_RELEASE, sql)
        self.assertIn("2026-07-22.0", sql)

    def test_parser_exposes_no_pinned_scope_parameter_flags(self):
        parser = cmd_module.Command().create_parser("manage.py", "fetch_overture_extract")
        dests = {action.dest for action in parser._actions}
        forbidden_substrings = ("release", "bbox", "categor", "confidence")
        offending = {
            dest for dest in dests if any(sub in dest for sub in forbidden_substrings)
        }
        self.assertEqual(offending, set())


class RequirementsOfflineIsolationTests(SimpleTestCase):
    def test_duckdb_absent_from_requirements_txt(self):
        text = (BASE_DIR / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("duckdb", text)

    def test_requirements_offline_pins_duckdb_exactly(self):
        text = (BASE_DIR / "requirements-offline.txt").read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(lines, ["duckdb==1.5.5"])


def _collect_module_scope_import_names(path):
    """Names imported by statements that execute when `path` is imported --
    everything except the body of a `def`/`async def` (a new, deferred
    scope). Descends into `if`/`try`/`with`/class bodies at module level
    since those DO execute at import time; stops at function boundaries,
    which is the whole point of this guard (fetch_overture_extract.py's
    `import duckdb` lives inside `handle()`, a function body, and must
    never show up here)."""
    import ast

    parsed = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                names.extend(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                names.append(child.module or "")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            else:
                visit(child)

    visit(parsed)
    return names


def _scan_startup_reachable_targets():
    """Every `.py` file under `routing/` (excluding `routing/tests/`) and
    `config/`, plus every `.py` file directly at the repo root (e.g.
    `manage.py`) -- the set `DuckdbModuleScopeImportGuardTests` asserts
    carries zero module-scope Parquet-toolchain imports."""
    targets = list(BASE_DIR.glob("*.py"))
    for path in ROUTING_DIR.rglob("*.py"):
        if "tests" in path.relative_to(ROUTING_DIR).parts:
            continue
        targets.append(path)
    targets.extend(CONFIG_DIR.rglob("*.py"))
    return targets


class DuckdbModuleScopeImportGuardTests(SimpleTestCase):
    """Statically (AST-only, no execution) proves that `duckdb` is never
    imported at module scope anywhere reachable from `wsgi.py` or
    `manage.py` startup, and that `fetch_overture_extract.py` genuinely
    does import it somewhere -- so this guard cannot pass vacuously by the
    module having simply lost its capability."""

    def test_no_startup_reachable_module_imports_duckdb_at_module_scope(self):
        violations = []
        for path in _scan_startup_reachable_targets():
            for name in _collect_module_scope_import_names(path):
                if name == _DUCKDB_PREFIX or name.startswith(f"{_DUCKDB_PREFIX}."):
                    violations.append(str(path))

        self.assertEqual(
            violations,
            [],
            f"module-scope duckdb import reachable from startup: {violations}",
        )

    def test_fetch_overture_extract_imports_duckdb_somewhere(self):
        all_names = _collect_import_names(FETCH_COMMAND_PATH)
        self.assertIn(_DUCKDB_PREFIX, all_names)

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
from django.core.management.base import CommandError

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

    def test_resolved_s3_path_contains_the_pinned_release_exactly_once(self):
        resolved_path = overture_scope.overture_s3_path()
        sql = cmd_module._extract_sql(resolved_path)
        self.assertIn(overture_scope.OVERTURE_RELEASE, sql)

        expected_path = overture_scope.OVERTURE_S3_PATH_TEMPLATE.format(
            release=overture_scope.OVERTURE_RELEASE
        )
        self.assertEqual(resolved_path, expected_path)
        self.assertEqual(resolved_path.count(overture_scope.OVERTURE_RELEASE), 1)

    def test_parser_exposes_no_pinned_scope_parameter_flags(self):
        parser = cmd_module.Command().create_parser("manage.py", "fetch_overture_extract")
        dests = {action.dest for action in parser._actions}
        forbidden_substrings = ("release", "bbox", "categor", "confidence")
        offending = {
            dest for dest in dests if any(sub in dest for sub in forbidden_substrings)
        }
        self.assertEqual(offending, set())


class CategoryPredicateMigrationTests(SimpleTestCase):
    """Pin the categories -> taxonomy struct-path migration (D-17/D-24) as
    pure string assertions against the value `_extract_sql` returns. These
    need no DuckDB and therefore run in both backend CI jobs -- the
    duckdb-fixture job proves the migrated query executes; these prove it
    kept reading the field it is supposed to read, in both positions it
    appears (the SELECT projection and the WHERE membership predicate).

    Reverting `_extract_sql`'s two `taxonomy.primary` occurrences back to
    `categories.primary` by hand and re-running this class was observed to
    fail both `test_taxonomy_primary_appears_in_projection_and_predicate_only`
    and `test_deprecated_categories_struct_path_is_absent`; reverting the
    hand edit restored a clean `git diff`. See the plan SUMMARY for the
    exact failure text observed."""

    def setUp(self):
        self.sql = cmd_module._extract_sql("routing/tests/fixtures/overture/places_sample.parquet")

    def test_taxonomy_primary_appears_in_projection_and_predicate_only(self):
        # Exactly twice: once aliased to `category` in the SELECT
        # projection, once in the `IN (...)` membership predicate.
        self.assertEqual(self.sql.count("taxonomy.primary"), 2)

    def test_deprecated_categories_struct_path_is_absent(self):
        # Asserted on the *returned SQL string*, never by grepping the
        # source file -- the module's own dated forward-risk amendment
        # legitimately names the old field in prose, and a source-file grep
        # would trip on that prose rather than the query itself.
        self.assertNotIn("categories.primary", self.sql)

    def test_category_filter_members_are_derived_not_restated(self):
        # Both members of CATEGORY_FILTER still appear, quoted, inside the
        # membership predicate -- derived from the live tuple rather than a
        # second hard-coded literal pair in this test file.
        for category in overture_scope.CATEGORY_FILTER:
            self.assertIn(f"'{category}'", self.sql)

    def test_confidence_floor_and_bbox_predicate_unchanged(self):
        # The migration touches the category path only -- the confidence
        # floor and the two-box bbox predicate are untouched.
        self.assertIn(f"confidence >= {overture_scope.CONFIDENCE_FLOOR}", self.sql)
        self.assertIn(overture_scope.bbox_predicate_sql(), self.sql)


class FixtureColumnPresenceTests(SimpleTestCase):
    """Asserts the committed Parquet fixture carries the two columns the
    migrated query reads (`basic_category`, `taxonomy`), without importing
    duckdb -- a byte-level scan of the file's raw bytes for the column
    names. This is deliberately crude: it is chosen over adding a DuckDB
    dependency to the backend test jobs, which is exactly the isolation
    `RequirementsOfflineIsolationTests` and `DuckdbModuleScopeImportGuardTests`
    below exist to preserve. Parquet's column names are written as literal
    UTF-8 strings in both the per-row-group schema entries and the file
    footer, so a plain substring search over the raw bytes reliably finds
    them without parsing the container format."""

    FIXTURE_PATH = (
        Path(settings.BASE_DIR)
        / "routing"
        / "tests"
        / "fixtures"
        / "overture"
        / "places_sample.parquet"
    )

    def setUp(self):
        self.raw_bytes = self.FIXTURE_PATH.read_bytes()

    def test_fixture_carries_basic_category_and_taxonomy_columns(self):
        self.assertIn(b"basic_category", self.raw_bytes)
        self.assertIn(b"taxonomy", self.raw_bytes)

    def test_check_is_non_vacuous_absent_sentinel_is_not_found(self):
        # Guards against the check having become a tautology: a column name
        # that must NOT be present is asserted absent, so this byte-level
        # check can genuinely fail rather than always passing regardless of
        # fixture content.
        self.assertNotIn(b"DEFINITELY_NOT_A_REAL_COLUMN_SENTINEL", self.raw_bytes)


class WhereClauseSharingTests(SimpleTestCase):
    """Pins that `_extract_sql` and `_count_only_sql` embed the identical
    `_where_clause()` fragment (D-15's "one predicate, two queries")."""

    def test_shared_fragment_is_non_empty_and_present_in_both_queries(self):
        fragment = cmd_module._where_clause()
        # Non-empty is asserted first -- an empty fragment would make the
        # `assertIn` checks below pass vacuously against any string.
        self.assertTrue(fragment)

        extract_sql = cmd_module._extract_sql("source.parquet")
        count_sql = cmd_module._count_only_sql("source.parquet")
        self.assertIn(fragment, extract_sql)
        self.assertIn(fragment, count_sql)


class CountBandArithmeticTests(SimpleTestCase):
    def test_floor_below_ceiling_above_floor_positive(self):
        baseline = 10_248
        floor, ceiling = cmd_module._count_band(baseline)
        self.assertLess(floor, baseline)
        self.assertGreater(ceiling, baseline)
        self.assertGreater(floor, 0)


class CommittedExtractRowCountTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def test_missing_file_raises_command_error_naming_the_path(self):
        missing_path = Path(self.tmpdir.name) / "does-not-exist.csv"
        with self.assertRaises(CommandError) as captured:
            cmd_module._committed_extract_row_count(missing_path)
        self.assertIn(str(missing_path), str(captured.exception))

    def test_header_only_file_raises_command_error(self):
        header_only_path = Path(self.tmpdir.name) / "header_only.csv"
        with open(header_only_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(cmd_module.RAW_EXTRACT_HEADER)
        with self.assertRaises(CommandError) as captured:
            cmd_module._committed_extract_row_count(header_only_path)
        self.assertIn(str(header_only_path), str(captured.exception))


class CountOnlyModeTests(SimpleTestCase):
    """Exercises `--count-only` end to end via `call_command`, reusing the
    single `_run_extract_query` mocking boundary and the `duckdb`
    sys.modules stub convention `FetchOvertureExtractCommandTests` already
    establishes."""

    BASELINE = 100

    def setUp(self):
        duckdb_stub_patcher = mock.patch.dict(sys.modules, {"duckdb": mock.MagicMock()})
        duckdb_stub_patcher.start()
        self.addCleanup(duckdb_stub_patcher.stop)

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "raw.csv"
        self.report_path = Path(self.tmpdir.name) / "report.md"
        self.baseline_path = Path(self.tmpdir.name) / "baseline.csv"
        self._write_baseline(self.BASELINE)

    def _write_baseline(self, row_count):
        with open(self.baseline_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(cmd_module.RAW_EXTRACT_HEADER)
            for i in range(row_count):
                writer.writerow(_row(gers_id=f"id-{i}"))

    def _call(self, source_count):
        with mock.patch.object(
            cmd_module, "_run_extract_query", return_value=[(source_count,)]
        ):
            call_command(
                "fetch_overture_extract",
                "--count-only",
                f"--output-path={self.output_path}",
                f"--report-path={self.report_path}",
                f"--baseline-path={self.baseline_path}",
            )

    def test_source_count_equal_to_baseline_is_accepted(self):
        self._call(self.BASELINE)  # must not raise

    def test_source_count_of_zero_is_rejected(self):
        # The exact truncation symptom this gate exists to catch -- a gate
        # that accepts zero is worse than no gate.
        with self.assertRaises(CommandError):
            self._call(0)

    def test_floor_boundary_just_inside_is_accepted_just_outside_is_rejected(self):
        floor, _ceiling = cmd_module._count_band(self.BASELINE)
        self._call(floor)  # just inside -- accepted
        with self.assertRaises(CommandError):
            self._call(floor - 1)  # just outside -- rejected

    def test_ceiling_boundary_just_inside_is_accepted_just_outside_is_rejected(self):
        _floor, ceiling = cmd_module._count_band(self.BASELINE)
        self._call(ceiling)  # just inside -- accepted
        with self.assertRaises(CommandError):
            self._call(ceiling + 1)  # just outside -- rejected

    def test_count_only_run_writes_neither_csv_nor_report(self):
        self._call(self.BASELINE)
        self.assertFalse(self.output_path.exists())
        self.assertFalse(self.report_path.exists())

    def test_count_only_still_checks_when_output_path_already_exists(self):
        # Proves the pre-existing "output exists, pass --force" early
        # return does not swallow the count-only branch: an out-of-band
        # count against an already-existing output path must still raise,
        # not silently no-op.
        self.output_path.write_text("pretend pre-existing extract", encoding="utf-8")
        with self.assertRaises(CommandError):
            self._call(0)


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

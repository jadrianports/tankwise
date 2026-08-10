"""Tests for `discover_overture_release` and `bump_overture_release`.

No DuckDB anywhere in this file: discovery's DuckDB call is mocked at
`_run_glob_query` (the same single-boundary convention
`test_fetch_overture_extract.py` uses for its own query boundary), with
`duckdb` itself stubbed into `sys.modules` so `handle()`'s lazy
`import duckdb` resolves without the real package installed. The rewrite
command needs no DuckDB at all -- it only ever touches text files on disk.

The rewrite tests use synthetic, non-date-shaped tokens
(`RELEASE-TOKEN-OLD`/`RELEASE-TOKEN-NEW`) rather than real release-date
literals, mirroring `test_overture_scope.py`'s own anti-vacuity convention:
`rewrite_release` is a plain substring/regex substitution, so it needs no
real release shape to exercise either direction, and this keeps the test
file itself free of a quoted release-date literal.
"""
import io
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from routing.management.commands import bump_overture_release as bump_cmd_module
from routing.management.commands import discover_overture_release as discover_cmd_module
from routing.pipeline import overture_scope

OLD_TOKEN = "RELEASE-TOKEN-OLD"
NEW_TOKEN = "RELEASE-TOKEN-NEW"


def _s3_path(release, filename="part-00000.parquet"):
    return (
        f"s3://overturemaps-us-west-2/release/{release}/theme=places/"
        f"type=place/{filename}",
    )


# ---------------------------------------------------------------------------
# discover_overture_release
# ---------------------------------------------------------------------------


class ReleaseGlobPatternTests(SimpleTestCase):
    def test_pattern_equals_template_wildcarded_and_shaped_like_a_places_scan(self):
        pattern = discover_cmd_module._release_glob_pattern()
        expected = overture_scope.OVERTURE_S3_PATH_TEMPLATE.format(release="*")
        # The regression guard against the silently-empty naive form: the
        # pattern must be derived from the exact template the real extract
        # uses, never a shorter hand-written wildcard.
        self.assertEqual(pattern, expected)
        self.assertTrue(pattern.endswith(".parquet"))
        self.assertIn("theme=places", pattern)
        self.assertIn("type=place", pattern)


class ReleasesFromPathsTests(SimpleTestCase):
    def test_extracts_dedupes_across_partition_files_and_sorts_ascending(self):
        rows = [
            _s3_path("2026-07-22.0", "part-00001.parquet"),
            _s3_path("2026-06-17.0", "part-00000.parquet"),
            _s3_path("2026-07-22.0", "part-00002.parquet"),
            _s3_path("2026-07-22.0", "part-00003.parquet"),
        ]
        result = discover_cmd_module._releases_from_paths(rows)
        self.assertEqual(result, ["2026-06-17.0", "2026-07-22.0"])

    def test_newest_is_selected_by_plain_string_ordering_not_insertion_order(self):
        # The newest member ("2026-08-19.0") is inserted first, not last --
        # proving the result is sorted, not merely returned in call order.
        rows = [
            _s3_path("2026-08-19.0"),
            _s3_path("2026-07-22.0"),
            _s3_path("2026-06-17.0"),
        ]
        result = discover_cmd_module._releases_from_paths(rows)
        self.assertEqual(result[-1], "2026-08-19.0")


class DiscoverOvertureReleaseCommandTests(SimpleTestCase):
    """Query boundary mocked at `_run_glob_query`; `duckdb` itself is
    stubbed into `sys.modules` so `handle()`'s lazy `import duckdb`
    resolves against a harmless mock rather than requiring the real
    package -- mirrors `FetchOvertureExtractCommandTests`'s own setup."""

    def setUp(self):
        duckdb_stub_patcher = mock.patch.dict(sys.modules, {"duckdb": mock.MagicMock()})
        duckdb_stub_patcher.start()
        self.addCleanup(duckdb_stub_patcher.stop)

    def _call(self, rows):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(discover_cmd_module, "_run_glob_query", return_value=rows):
            call_command("discover_overture_release", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_empty_query_result_raises_naming_the_pattern(self):
        pattern = discover_cmd_module._release_glob_pattern()
        with mock.patch.object(discover_cmd_module, "_run_glob_query", return_value=[]):
            with self.assertRaises(CommandError) as captured:
                call_command(
                    "discover_overture_release",
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
        self.assertIn(pattern, str(captured.exception))

    def test_stdout_carries_exactly_three_key_equals_value_lines(self):
        rows = [
            _s3_path(overture_scope.OVERTURE_RELEASE),
            _s3_path("2026-08-19.0"),
        ]
        out, _err = self._call(rows)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, r"^[a-z_]+=[^\s]+$")
        self.assertEqual(lines[0], f"pinned_release={overture_scope.OVERTURE_RELEASE}")
        self.assertEqual(lines[1], "newest_release=2026-08-19.0")
        self.assertEqual(lines[2], "is_newer=true")

    def test_is_newer_false_when_newest_equals_pinned(self):
        rows = [_s3_path(overture_scope.OVERTURE_RELEASE)]
        out, _err = self._call(rows)
        lines = out.splitlines()
        self.assertEqual(lines[2], "is_newer=false")

    def test_pinned_release_absent_warns_on_stderr_and_does_not_raise(self):
        rows = [_s3_path("2026-08-19.0")]
        # Must not raise -- the pinned release aging out of the bucket's
        # short retained history is not a failure.
        out, err = self._call(rows)
        self.assertIn(overture_scope.OVERTURE_RELEASE, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)


# ---------------------------------------------------------------------------
# bump_overture_release
# ---------------------------------------------------------------------------


class RewriteReleaseTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

        (self.root / "routing" / "pipeline").mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "routing" / "tests").mkdir(parents=True)
        (self.root / "data").mkdir()

        self.scope_path = self.root / "routing" / "pipeline" / "overture_scope.py"
        self.notice_path = self.root / "NOTICE"
        self.readme_path = self.root / "README.md"
        self.algorithm_path = self.root / "docs" / "ALGORITHM.md"

        self.decoy_test_path = self.root / "routing" / "tests" / "test_decoy.py"
        self.decoy_report_path = self.root / "data" / "overture-generated-report.md"
        self.decoy_source_path = self.root / "unrelated_module.py"

    def _write_happy_path_tree(self):
        self.scope_path.write_text(
            "\n".join(
                [
                    '"""Module docstring."""',
                    "",
                    f"# Current release: {OLD_TOKEN} -- comment prose only.",
                    f'OVERTURE_RELEASE = "{OLD_TOKEN}"',
                    "",
                    "OTHER_CONSTANT = 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.notice_path.write_text(f"NOTICE mentions {OLD_TOKEN} once.", encoding="utf-8")
        self.readme_path.write_text(f"README release `{OLD_TOKEN}`.", encoding="utf-8")
        self.algorithm_path.write_text(f"ALGORITHM release `{OLD_TOKEN}`.", encoding="utf-8")

        self.decoy_test_path.write_text(
            f"# a test file that happens to mention {OLD_TOKEN}\n", encoding="utf-8"
        )
        self.decoy_report_path.write_text(
            f"Generated report for release {OLD_TOKEN}.\n", encoding="utf-8"
        )
        self.decoy_source_path.write_text(
            f"OLD_RELEASE_COMMENT = '{OLD_TOKEN}'\n", encoding="utf-8"
        )

    def test_happy_path_rewrites_constant_and_every_restatement_file(self):
        self._write_happy_path_tree()

        result = bump_cmd_module.rewrite_release(self.root, OLD_TOKEN, NEW_TOKEN)

        self.assertEqual(result[bump_cmd_module.SCOPE_MODULE_RELATIVE_PATH], 1)
        for relative_path in overture_scope.RELEASE_RESTATEMENT_FILES:
            self.assertGreaterEqual(result[relative_path], 1)

        scope_text = self.scope_path.read_text(encoding="utf-8")
        self.assertIn(f'OVERTURE_RELEASE = "{NEW_TOKEN}"', scope_text)
        self.assertNotIn(f'OVERTURE_RELEASE = "{OLD_TOKEN}"', scope_text)
        # The comment line above the assignment is untouched -- only the
        # assignment line itself is a substitution target.
        self.assertIn(f"# Current release: {OLD_TOKEN} -- comment prose only.", scope_text)

        self.assertIn(NEW_TOKEN, self.notice_path.read_text(encoding="utf-8"))
        self.assertNotIn(OLD_TOKEN, self.notice_path.read_text(encoding="utf-8"))
        self.assertIn(NEW_TOKEN, self.readme_path.read_text(encoding="utf-8"))
        self.assertIn(NEW_TOKEN, self.algorithm_path.read_text(encoding="utf-8"))

    def test_decoy_files_are_byte_identical_after_rewrite(self):
        self._write_happy_path_tree()

        before = {
            path: path.read_bytes()
            for path in (
                self.decoy_test_path,
                self.decoy_report_path,
                self.decoy_source_path,
            )
        }

        bump_cmd_module.rewrite_release(self.root, OLD_TOKEN, NEW_TOKEN)

        for path, before_bytes in before.items():
            self.assertEqual(
                path.read_bytes(),
                before_bytes,
                f"{path} was modified by rewrite_release -- write-scope guard failed",
            )

    def test_returned_mapping_key_set_equals_exactly_the_allowlist(self):
        self._write_happy_path_tree()

        result = bump_cmd_module.rewrite_release(self.root, OLD_TOKEN, NEW_TOKEN)

        expected_keys = {bump_cmd_module.SCOPE_MODULE_RELATIVE_PATH} | set(
            overture_scope.RELEASE_RESTATEMENT_FILES
        )
        self.assertEqual(set(result.keys()), expected_keys)

    def test_scope_module_anchored_pattern_does_not_over_match_similar_names(self):
        self.scope_path.write_text(
            "\n".join(
                [
                    f'OVERTURE_RELEASE = "{OLD_TOKEN}"',
                    f'OVERTURE_RELEASE_BACKUP = "{OLD_TOKEN}"',
                ]
            ),
            encoding="utf-8",
        )
        self.notice_path.write_text(OLD_TOKEN, encoding="utf-8")
        self.readme_path.write_text(OLD_TOKEN, encoding="utf-8")
        self.algorithm_path.write_text(OLD_TOKEN, encoding="utf-8")

        # No violation here -- exactly one real assignment line matches the
        # anchored pattern (the backup line uses a different name), so this
        # asserts the happy path is not accidentally over-matching.
        result = bump_cmd_module.rewrite_release(self.root, OLD_TOKEN, NEW_TOKEN)
        self.assertEqual(result[bump_cmd_module.SCOPE_MODULE_RELATIVE_PATH], 1)

    def test_scope_module_missing_assignment_raises_naming_the_path(self):
        self.scope_path.write_text("OVERTURE_RELEASE = \"something-else\"\n", encoding="utf-8")
        self.notice_path.write_text(OLD_TOKEN, encoding="utf-8")
        self.readme_path.write_text(OLD_TOKEN, encoding="utf-8")
        self.algorithm_path.write_text(OLD_TOKEN, encoding="utf-8")

        with self.assertRaises(CommandError) as captured:
            bump_cmd_module.rewrite_release(self.root, OLD_TOKEN, NEW_TOKEN)
        self.assertIn(str(self.scope_path), str(captured.exception))

    def test_prose_file_missing_old_token_raises_naming_that_file(self):
        self._write_happy_path_tree()
        # README no longer restates the release at all.
        self.readme_path.write_text("Nothing release-related here.", encoding="utf-8")

        with self.assertRaises(CommandError) as captured:
            bump_cmd_module.rewrite_release(self.root, OLD_TOKEN, NEW_TOKEN)
        self.assertIn("README.md", str(captured.exception))


class BumpOvertureReleaseCommandValidationTests(SimpleTestCase):
    """Command-level validation, exercised against the real pinned
    `overture_scope.OVERTURE_RELEASE` -- none of these cases reach
    `rewrite_release`, so no real repository file is ever touched."""

    def _call(self, new_release):
        call_command(
            "bump_overture_release",
            new_release,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    def test_malformed_new_release_is_rejected(self):
        with self.assertRaises(CommandError) as captured:
            self._call("not-a-release")
        self.assertIn("not a well-formed release", str(captured.exception))

    def test_new_release_equal_to_current_is_rejected(self):
        with self.assertRaises(CommandError) as captured:
            self._call(overture_scope.OVERTURE_RELEASE)
        self.assertIn("equals the current pinned release", str(captured.exception))

    def test_new_release_sorting_before_current_is_rejected(self):
        with self.assertRaises(CommandError) as captured:
            self._call("2000-01-01.0")
        self.assertIn("does not sort strictly after", str(captured.exception))

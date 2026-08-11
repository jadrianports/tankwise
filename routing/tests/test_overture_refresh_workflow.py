"""Structural guards over `.github/workflows/overture-refresh.yml` -- the
scheduled workflow that discovers a newer Overture release, gates on its
source row count, and opens a pull request through the stored fine-grained
token rather than the default one.

This module parses the workflow with `yaml.safe_load`, never executes any
of it. `yaml` is available transitively through `drf-spectacular` (schema
generation already depends on it), so it is installed in both backend CI
jobs (`backend-sqlite`/`backend-postgres`) without a new pin -- this is not
an undeclared dependency.

Every assertion here targets a property that is expensive to discover by
failure: an omitted `token:` input on a checkout step presents as "the pull
request opened but CI isn't running" days later, a scrambled step order
presents as a source-integrity gate that silently never ran, and a drifted
branch-prefix literal presents as no symptom at all -- pull requests keep
opening, duplicate suppression keeps appearing to work, and the staleness
sweep simply never fires on anything. See this repository's `overture-refresh/`
prefix guard below for exactly that case.

PyYAML parses a bare `on:` mapping key as the boolean `True` under YAML 1.1
resolution rules (`on`/`off`/`yes`/`no` are all boolean literals in that
spec) -- `_trigger_block()` below reads `doc.get("on", doc.get(True))` to
stay correct regardless of which key PyYAML actually produced.
"""
import re
import subprocess

from django.conf import settings
from django.test import SimpleTestCase

import yaml

BASE_DIR = settings.BASE_DIR
WORKFLOW_PATH = BASE_DIR / ".github" / "workflows" / "overture-refresh.yml"

EXPECTED_JOB_NAMES = ("discover", "refresh", "stale_sweep")
EXPECTED_BRANCH_PREFIX = "overture-refresh/"

# The single source of truth for the branch name the two checks below refuse
# to let this workflow write to. Every pattern is built from this constant
# rather than the literal "main" so the guard stays correct if the default
# branch is ever renamed.
DEFAULT_BRANCH = "main"

_PUSH_TO_DEFAULT_BRANCH_PATTERNS = (
    r"git\s+push\b[^\n]*\borigin\s+" + re.escape(DEFAULT_BRANCH) + r"\b",
    r"git\s+push\b[^\n]*\bHEAD:" + re.escape(DEFAULT_BRANCH) + r"\b",
    r"git\s+push\b[^\n]*\brefs/heads/" + re.escape(DEFAULT_BRANCH) + r"\b",
)

_GH_API_DEFAULT_BRANCH_WRITE_PATTERNS = (
    r"gh\s+api\b[^\n]*\bgit/refs/heads/" + re.escape(DEFAULT_BRANCH) + r"\b",
    r"gh\s+api\b[^\n]*\bcontents/[^\n]*\bref=" + re.escape(DEFAULT_BRANCH) + r"\b",
    r"gh\s+api\b[^\n]*\bcontents/[^\n]*\bbranch=" + re.escape(DEFAULT_BRANCH) + r"\b",
)

_PR_MERGE_PATTERNS = (r"gh\s+pr\s+merge\b",)

_AUTO_MERGE_FLAG_PATTERNS = (
    r"gh\s+pr\b[^\n]*--auto\b",
    r"gh\s+pr\b[^\n]*--admin\b",
    r"gh\s+pr\b[^\n]*--squash\b",
)

# This path is in the refresh workflow's write-scope allowlist but is
# generated at runtime by measure_refresh_diff and has never been
# committed to git -- an exact-equality write-scope assert built on a
# tracked-only listing could not see it, which is why the refresh job
# aborted before ever opening a pull request (dated 2026-08-12). It is
# exempt from the tracked-path assertion below. This exemption is
# one-directional on purpose: once a refresh pull request actually merges,
# the workflow's own `git add` will have committed the report and the path
# becomes tracked -- the assertion is "tracked OR exempt," so it stays
# correct across that transition rather than becoming a planted future
# failure.
UNTRACKED_ALLOWLIST_PATHS = frozenset({"data/overture-refresh-report.md"})

# Anchors the write-scope allowlist's `ALLOWED=$(printf ...)` construction
# and terminates at the `| sort)` line, so unrelated quoted strings
# elsewhere in the step ("$CHANGED", the $RUNNER_TEMP file path) can never
# be swept in.
_ALLOWLIST_BLOCK_PATTERN = re.compile(r"ALLOWED=\$\(printf.*?\| sort\)", re.DOTALL)


def _matches_any(patterns, text):
    return [pattern for pattern in patterns if re.search(pattern, text)]


def _load_workflow():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    return doc, text


def _trigger_block(doc):
    return doc.get("on", doc.get(True))


def _all_steps(doc):
    """Every step in every job, flattened, each tagged with its job name --
    used by checks that must hold across the whole file, not one job."""
    steps = []
    for job_name, job in doc["jobs"].items():
        for step in job.get("steps", []):
            steps.append((job_name, step))
    return steps


def _checkout_steps(doc):
    return [
        (job_name, step)
        for job_name, step in _all_steps(doc)
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]


def _step_run_text(step):
    return step.get("run", "") or ""


def _find_step_index(steps, substring):
    """Index of the first step (within an already-selected job's `steps`
    list) whose name or run text contains `substring`, or None. Returns an
    index, not the step itself, so callers can assert on ORDER, not merely
    presence."""
    for index, step in enumerate(steps):
        haystack = f"{step.get('name', '')}\n{_step_run_text(step)}"
        if substring in haystack:
            return index
    return None


def _write_scope_step(doc):
    """The `refresh` job's step whose name contains "write scope", or
    None."""
    refresh_steps = doc["jobs"]["refresh"]["steps"]
    index = _find_step_index(refresh_steps, "write scope")
    return refresh_steps[index] if index is not None else None


def _allowlist_paths(run_text):
    """The ordered list of quoted paths inside the `ALLOWED=$(printf ...)`
    block, or None if the block is not found."""
    match = _ALLOWLIST_BLOCK_PATTERN.search(run_text)
    if match is None:
        return None
    return re.findall(r'"([^"]+)"', match.group(0))


def _staged_paths(run_text):
    """The paths the commit step's `git add` stages, or None if the
    `git add` line is not found. Walks the lines following a line whose
    stripped form is exactly `git add \\`, collecting each stripped token
    with its trailing backslash removed, stopping after the first line
    that does not end in a backslash."""
    lines = run_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "git add \\":
            start = index + 1
            break
    if start is None:
        return None
    paths = []
    for line in lines[start:]:
        stripped = line.strip()
        has_continuation = stripped.endswith("\\")
        if has_continuation:
            stripped = stripped[:-1].strip()
        paths.append(stripped)
        if not has_continuation:
            break
    return paths


class WorkflowNonVacuityTests(SimpleTestCase):
    """Guards the whole module against a renamed or moved workflow file
    passing every check below vacuously over an empty or wrong document."""

    def test_workflow_file_parses_to_a_non_empty_document_with_expected_jobs(self):
        doc, _ = _load_workflow()
        self.assertTrue(doc)
        self.assertIn("jobs", doc)
        self.assertEqual(set(doc["jobs"].keys()), set(EXPECTED_JOB_NAMES))


class TriggerAndPermissionsTests(SimpleTestCase):
    def test_trigger_set_is_schedule_and_dispatch_only(self):
        doc, _ = _load_workflow()
        triggers = _trigger_block(doc)
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        # A fork pull request must never be able to reach the stored token.
        self.assertNotIn("pull_request", triggers)
        self.assertNotIn("pull_request_target", triggers)

    def test_cron_is_weekly_with_a_non_zero_minute(self):
        doc, _ = _load_workflow()
        schedules = _trigger_block(doc)["schedule"]
        self.assertEqual(len(schedules), 1)
        cron = schedules[0]["cron"]
        minute, hour, day_of_month, month, day_of_week = cron.split()
        self.assertNotEqual(minute, "0")
        # Weekly: a single day-of-week value, every day-of-month/month.
        self.assertEqual(day_of_month, "*")
        self.assertEqual(month, "*")
        self.assertNotEqual(day_of_week, "*")

    def test_top_level_permissions_are_contents_read_only(self):
        doc, _ = _load_workflow()
        self.assertEqual(doc["permissions"], {"contents": "read"})

    def test_concurrency_group_declared_without_cancelling_in_progress_runs(self):
        doc, _ = _load_workflow()
        concurrency = doc["concurrency"]
        self.assertTrue(concurrency["group"])
        self.assertFalse(concurrency["cancel-in-progress"])


class CheckoutTokenTests(SimpleTestCase):
    """The single most likely real-world failure for this design (RESEARCH.md's
    anti-recursion pitfall): a checkout step that omits an explicit token
    silently re-authenticates a later push as the default token. Looping
    over every checkout step -- rather than asserting on named steps -- is
    what catches a checkout added later without one."""

    def test_every_checkout_step_sets_an_explicit_token_input(self):
        doc, _ = _load_workflow()
        checkout_steps = _checkout_steps(doc)
        # Non-vacuity: this design uses checkout at least twice (discover,
        # refresh) -- if that count ever drops to zero the loop below would
        # pass trivially over nothing.
        self.assertGreaterEqual(len(checkout_steps), 2)
        for job_name, step in checkout_steps:
            token = step.get("with", {}).get("token")
            self.assertTrue(
                token,
                f"checkout step in job {job_name!r} has no explicit 'token' input",
            )
            self.assertIn("secrets.", token)


class RefreshJobGatingAndOrderingTests(SimpleTestCase):
    def test_refresh_job_runs_only_when_newer_and_no_existing_branch(self):
        doc, _ = _load_workflow()
        refresh_job = doc["jobs"]["refresh"]
        self.assertEqual(refresh_job.get("needs"), "discover")
        condition = refresh_job.get("if", "")
        self.assertIn("is_newer", condition)
        self.assertIn("true", condition)
        self.assertIn("branch_exists", condition)
        self.assertIn("false", condition)

    def test_refresh_job_steps_appear_in_the_required_order(self):
        doc, _ = _load_workflow()
        steps = doc["jobs"]["refresh"]["steps"]

        count_only_index = _find_step_index(steps, "--count-only")
        extract_index = _find_step_index(steps, "fetch_overture_extract --force")
        transform_index = _find_step_index(steps, "import_overture_stations")
        measure_index = _find_step_index(steps, "measure_refresh_diff")
        rewrite_index = _find_step_index(steps, "bump_overture_release")
        commit_index = _find_step_index(steps, "git commit")

        # Located, not None -- a renamed or restructured step must fail
        # loudly here rather than pass over a missing index.
        for label, index in (
            ("count-only gate", count_only_index),
            ("real extract", extract_index),
            ("transform", transform_index),
            ("measurement", measure_index),
            ("release rewrite", rewrite_index),
            ("commit", commit_index),
        ):
            self.assertIsNotNone(index, f"could not locate the {label} step")

        self.assertLess(count_only_index, extract_index)
        self.assertLess(extract_index, transform_index)
        self.assertLess(transform_index, measure_index)
        self.assertLess(measure_index, rewrite_index)
        self.assertLess(rewrite_index, commit_index)


class BranchPrefixAgreementTests(SimpleTestCase):
    """D-09's `overture-refresh/` prefix is written as a literal at three
    independent sites -- the refresh job's branch-creation step, the
    discover job's branch-exists check, and the staleness sweep's
    head-branch filter. A drift between them has no runtime symptom at
    all (pull requests keep opening, duplicate suppression keeps appearing
    to work, the staleness signal simply never fires on anything), which is
    exactly why this assertion earns its place."""

    def test_all_three_branch_prefix_sites_agree(self):
        doc, _ = _load_workflow()

        discover_steps = doc["jobs"]["discover"]["steps"]
        refresh_steps = doc["jobs"]["refresh"]["steps"]
        sweep_steps = doc["jobs"]["stale_sweep"]["steps"]

        branch_check_step = discover_steps[
            _find_step_index(discover_steps, "branch already exists")
        ]
        commit_step = refresh_steps[_find_step_index(refresh_steps, "git commit")]
        sweep_step = sweep_steps[_find_step_index(sweep_steps, "Sweep open refresh")]

        discover_match = re.search(
            r'BRANCH="([^$"]+)\$\{\{', _step_run_text(branch_check_step)
        )
        refresh_match = re.search(
            r'checkout -b "([^$"]+)\$\{NEWEST_RELEASE\}"', _step_run_text(commit_step)
        )
        sweep_match = re.search(
            r'BRANCH_PREFIX="([^"]+)"', _step_run_text(sweep_step)
        )

        # Precondition: all three sites were actually located. Asserted
        # before the equality check so a renamed or restructured step makes
        # this fail loudly rather than silently pass over an empty set.
        self.assertIsNotNone(discover_match, "discover job's branch-exists check not found")
        self.assertIsNotNone(refresh_match, "refresh job's branch-creation step not found")
        self.assertIsNotNone(sweep_match, "staleness sweep's head-branch filter not found")

        prefixes = {discover_match.group(1), refresh_match.group(1), sweep_match.group(1)}
        self.assertEqual(prefixes, {EXPECTED_BRANCH_PREFIX})


class WriteScopeStaticGuardTests(SimpleTestCase):
    """The static counterpart to the refresh job's own runtime write-scope
    assertion: no step anywhere in this file may write under the tests
    tree, and no step may reference the dispatch admission manifest by
    name. This is the property that makes the automation structurally
    unable to turn a red guard green."""

    def test_no_step_writes_under_the_tests_tree_or_names_the_admission_manifest(self):
        doc, _ = _load_workflow()
        for job_name, step in _all_steps(doc):
            run_text = _step_run_text(step)
            self.assertNotIn(
                "routing/tests",
                run_text,
                f"job {job_name!r} step {step.get('name')!r} references routing/tests",
            )
            self.assertNotIn(
                "ADMISSION_MANIFEST",
                run_text,
                f"job {job_name!r} step {step.get('name')!r} references ADMISSION_MANIFEST",
            )


class NoWriteToDefaultBranchOrMergeCommandTests(SimpleTestCase):
    """The agreed compensating control from 23-07-SUMMARY.md: the developer
    declined the no-bypass ruleset on `main` that would have made this
    property platform-enforced, so this guard is the only thing standing
    between a future edit to this file and a silent write path to the
    default branch. It does not restore platform enforcement -- it converts
    "the YAML happens not to push to main" into "a test fails the moment it
    does," which is the realistic failure mode this workflow being edited
    later, by a human or an agent, and quietly acquiring a write path to
    `main` or a way to merge its own pull request."""

    def test_no_step_pushes_to_the_default_branch(self):
        doc, _ = _load_workflow()
        for job_name, step in _all_steps(doc):
            run_text = _step_run_text(step)
            hits = _matches_any(_PUSH_TO_DEFAULT_BRANCH_PATTERNS, run_text)
            self.assertFalse(
                hits,
                f"job {job_name!r} step {step.get('name')!r} appears to push to "
                f"the default branch {DEFAULT_BRANCH!r}: matched {hits}",
            )

    def test_no_step_writes_to_the_default_branch_via_gh_api(self):
        doc, _ = _load_workflow()
        for job_name, step in _all_steps(doc):
            run_text = _step_run_text(step)
            hits = _matches_any(_GH_API_DEFAULT_BRANCH_WRITE_PATTERNS, run_text)
            self.assertFalse(
                hits,
                f"job {job_name!r} step {step.get('name')!r} appears to write to "
                f"{DEFAULT_BRANCH!r} via the GitHub API: matched {hits}",
            )

    def test_no_step_merges_a_pull_request(self):
        doc, _ = _load_workflow()
        for job_name, step in _all_steps(doc):
            run_text = _step_run_text(step)
            hits = _matches_any(_PR_MERGE_PATTERNS, run_text)
            self.assertFalse(
                hits,
                f"job {job_name!r} step {step.get('name')!r} appears to merge a "
                f"pull request: matched {hits}",
            )

    def test_no_gh_pr_invocation_carries_an_auto_merge_flag(self):
        doc, _ = _load_workflow()
        for job_name, step in _all_steps(doc):
            run_text = _step_run_text(step)
            hits = _matches_any(_AUTO_MERGE_FLAG_PATTERNS, run_text)
            self.assertFalse(
                hits,
                f"job {job_name!r} step {step.get('name')!r} appears to carry an "
                f"auto-merge flag on a gh pr invocation: matched {hits}",
            )


class StaleSweepIndependenceTests(SimpleTestCase):
    def test_stale_sweep_job_has_no_dependency_on_the_other_two_jobs(self):
        doc, _ = _load_workflow()
        sweep_job = doc["jobs"]["stale_sweep"]
        self.assertIsNone(sweep_job.get("needs"))
        self.assertIsNone(sweep_job.get("if"))

    def test_stale_sweep_names_its_pinned_thresholds_and_label(self):
        _, text = _load_workflow()
        self.assertIn("STALE_THRESHOLD_DAYS=14", text)
        self.assertIn("RECOMMENT_INTERVAL_DAYS=7", text)
        self.assertIn("stale-refresh", text)


class RequirementsOfflineInstallSiteTests(SimpleTestCase):
    """Both the discover and refresh jobs install the Parquet/geo toolchain
    (discover_overture_release and fetch_overture_extract each lazily
    import duckdb) -- two install sites, one per job/runner, both
    byte-identical to ci.yml's own duckdb-fixture install line, never a
    third or divergently-written install mechanism."""

    def test_offline_requirements_install_line_matches_the_ci_convention_everywhere_it_appears(self):
        _, text = _load_workflow()
        install_lines = [
            line.strip()
            for line in text.splitlines()
            if "pip install" in line and "requirements-offline.txt" in line
        ]
        self.assertGreaterEqual(len(install_lines), 1)
        for line in install_lines:
            self.assertIn("pip install -r requirements.txt -r requirements-offline.txt", line)


class WriteScopeAllowlistIntegrityTests(SimpleTestCase):
    """Guards against an allowlist entry that names a file git can never
    report as changed -- a defect with no local symptom at all: every unit
    test passes, the YAML parses, and the failure only ever appears in a
    scheduled run nobody watches. This is milestone audit finding W3."""

    def test_write_scope_allowlist_parses_to_a_non_empty_path_list(self):
        doc, _ = _load_workflow()
        step = _write_scope_step(doc)
        self.assertIsNotNone(step, "could not locate the write-scope step")
        paths = _allowlist_paths(_step_run_text(step))
        self.assertIsNotNone(paths, "could not parse the write-scope allowlist block")
        self.assertTrue(paths)
        for anchor in (
            "data/overture_stations.csv",
            "data/overture-refresh-report.md",
            "routing/pipeline/overture_scope.py",
            "NOTICE",
        ):
            self.assertIn(anchor, paths)

    def test_every_allowlist_path_is_tracked_in_git_or_a_named_exemption(self):
        doc, _ = _load_workflow()
        step = _write_scope_step(doc)
        self.assertIsNotNone(step, "could not locate the write-scope step")
        paths = _allowlist_paths(_step_run_text(step))
        self.assertTrue(paths)

        result = subprocess.run(
            ["git", "ls-files", "--"] + paths,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked = {line.strip() for line in result.stdout.splitlines() if line.strip()}

        for path in paths:
            self.assertTrue(
                path in tracked or path in UNTRACKED_ALLOWLIST_PATHS,
                f"allowlist path {path!r} is neither tracked in git nor a declared "
                "runtime-generated exemption -- git can never report it as changed, "
                "so the refresh job's write-scope assert will abort before ever "
                "opening a pull request",
            )

    def test_the_untracked_exemption_set_has_exactly_its_one_documented_member(self):
        self.assertEqual(
            UNTRACKED_ALLOWLIST_PATHS, frozenset({"data/overture-refresh-report.md"})
        )

    def test_the_allowlist_and_the_staged_path_list_agree(self):
        """The same literal set of paths is written twice in this job, in the
        assert step and the commit step. Drift in one direction is
        self-detecting (`git add` errors on a path that does not exist);
        drift in the other is silent -- a file passes the write-scope check,
        is never staged, and quietly does not reach the pull request. This is
        the identical argument BranchPrefixAgreementTests already makes for
        the three "overture-refresh/" literals."""
        doc, _ = _load_workflow()
        refresh_steps = doc["jobs"]["refresh"]["steps"]

        write_scope_step = _write_scope_step(doc)
        self.assertIsNotNone(write_scope_step, "could not locate the write-scope step")
        allowed = _allowlist_paths(_step_run_text(write_scope_step))
        self.assertTrue(allowed)

        commit_index = _find_step_index(refresh_steps, "git commit")
        self.assertIsNotNone(commit_index, "could not locate the commit step")
        staged = _staged_paths(_step_run_text(refresh_steps[commit_index]))
        self.assertTrue(staged)

        self.assertEqual(set(allowed), set(staged))

    def test_the_write_scope_check_reads_untracked_files_not_only_tracked_modifications(self):
        """The B1 regression guard: the allowlist names a file that is
        created rather than modified, so a check built on a tracked-only
        listing can never see it, always mismatches, and exits 1 three steps
        before the pull request is opened -- this is milestone audit finding
        B1, reproduced twice independently before it was fixed."""
        doc, _ = _load_workflow()
        step = _write_scope_step(doc)
        self.assertIsNotNone(step, "could not locate the write-scope step")
        run_text = _step_run_text(step)
        self.assertIn("git status --porcelain --untracked-files=all", run_text)
        self.assertNotIn("git diff --name-only", run_text)

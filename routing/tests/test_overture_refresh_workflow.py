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

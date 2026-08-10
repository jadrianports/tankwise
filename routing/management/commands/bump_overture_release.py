"""The bounded release-string rewrite the refresh pull request's commit
carries (D-02/D-06/D-22).

This is a command, not a workflow shell step, so its write allowlist is
encoded in tested Python rather than in YAML. It mutates checked-out source
in a working tree; it never influences a fetch or an import at run time, so
Phase 22's rejection of a release CLI flag stays untouched -- every
consumer still imports `overture_scope.OVERTURE_RELEASE`, the one source
of truth.

Its core, `rewrite_release`, is a pure function: it takes an explicit root
and performs no repository-root lookup of its own, which is what lets a
test exercise it against a temporary tree instead of the real checkout. It
writes exactly two kinds of thing:

  - the `OVERTURE_RELEASE` assignment line in `overture_scope.py` under the
    given root -- and only that line, via a regex anchored to the exact
    current value, leaving the surrounding comment prose alone;
  - every file named in `overture_scope.RELEASE_RESTATEMENT_FILES`,
    resolved under the given root -- the same tuple plan 23-01's
    consistency guard checks, so the guard and this writer can never
    silently disagree about which files are in scope.

It writes nothing else. Not a path under `routing/tests`, not the dispatch
admission manifest, not either generated report under `data/` -- those are
rewritten wholesale by their own generating commands on every run, and a
substitution-based edit would make a generated report describe a run that
never happened.
"""
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.pipeline import overture_scope

# Repo-root-relative, POSIX-style, mirroring RELEASE_RESTATEMENT_FILES's own
# path convention.
SCOPE_MODULE_RELATIVE_PATH = "routing/pipeline/overture_scope.py"


def _release_assignment_pattern(old):
    """A regex anchored to the start of the assignment line, matching only
    `OVERTURE_RELEASE = "<old>"` exactly -- never a shorter or partial
    match, and never any of the surrounding comment prose above it."""
    return re.compile(rf'^OVERTURE_RELEASE = "{re.escape(old)}"$', re.MULTILINE)


def rewrite_release(root, old, new):
    """Pure rewrite over the tree rooted at `root`: substitutes `old` for
    `new` in exactly the `OVERTURE_RELEASE` assignment line of
    `overture_scope.py`, and in every occurrence inside each file named in
    `overture_scope.RELEASE_RESTATEMENT_FILES`. Returns a mapping of
    root-relative path to substitution count. Raises `CommandError` naming
    the offending path when the constant assignment is not found exactly
    once, or when a restatement file contains zero occurrences of `old` --
    the latter is deliberate: a prose file that stopped restating the
    release, or was renamed, must stop the rewrite rather than silently
    drift and fail the consistency guard downstream for a reason nobody can
    trace.

    Takes no repository-root lookup of its own -- `root` is always the
    caller's, which is what makes this function fully testable against a
    temporary tree."""
    root = Path(root)
    results = {}

    scope_path = root / SCOPE_MODULE_RELATIVE_PATH
    text = scope_path.read_text(encoding="utf-8")
    pattern = _release_assignment_pattern(old)
    new_text, count = pattern.subn(f'OVERTURE_RELEASE = "{new}"', text)
    if count != 1:
        raise CommandError(
            f"Expected exactly one 'OVERTURE_RELEASE = \"{old}\"' "
            f"assignment line in {scope_path}, found {count}."
        )
    scope_path.write_text(new_text, encoding="utf-8")
    results[SCOPE_MODULE_RELATIVE_PATH] = count

    for relative_path in overture_scope.RELEASE_RESTATEMENT_FILES:
        path = root / relative_path
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise CommandError(
                f"{relative_path} does not restate release {old!r} -- "
                "either it stopped restating the release or was renamed. "
                "Refusing to rewrite silently."
            )
        path.write_text(text.replace(old, new), encoding="utf-8")
        results[relative_path] = count

    return results


class Command(BaseCommand):
    help = (
        "Bounded release-string rewrite: substitutes the pinned "
        "OVERTURE_RELEASE constant and its restatement in each of "
        "overture_scope.RELEASE_RESTATEMENT_FILES, and nothing else. "
        "Mutates checked-out source in a working tree; never influences a "
        "fetch or an import at run time."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "new_release",
            help="The new OVERTURE_RELEASE value to bump to, e.g. 2026-08-19.0.",
        )
        parser.add_argument(
            "--root",
            dest="root",
            default=str(Path(settings.BASE_DIR)),
            help="Root directory to rewrite under. Default: the project base directory.",
        )

    def handle(self, *args, **options):
        new_release = options["new_release"]
        root = options["root"]
        current_release = overture_scope.OVERTURE_RELEASE

        if not overture_scope.is_well_formed_release(new_release):
            raise CommandError(
                f"{new_release!r} is not a well-formed release string "
                "(expected shape: YYYY-MM-DD.N)."
            )

        if new_release == current_release:
            raise CommandError(
                f"New release {new_release!r} equals the current pinned "
                "release -- nothing to bump."
            )

        if not new_release > current_release:
            raise CommandError(
                f"New release {new_release!r} does not sort strictly "
                f"after the current pinned release {current_release!r}. A "
                "deliberate rollback to an older release is a human edit, "
                "not this command's job."
            )

        results = rewrite_release(root, current_release, new_release)

        for path, count in results.items():
            self.stdout.write(f"{path}: {count} substitution(s)")

"""Offline discovery of the newest Overture release available in the
anonymous public S3 bucket the extract already reads (D-01).

Structured on `fetch_overture_extract`'s shape: a lazy `import duckdb`
inside `handle()` -- never at module scope, since
`DuckdbModuleScopeImportGuardTests` (routing/tests/test_fetch_overture_extract.py)
scans every module reachable from `wsgi.py`/`manage.py` startup, including
this one -- and one thin execute wrapper, `_run_glob_query`, as the single
boundary the test suite mocks.

This command adds no new authentication and no new dependency: it reuses
the exact same anonymous, unsigned S3 access `fetch_overture_extract`
already performs, against the same bucket. Only the `httpfs` DuckDB
extension is installed/loaded -- the `spatial` extension is not needed for
a directory listing and loading it anyway would be cargo cult.

`stdout` carries exactly three machine-readable `key=value` lines and
nothing else -- the refresh workflow appends this stream straight into its
GitHub Actions step outputs, so a styled or chatty stdout write would
corrupt it. Every human-readable line (the discovered release set, its
count, the query timing, the stale-pin warning) goes to `stderr` instead.
"""
import re
import time

from django.core.management.base import BaseCommand, CommandError

from routing.pipeline import overture_scope

# The release segment is whatever sits directly between "release/" and the
# next path separator in a returned S3 key, e.g.
# "s3://overturemaps-us-west-2/release/2026-07-22.0/theme=places/type=place/
# part-00000.parquet" -> "2026-07-22.0". A single release produces many
# partition files sharing this same segment, which is why the extractor
# below dedupes.
_RELEASE_SEGMENT_PATTERN = re.compile(r"/release/([^/]+)/")


def _release_glob_pattern():
    """The glob pattern this command lists against: exactly
    `overture_scope.OVERTURE_S3_PATH_TEMPLATE` with the release slot
    wildcarded. Never a second hand-written path literal -- deriving the
    pattern from the same template `overture_s3_path()` formats is what
    makes discovery and the real extract unable to desync.

    A shorter, more "obvious" form -- wildcarding only the path segment
    directly under the release prefix, e.g.
    `s3://overturemaps-us-west-2/release/*` -- was verified live to return
    zero rows with no error: S3 has no real directories, object keys for a
    single release sit several segments deeper
    (`release/<release>/theme=places/type=place/<file>.parquet`), and a
    single wildcard segment does not cross a `/` separator. That failure is
    silent: an empty result there is indistinguishable from "no new release
    exists". The full template wildcarded only at the release slot was
    verified live at 2.97s, returning real results.
    """
    return overture_scope.OVERTURE_S3_PATH_TEMPLATE.format(release="*")


def _run_glob_query(connection, sql):
    """Thin wrapper around the one DuckDB call this command makes -- the
    single boundary the test suite mocks (mirrors
    `fetch_overture_extract._run_extract_query`'s own convention)."""
    return connection.execute(sql).fetchall()


def _releases_from_paths(rows):
    """Pure: extract the release segment from each row's path (the segment
    that follows the `release/` prefix), dedupe across the many partition
    files a single release produces, and return the result sorted
    ascending. No I/O.

    Ordering is a plain string comparison -- the release naming shape
    (`YYYY-MM-DD.N`) is fixed-width up to the sequence suffix and dates sort
    identically as strings or as dates, so lexicographic order is
    chronological order here.
    """
    releases = set()
    for row in rows:
        path = row[0]
        match = _RELEASE_SEGMENT_PATTERN.search(path)
        if match:
            releases.add(match.group(1))
    return sorted(releases)


class Command(BaseCommand):
    help = (
        "Discover the newest Overture release available in the anonymous "
        "public S3 bucket fetch_overture_extract already reads, by "
        "wildcarding the release slot of the existing path template. "
        "Emits three machine-readable key=value lines on stdout for the "
        "refresh workflow to consume as step outputs; every human-readable "
        "line goes to stderr instead."
    )

    def handle(self, *args, **options):
        # Lazy import -- see fetch_overture_extract's module docstring and
        # DuckdbModuleScopeImportGuardTests, which now also covers this
        # module's own lazy import.
        import duckdb

        connection = duckdb.connect()
        # Only httpfs -- no spatial extension. This command reads no
        # geometry column, so loading spatial would be cargo cult copied
        # from the extract command rather than something this glob needs.
        connection.execute("INSTALL httpfs; LOAD httpfs;")
        # Anonymous access is sufficient -- the Overture bucket is genuinely
        # public and DuckDB falls through to unsigned requests when no
        # credential provider resolves.
        connection.execute(f"SET s3_region='{overture_scope.OVERTURE_S3_REGION}';")

        pattern = _release_glob_pattern()
        sql = f"SELECT file FROM glob('{pattern}')"

        start = time.monotonic()
        rows = _run_glob_query(connection, sql)
        duration_s = time.monotonic() - start

        releases = _releases_from_paths(rows)

        if not releases:
            raise CommandError(
                f"Discovery glob {pattern!r} returned zero releases. An "
                "empty glob and 'no new release exists' are "
                "indistinguishable to a caller, so this is a hard failure "
                "rather than a silently returned empty result -- see this "
                "command's module docstring."
            )

        pinned = overture_scope.OVERTURE_RELEASE
        newest = releases[-1]

        self.stderr.write(f"Discovered releases: {releases}")
        self.stderr.write(f"Count: {len(releases)}")
        self.stderr.write(f"Query duration: {duration_s:.2f}s")

        if pinned not in releases:
            self.stderr.write(
                self.style.WARNING(
                    f"Pinned release {pinned!r} not found among the "
                    f"discovered releases {releases} -- the bucket "
                    "retains only a short release history and has likely "
                    "aged the pinned one out. Not treated as a failure."
                )
            )

        is_newer = newest > pinned

        # Exactly three lines, unstyled, nothing else -- the refresh
        # workflow's step outputs consume this stream directly.
        self.stdout.write(f"pinned_release={pinned}")
        self.stdout.write(f"newest_release={newest}")
        self.stdout.write(f"is_newer={'true' if is_newer else 'false'}")

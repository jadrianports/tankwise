"""One-time, ONLINE, developer-machine-only capture of Overture Places rows
inside the gap-fill scope, via DuckDB's httpfs/spatial extensions reading
straight off the public Overture S3 bucket.

[AMENDED 2026-08-11] The opening line above describes this command's
original, one-time developer-machine capture. As of this phase, the
`--count-only` mode also runs inside the scheduled refresh workflow, as a
fast source-integrity pre-check against the real bucket before the full
extract this command otherwise performs. The full extract itself remains a
one-time, developer-machine-only capture per that original line; only the
count-only mode is now also invoked from CI-adjacent, automated context.
This does NOT relax the statement below that this command must not run in
CI's `backend-sqlite` or `backend-postgres` jobs -- that remains true and
load-bearing; the refresh workflow that runs `--count-only` is a separate,
dedicated job, not either of those two.

This command is the ONLY consumer of the Parquet/geo toolchain
(`requirements-offline.txt`, never `requirements.txt`) anywhere in this
repository. `DuckdbModuleScopeImportGuardTests`
(routing/tests/test_fetch_overture_extract.py) is the static proof that no
module reachable from `wsgi.py` or `manage.py` startup imports it -- that
guard, not the lazy import alone, is what actually proves a production
environment that never installed `requirements-offline.txt` still boots
gunicorn cleanly.

Every filter parameter -- release, bbox, category set, confidence floor --
comes from `routing.pipeline.overture_scope` and is pinned in code, never a
CLI flag (D-03/D-05). This command applies NO hygiene filtering of its own
(closed-status, mojibake, alternative-fuel, dedup) -- that is the pure-Python
transform's job (`import_overture_stations`, plan 22-10, D-24). The extract
this command writes is the audit trail those hygiene decisions are made
against.

Must NOT run in CI's `backend-sqlite` or `backend-postgres` jobs -- those two
jobs' installed set is itself part of the evidence that the production image
does not need this toolchain. The dedicated `duckdb-fixture` CI job
(.github/workflows/ci.yml) exercises this module's query-building logic
against a tiny committed Parquet fixture instead.

A fresh developer machine's first invocation pays a brief one-time network
fetch of the httpfs/spatial extension binaries (a few seconds) before the
real query starts -- not the query itself.

Recorded forward risk, not a task here: the `categories` field this command
filters on is deprecated as of the pinned release and is scheduled for
removal in the September 2026 Overture release, replaced by
`basic_category`/`taxonomy`. See the written report's own note. A refresh
run against a later release (Phase 23) must migrate this command's category
predicate at that point.

[AMENDED 2026-08-11] The migration described above has happened. `_extract_sql`
now reads `taxonomy.primary` -- never `categories.primary` -- in both its
SELECT projection and its WHERE membership predicate. `CATEGORY_FILTER` is
unchanged. The committed `data/overture-extract-report.md` still carries the
OLD note text above (`_FORWARD_RISK_NOTE`'s pre-migration wording) because
that file is regenerated wholesale by this command's next real run against
the real Overture bucket, not hand-edited here.
"""
import csv
import logging
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.pipeline import overture_scope

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_PATH = Path(settings.BASE_DIR) / "data" / "overture_raw_extract.csv"
DEFAULT_REPORT_PATH = Path(settings.BASE_DIR) / "data" / "overture-extract-report.md"

# Written column order, positional (csv.writer against this header, never
# DictWriter) -- mirrors geocode_stations._export_csv's own convention.
RAW_EXTRACT_HEADER = [
    "gers_id",
    "name",
    "brand_name",
    "address_freeform",
    "address_locality",
    "address_region",
    "address_postcode",
    "category",
    "confidence",
    "operating_status",
    "longitude",
    "latitude",
]

# [AMENDED 2026-08-11] Original wording (kept for the record, no longer
# current): "The `categories` field this extract filters on is deprecated
# as of the pinned release and is scheduled for removal in the September
# 2026 Overture release, replaced by `basic_category` and `taxonomy`. A
# refresh run against a later release must migrate this command's category
# predicate before that release ships."
_FORWARD_RISK_NOTE = (
    "This command's category predicate was migrated off the deprecated "
    "`categories` field to `taxonomy.primary` on 2026-08-11, ahead of the "
    "September 2026 Overture release in which `categories` is removed. "
    "`CATEGORY_FILTER`'s two values are unchanged; only the struct path "
    "reading them changed."
)


def _where_clause():
    """The WHERE predicate fragment shared by `_extract_sql` and
    `_count_only_sql` -- the two-box bbox predicate, the category membership
    test against `overture_scope.CATEGORY_FILTER`, and the confidence floor.
    Extracted into one place so the count query and the extract query can
    never disagree about which rows they describe: the count-only pre-check
    exists to assert an expected row count for the exact rows the real
    extract will later read, and a second, independently-written copy of
    this predicate would be a second place for that agreement to silently
    break."""
    category_list = ", ".join(f"'{c}'" for c in overture_scope.CATEGORY_FILTER)
    return (
        f"({overture_scope.bbox_predicate_sql()}) "
        f"AND taxonomy.primary IN ({category_list}) "
        f"AND confidence >= {overture_scope.CONFIDENCE_FLOOR}"
    )


def _extract_sql(source_path):
    """Build the one SQL statement the real extract issues. `source_path` is
    the `read_parquet(...)` FROM target -- the real S3 URI in production
    (`overture_scope.overture_s3_path()`), or a local Parquet fixture path
    when exercised by the CI `duckdb-fixture` job against
    `routing/tests/fixtures/overture/places_sample.parquet`. Every predicate
    is pinned from `overture_scope`; nothing here is a CLI flag
    (D-03/D-05). No hygiene predicate (operating_status, mojibake,
    alt-fuel) is applied -- that is the transform's job, not this fetch."""
    return (
        "SELECT "
        "id AS gers_id, "
        "names.primary AS name, "
        "brand.names.primary AS brand_name, "
        "addresses[1].freeform AS address_freeform, "
        "addresses[1].locality AS address_locality, "
        "addresses[1].region AS address_region, "
        "addresses[1].postcode AS address_postcode, "
        "taxonomy.primary AS category, "
        "confidence, "
        "operating_status, "
        "ST_X(geometry) AS longitude, "
        "ST_Y(geometry) AS latitude "
        f"FROM read_parquet('{source_path}', hive_partitioning=1) "
        f"WHERE {_where_clause()}"
    )


def _count_only_sql(source_path):
    """Sibling of `_extract_sql`: a `COUNT(*)` over the same
    `read_parquet(...)` target with the identical `_where_clause()`
    fragment and no projection columns. This is the query the `--count-only`
    pre-check runs -- seconds, not the ~512s the real extract takes --
    against the real bucket before any transform or diff logic runs."""
    return (
        "SELECT COUNT(*) "
        f"FROM read_parquet('{source_path}', hive_partitioning=1) "
        f"WHERE {_where_clause()}"
    )


def _run_extract_query(connection, sql):
    """Thin wrapper around a DuckDB call this command makes -- shared by
    both the real extract query and the count-only pre-check's `COUNT(*)`
    query, so this is no longer literally "the one DuckDB call" singular.
    It remains the single boundary the test suite mocks (mirrors
    test_geocode_stations.py's mock-the-query-layer precedent). Everything
    else in this module is reachable and testable without the Parquet
    toolchain installed."""
    return connection.execute(sql).fetchall()


def _open_duckdb_connection(duckdb_module):
    """Open a DuckDB connection with the httpfs/spatial extensions loaded
    and the pinned S3 region configured -- the setup both the real extract
    and the count-only pre-check need before either issues a query against
    the real bucket."""
    connection = duckdb_module.connect()
    # Runtime extensions, not pip packages -- INSTALL performs a one-time
    # network fetch of the extension binary on a fresh developer machine (a
    # few seconds), separate from and prior to the real query.
    connection.execute("INSTALL httpfs; LOAD httpfs;")
    connection.execute("INSTALL spatial; LOAD spatial;")
    # Anonymous access is sufficient -- the Overture bucket is genuinely
    # public and DuckDB falls through to unsigned requests when no
    # credential provider resolves. No credential plumbing of any kind.
    connection.execute(f"SET s3_region='{overture_scope.OVERTURE_S3_REGION}';")
    return connection


def _committed_extract_row_count(path):
    """Return the number of data rows (header excluded) in the committed
    raw-extract CSV at `path` -- the observed baseline the count-only band
    is applied to. Raises `CommandError` naming `path` when the file is
    missing or carries no data rows: a zero baseline would make the band
    vacuous, accepting any source count at all."""
    path = Path(path)
    if not path.exists():
        raise CommandError(
            f"Committed baseline extract not found at {path} -- cannot "
            "derive a count-only tolerance band without it."
        )
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            next(reader)
        except StopIteration:
            raise CommandError(
                f"Committed baseline extract at {path} is empty (no header "
                "row) -- cannot derive a count-only tolerance band."
            )
        count = sum(1 for _ in reader)
    if count == 0:
        raise CommandError(
            f"Committed baseline extract at {path} has a header row but "
            "zero data rows -- a zero baseline would make the count-only "
            "tolerance band vacuous, accepting any source count."
        )
    return count


def _count_band(baseline):
    """Return `(floor, ceiling)` -- the integer bounds produced by applying
    `overture_scope.RAW_EXTRACT_COUNT_BAND` to `baseline`. Pure."""
    lo_multiplier, hi_multiplier = overture_scope.RAW_EXTRACT_COUNT_BAND
    return int(baseline * lo_multiplier), int(baseline * hi_multiplier)


def _render_report(
    *,
    release,
    boxes,
    category_filter,
    confidence_floor,
    raw_row_count,
    written_count,
    skipped_buckets,
    byte_size,
    duration_s,
):
    lines = [
        "# Overture Raw Extract Report",
        "",
        f"- Release: {release}",
        "- Gap-fill boxes:",
    ]
    for box in boxes:
        lines.append(
            f"  - {box.label}: lng [{box.xmin}, {box.xmax}], lat [{box.ymin}, {box.ymax}]"
        )
    lines += [
        f"- Category filter: {', '.join(category_filter)}",
        f"- Confidence floor: {confidence_floor}",
        f"- Raw rows returned: {raw_row_count}",
        f"- Rows written: {written_count}",
        "- Rows skipped:",
    ]
    if skipped_buckets:
        for bucket, count in skipped_buckets.items():
            lines.append(f"  - {bucket}: {count}")
    else:
        lines.append("  - (none)")
    lines += [
        f"- Output byte size: {byte_size}",
        f"- Query wall-clock duration: {duration_s:.1f}s",
        "",
        "## Forward risk",
        "",
        _FORWARD_RISK_NOTE,
        "",
    ]
    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "One-time, ONLINE, developer-machine-only capture of Overture "
        "Places rows inside the gap-fill scope (routing.pipeline."
        "overture_scope). The only consumer of the Parquet/geo toolchain "
        "in this repository -- must NOT run in CI's backend-sqlite or "
        "backend-postgres jobs. Writes a committed CSV extract that the "
        "pure-Python transform (import_overture_stations, plan 22-10) "
        "reads; applies no hygiene filtering of its own."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-path",
            dest="output_path",
            default=str(DEFAULT_OUTPUT_PATH),
            help="Where to write the raw CSV extract. Default: data/overture_raw_extract.csv",
        )
        parser.add_argument(
            "--report-path",
            dest="report_path",
            default=str(DEFAULT_REPORT_PATH),
            help="Where to write the extract report. Default: data/overture-extract-report.md",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Overwrite an existing extract. Default: a second run "
            "against an existing output path is a no-op.",
        )
        parser.add_argument(
            "--count-only",
            dest="count_only",
            action="store_true",
            default=False,
            help="Run only the fast COUNT(*) source-integrity pre-check "
            "against the real bucket and exit -- no CSV or report is "
            "written. Aborts non-zero when the source count falls outside "
            "the tolerance band derived from --baseline-path's row count. "
            "Evaluated before the existing output-exists no-op check.",
        )
        parser.add_argument(
            "--baseline-path",
            dest="baseline_path",
            default=str(DEFAULT_OUTPUT_PATH),
            help="Committed raw-extract CSV whose row count is the "
            "count-only band's baseline. Default: data/overture_raw_extract.csv",
        )

    def handle(self, *args, **options):
        # Imported here, not at module scope, so a production environment
        # that never installed requirements-offline.txt can still import
        # this module and every other module reachable from wsgi.py /
        # manage.py startup, and boot gunicorn cleanly. See
        # DuckdbModuleScopeImportGuardTests.
        import duckdb

        # Evaluated BEFORE the output-exists no-op check below. In the
        # refresh workflow the committed extract always exists, so if this
        # branch were ordered after that check it would never run there --
        # silently disabling the one pre-check that catches a truncated or
        # partially-written source read before the ~512s real extract.
        if options["count_only"]:
            baseline = _committed_extract_row_count(options["baseline_path"])
            floor, ceiling = _count_band(baseline)

            connection = _open_duckdb_connection(duckdb)
            sql = _count_only_sql(overture_scope.overture_s3_path())
            ((source_count,),) = _run_extract_query(connection, sql)

            self.stdout.write(
                f"Count-only: source_count={source_count} baseline={baseline} "
                f"band=[{floor}, {ceiling}]"
            )

            if not (floor <= source_count <= ceiling):
                raise CommandError(
                    f"Source integrity failure: observed source count "
                    f"{source_count} falls outside the tolerance band "
                    f"[{floor}, {ceiling}] derived from baseline {baseline}. "
                    "This is a source integrity failure, not a data change "
                    "to accept."
                )
            return

        output_path = Path(options["output_path"])
        report_path = Path(options["report_path"])
        force = options["force"]

        if output_path.exists() and not force:
            self.stdout.write(
                f"{output_path} already exists -- pass --force to overwrite. No-op."
            )
            return

        connection = _open_duckdb_connection(duckdb)

        sql = _extract_sql(overture_scope.overture_s3_path())

        start = time.monotonic()
        rows = _run_extract_query(connection, sql)
        duration_s = time.monotonic() - start

        written = 0
        skipped_buckets = {"malformed_coordinate": 0}

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(RAW_EXTRACT_HEADER)
            for row in rows:
                (
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
                ) = row

                try:
                    longitude = float(longitude)
                    latitude = float(latitude)
                except (TypeError, ValueError):
                    skipped_buckets["malformed_coordinate"] += 1
                    logger.warning(
                        "Skipping row with malformed coordinate: gers_id=%r "
                        "longitude=%r latitude=%r",
                        gers_id,
                        longitude,
                        latitude,
                    )
                    continue

                writer.writerow(
                    [
                        gers_id or "",
                        name or "",
                        brand_name or "",
                        freeform or "",
                        locality or "",
                        region or "",
                        postcode or "",
                        category or "",
                        confidence if confidence is not None else "",
                        operating_status or "",
                        longitude,
                        latitude,
                    ]
                )
                written += 1

        byte_size = output_path.stat().st_size

        if byte_size > overture_scope.EXTRACT_SIZE_FINDING_THRESHOLD_BYTES:
            self.stdout.write(
                self.style.ERROR(
                    f"Extract is {byte_size} bytes, over the "
                    f"{overture_scope.EXTRACT_SIZE_FINDING_THRESHOLD_BYTES}-byte "
                    "pre-decided threshold. This is reported as a finding, "
                    "not fixed by simplifying the extract -- see the "
                    "plan's SUMMARY."
                )
            )

        report_text = _render_report(
            release=overture_scope.OVERTURE_RELEASE,
            boxes=overture_scope.GAP_FILL_BOXES,
            category_filter=overture_scope.CATEGORY_FILTER,
            confidence_floor=overture_scope.CONFIDENCE_FLOOR,
            raw_row_count=len(rows),
            written_count=written,
            skipped_buckets=skipped_buckets,
            byte_size=byte_size,
            duration_s=duration_s,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")

        if skipped_buckets["malformed_coordinate"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {skipped_buckets['malformed_coordinate']} "
                    "row(s) with a malformed coordinate"
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Extract: release={overture_scope.OVERTURE_RELEASE} "
                f"raw_rows={len(rows)} written={written} "
                f"skipped={sum(skipped_buckets.values())} bytes={byte_size} "
                f"duration={duration_s:.1f}s"
            )
        )
        self.stdout.write(self.style.SUCCESS(f"Report written to {report_path}"))

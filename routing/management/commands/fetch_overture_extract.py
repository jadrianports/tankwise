"""One-time, ONLINE, developer-machine-only capture of Overture Places rows
inside the gap-fill scope, via DuckDB's httpfs/spatial extensions reading
straight off the public Overture S3 bucket.

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
"""
import csv
import logging
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

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

_FORWARD_RISK_NOTE = (
    "The `categories` field this extract filters on is deprecated as of "
    "the pinned release and is scheduled for removal in the September "
    "2026 Overture release, replaced by `basic_category` and `taxonomy`. "
    "A refresh run against a later release must migrate this command's "
    "category predicate before that release ships."
)


def _extract_sql(source_path):
    """Build the one SQL statement this command issues. `source_path` is
    the `read_parquet(...)` FROM target -- the real S3 URI in production
    (`overture_scope.overture_s3_path()`), or a local Parquet fixture path
    when exercised by the CI `duckdb-fixture` job against
    `routing/tests/fixtures/overture/places_sample.parquet`. Every predicate
    is pinned from `overture_scope`; nothing here is a CLI flag
    (D-03/D-05). No hygiene predicate (operating_status, mojibake,
    alt-fuel) is applied -- that is the transform's job, not this fetch."""
    category_list = ", ".join(f"'{c}'" for c in overture_scope.CATEGORY_FILTER)
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
        f"WHERE ({overture_scope.bbox_predicate_sql()}) "
        f"AND taxonomy.primary IN ({category_list}) "
        f"AND confidence >= {overture_scope.CONFIDENCE_FLOOR}"
    )


def _run_extract_query(connection, sql):
    """Thin wrapper around the one DuckDB call this command makes -- the
    single boundary the test suite mocks (mirrors
    test_geocode_stations.py's mock-the-query-layer precedent). Everything
    else in this module is reachable and testable without the Parquet
    toolchain installed."""
    return connection.execute(sql).fetchall()


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

    def handle(self, *args, **options):
        # Imported here, not at module scope, so a production environment
        # that never installed requirements-offline.txt can still import
        # this module and every other module reachable from wsgi.py /
        # manage.py startup, and boot gunicorn cleanly. See
        # DuckdbModuleScopeImportGuardTests.
        import duckdb

        output_path = Path(options["output_path"])
        report_path = Path(options["report_path"])
        force = options["force"]

        if output_path.exists() and not force:
            self.stdout.write(
                f"{output_path} already exists -- pass --force to overwrite. No-op."
            )
            return

        connection = duckdb.connect()
        # Runtime extensions, not pip packages -- INSTALL performs a
        # one-time network fetch of the extension binary on a fresh
        # developer machine (a few seconds), separate from and prior to
        # the real query below.
        connection.execute("INSTALL httpfs; LOAD httpfs;")
        connection.execute("INSTALL spatial; LOAD spatial;")
        # Anonymous access is sufficient -- the Overture bucket is
        # genuinely public and DuckDB falls through to unsigned requests
        # when no credential provider resolves. No credential plumbing of
        # any kind.
        connection.execute(f"SET s3_region='{overture_scope.OVERTURE_S3_REGION}';")

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

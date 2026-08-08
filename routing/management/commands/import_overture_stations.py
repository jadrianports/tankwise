"""Thin offline command over the pure `overture.transform()`.

Reads the committed raw Overture extract (`data/overture_raw_extract.csv`),
calls `routing.pipeline.overture.transform()`, and writes the committed
station CSV plus the committed import report -- unconditionally, whatever
the outcome. Zero network calls at any time, consumes no database, and
never imports `Station`. Every hygiene, price and identity decision belongs
to `routing.pipeline.overture` (plan 22-10, D-24); this command only reads,
delegates, and writes -- so plan 22-11 inserting the dedup stage into
`transform()` is a one-call change here, not a rewrite.

Determinism is a hard requirement, from three sources: `overture.transform`
already sorts its output by minted `opis_id` ascending; every write below
opens with `newline=""` and lets `csv.writer` apply its fixed dialect
terminator, so the file does not vary by platform; and every `Decimal`
crossing into the CSV goes through `_format_decimal`, one shared helper
quantized to `Station`'s own `decimal_places=8`, so the same numeric value
never renders two different ways across two runs.
"""
import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from routing.pipeline import overture

DEFAULT_INPUT_PATH = Path(settings.BASE_DIR) / "data" / "overture_raw_extract.csv"
DEFAULT_OUTPUT_PATH = Path(settings.BASE_DIR) / "data" / "overture_stations.csv"
DEFAULT_REPORT_PATH = Path(settings.BASE_DIR) / "data" / "overture-import-report.md"

# Matches Station's retail_price/price_min/price_max/latitude/longitude,
# all DecimalField(decimal_places=8). Routed through this one helper so a
# Decimal parsed from the raw extract at varying source precision (e.g.
# "-96.797") never reaches the CSV at that ambient precision -- that would
# be a real way byte-identity is lost across two runs of the same input.
_DECIMAL_QUANTIZE = Decimal("0.00000001")


def _format_decimal(value):
    return str(Decimal(value).quantize(_DECIMAL_QUANTIZE))


def _render_report(report):
    lines = [
        "# Overture Import Report",
        "",
        f"- Release: {report.release}",
        "- Gap-fill boxes:",
    ]
    for box in report.boxes:
        lines.append(
            f"  - {box.label}: lng [{box.xmin}, {box.xmax}], lat [{box.ymin}, {box.ymax}]"
        )
    lines += [
        f"- Category filter: {', '.join(report.category_filter)}",
        f"- Confidence floor: {report.confidence_floor}",
        f"- Input rows: {report.input_row_count}",
        "- Hygiene buckets:",
    ]
    for bucket in overture.OVERTURE_HYGIENE_BUCKETS:
        lines.append(f"  - {bucket}: {report.bucket_counts.get(bucket, 0)}")
    lines += [
        f"- Kept: {report.kept_count}",
        "- Priced rows by region:",
    ]
    if report.priced_row_counts_by_region:
        for region, count in sorted(report.priced_row_counts_by_region.items()):
            lines.append(f"  - {region}: {count}")
    else:
        lines.append("  - (none)")
    lines.append("")
    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "Documented offline transform over the committed raw Overture "
        "extract (data/overture_raw_extract.csv): zero network calls at "
        "any time, no database consumed. Writes the committed station CSV "
        "(unless --dry-run) plus the committed import report. Every "
        "hygiene, price and identity decision belongs to "
        "routing.pipeline.overture, not this command."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--input-path",
            dest="input_path",
            default=str(DEFAULT_INPUT_PATH),
            help="Path to the raw Overture extract. Default: data/overture_raw_extract.csv",
        )
        parser.add_argument(
            "--output-path",
            dest="output_path",
            default=str(DEFAULT_OUTPUT_PATH),
            help="Where to write the station CSV. Default: data/overture_stations.csv",
        )
        parser.add_argument(
            "--report-path",
            dest="report_path",
            default=str(DEFAULT_REPORT_PATH),
            help="Where to write the import report. Default: data/overture-import-report.md",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            default=False,
            help="Print and write the reports without writing the station CSV.",
        )

    def handle(self, *args, **options):
        input_path = Path(options["input_path"])
        output_path = Path(options["output_path"])
        report_path = Path(options["report_path"])
        dry_run = options["dry_run"]

        with open(input_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            station_rows, report = overture.transform(reader)

        # Written unconditionally, whatever the outcome -- this codebase's
        # always-run-to-completion convention (mirrors
        # geocode_stations._write_report / import_stations' dedupe report).
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_report(report), encoding="utf-8")

        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", newline="", encoding="utf-8") as out:
                writer = csv.writer(out)
                writer.writerow(overture.EXPORT_HEADER)
                for station in station_rows:
                    writer.writerow(
                        [
                            station.opis_id,
                            station.name,
                            station.address,
                            station.city,
                            station.state,
                            station.rack_id,
                            _format_decimal(station.retail_price),
                            station.observation_count,
                            _format_decimal(station.price_min),
                            _format_decimal(station.price_max),
                            _format_decimal(station.latitude),
                            _format_decimal(station.longitude),
                            station.geocode_precision,
                            station.geocode_status,
                            station.price_source,
                            station.source,
                            station.gers_id,
                        ]
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"Overture import: release={report.release} "
                f"input_rows={report.input_row_count} kept={report.kept_count} "
                f"dry_run={dry_run}"
            )
        )
        for bucket in overture.OVERTURE_HYGIENE_BUCKETS:
            self.stdout.write(
                self.style.SUCCESS(f"  {bucket}: {report.bucket_counts.get(bucket, 0)}")
            )
        self.stdout.write(self.style.SUCCESS(f"Report written to {report_path}"))
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"--dry-run: station CSV NOT written ({output_path} untouched)"
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Station CSV written to {output_path}"))

"""Thin offline command over the pure `overture.transform()`.

Reads the committed raw Overture extract (`data/overture_raw_extract.csv`)
and the existing station CSV (`data/stations_geocoded.csv`), calls
`routing.pipeline.overture.transform()`, and writes the committed station
CSV plus the two committed reports -- unconditionally, whatever the
outcome. Zero network calls at any time, consumes no database, and never
imports `Station`. Every hygiene, price, dedup and identity decision
belongs to `routing.pipeline.overture` / `routing.pipeline.overture_dedupe`
(plan 22-10/22-11, D-24); this command only reads, delegates, and writes.

Determinism is a hard requirement, from three sources: `overture.transform`
already sorts its output by minted `opis_id` ascending (and its dedup
decisions by `gers_id` before this command writes them); every write below
opens with `newline=""` and lets `csv.writer` apply its fixed dialect
terminator, so the file does not vary by platform; and every `Decimal`
crossing into a CSV goes through `_format_decimal`, one shared helper
quantized to `Station`'s own `decimal_places=8`, so the same numeric value
never renders two different ways across two runs.
"""
import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from routing.pipeline import overture, overture_dedupe, overture_scope

DEFAULT_INPUT_PATH = Path(settings.BASE_DIR) / "data" / "overture_raw_extract.csv"
DEFAULT_EXISTING_PATH = Path(settings.BASE_DIR) / "data" / "stations_geocoded.csv"
DEFAULT_OUTPUT_PATH = Path(settings.BASE_DIR) / "data" / "overture_stations.csv"
DEFAULT_REPORT_PATH = Path(settings.BASE_DIR) / "data" / "overture-import-report.md"
DEFAULT_DECISIONS_PATH = (
    Path(settings.BASE_DIR) / "data" / "overture-dedupe-decisions.csv"
)

# Matches Station's retail_price/price_min/price_max/latitude/longitude,
# all DecimalField(decimal_places=8). Routed through this one helper so a
# Decimal parsed from the raw extract at varying source precision (e.g.
# "-96.797") never reaches the CSV at that ambient precision -- that would
# be a real way byte-identity is lost across two runs of the same input.
_DECIMAL_QUANTIZE = Decimal("0.00000001")


def _format_decimal(value):
    return str(Decimal(value).quantize(_DECIMAL_QUANTIZE))


def _render_source_section(report):
    lines = ["## Source", "", f"- Release: {report.release}", "- Gap-fill boxes:"]
    for box in report.boxes:
        lines.append(
            f"  - {box.label}: lng [{box.xmin}, {box.xmax}], lat [{box.ymin}, {box.ymax}]"
        )
    lines += [
        f"- Category filter: {', '.join(report.category_filter)}",
        f"- Confidence floor: {report.confidence_floor}",
        f"- Licence: {overture_scope.OVERTURE_LICENCE}",
        "",
    ]
    return lines


def _render_hygiene_section(report):
    post_hygiene_total = report.input_row_count - sum(
        report.bucket_counts.get(bucket, 0)
        for bucket in overture.OVERTURE_HYGIENE_BUCKETS
    )
    lines = [
        "## Hygiene exclusions",
        "",
        f"- Input rows: {report.input_row_count}",
        "- Exclusion buckets:",
    ]
    for bucket in overture.OVERTURE_HYGIENE_BUCKETS:
        lines.append(f"  - {bucket}: {report.bucket_counts.get(bucket, 0)}")
    lines += [
        f"- Post-hygiene total: {post_hygiene_total}",
        (
            "- Rows with an unknown (blank/NULL) operating status were "
            f"RETAINED, not excluded: {report.unknown_status_retained_count}"
        ),
        "",
    ]
    return lines


def _render_dedup_section(report):
    lines = [
        "## Dedup",
        "",
        (
            "Match tight on distance only when the existing row is "
            "rooftop-precision -- both sides are then real coordinates. "
            "For city-centroid rows, distance is not a dedup signal at "
            "all; match on normalized brand plus city and state. Never "
            "one shared radius."
        ),
        "",
        f"- Tight-tier matches (rooftop-precision existing rows): {report.tight_tier_match_count}",
        f"- City-tier matches (city-centroid existing rows, brand+city+state): {report.city_tier_match_count}",
        f"- No match (kept as new): {report.no_match_count}",
        f"- Tight-tier threshold used: {report.tight_tier_threshold_mi} mi",
        "- Sensitivity (information only -- never a retune signal, never cited to change the shipped threshold):",
    ]
    for threshold in sorted(report.sensitivity_counts):
        lines.append(f"  - {threshold} mi: {report.sensitivity_counts[threshold]}")
    lines.append("")
    return lines


def _render_spot_check_section(report):
    lines = [
        "## Spot-checked clusters",
        "",
        (
            f"Selection rule: the {overture_scope.SPOT_CHECK_CLUSTER_COUNT} "
            "densest clusters by candidate count inside the gap-fill boxes, plus any "
            "cluster containing a tight-tier decision whose distance falls within 20% "
            f"of the {overture_scope.TIGHT_TIER_THRESHOLD_MI} mi threshold. "
            "Every selected cluster below is reported whether it looks good or bad."
        ),
        "",
    ]
    for index, (cell, decisions) in enumerate(report.spot_check_clusters, start=1):
        sample = decisions[0]
        lines.append(
            f"### Cluster {index}: near {sample.city}, {sample.state} "
            f"(grid cell {cell}, {len(decisions)} candidate(s))"
        )
        lines.append("")
        for decision in sorted(decisions, key=lambda d: d.gers_id):
            lines.append(
                f"- {decision.gers_id} {decision.name!r}: {decision.tier} tier, "
                f"{decision.decision} ({decision.reason})"
            )
        lines.append("")
    if not report.spot_check_clusters:
        lines.append("(no clusters selected -- no post-hygiene candidates)")
        lines.append("")
    return lines


def _render_result_section(report):
    lines = [
        "## Result",
        "",
        f"- Kept: {report.kept_count}",
        "- Priced rows by region:",
    ]
    if report.priced_row_counts_by_region:
        for region, count in sorted(report.priced_row_counts_by_region.items()):
            lines.append(f"  - {region}: {count}")
    else:
        lines.append("  - (none)")
    id_range = overture_scope.OVERTURE_ID_RANGE
    lines += [
        f"- opis_id range used: [{id_range[0]}, {id_range[1]})",
        "",
    ]
    return lines


def _render_forward_risk_section():
    return [
        "## Forward risk",
        "",
        (
            "- The upstream `categories` field this import reads is "
            "deprecated as of the pinned release and scheduled for removal "
            "in the September 2026 release, replaced by `basic_category` "
            "and `taxonomy`. A refresh against any later release must "
            "migrate the field this import reads `categories.primary` "
            "from."
        ),
        "",
    ]


def _render_report(report):
    lines = ["# Overture Import Report", ""]
    lines += _render_source_section(report)
    lines += _render_hygiene_section(report)
    lines += _render_dedup_section(report)
    lines += _render_spot_check_section(report)
    lines += _render_result_section(report)
    lines += _render_forward_risk_section()
    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "Documented offline transform over the committed raw Overture "
        "extract (data/overture_raw_extract.csv) and the existing station "
        "CSV (data/stations_geocoded.csv): zero network calls at any "
        "time, no database consumed. Writes the committed station CSV "
        "(unless --dry-run) plus the committed import report and the "
        "per-decision dedup CSV, unconditionally. Every hygiene, price, "
        "dedup and identity decision belongs to routing.pipeline.overture "
        "/ routing.pipeline.overture_dedupe, not this command."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--input-path",
            dest="input_path",
            default=str(DEFAULT_INPUT_PATH),
            help="Path to the raw Overture extract. Default: data/overture_raw_extract.csv",
        )
        parser.add_argument(
            "--existing-path",
            dest="existing_path",
            default=str(DEFAULT_EXISTING_PATH),
            help="Path to the existing station CSV dedup compares against. Default: data/stations_geocoded.csv",
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
            "--decisions-path",
            dest="decisions_path",
            default=str(DEFAULT_DECISIONS_PATH),
            help="Where to write the per-decision dedup CSV. Default: data/overture-dedupe-decisions.csv",
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
        existing_path = Path(options["existing_path"])
        output_path = Path(options["output_path"])
        report_path = Path(options["report_path"])
        decisions_path = Path(options["decisions_path"])
        dry_run = options["dry_run"]

        with open(existing_path, newline="", encoding="utf-8") as ef:
            existing_reader = csv.DictReader(ef)
            existing_rows = overture_dedupe.load_existing_rows(existing_reader)

        with open(input_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            station_rows, report, decisions = overture.transform(reader, existing_rows)

        # Written unconditionally, whatever the outcome -- this codebase's
        # always-run-to-completion convention (mirrors
        # geocode_stations._write_report / import_stations' dedupe report).
        # Both the human-readable report and the machine-readable
        # per-decision CSV land on every run, dry or not -- that is what
        # makes --dry-run actually useful for reviewing a dedup pass before
        # committing anything.
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_report(report), encoding="utf-8")

        decisions_path.parent.mkdir(parents=True, exist_ok=True)
        with open(decisions_path, "w", newline="", encoding="utf-8") as df:
            writer = csv.writer(df)
            writer.writerow(overture_dedupe.DEDUPE_DECISION_HEADER)
            for decision in sorted(decisions, key=lambda d: d.gers_id):
                writer.writerow(
                    [
                        decision.gers_id,
                        decision.name,
                        decision.brand_token,
                        _format_decimal(decision.latitude),
                        _format_decimal(decision.longitude),
                        decision.city,
                        decision.state,
                        decision.tier,
                        decision.matched_opis_id,
                        decision.matched_name,
                        decision.matched_geocode_precision,
                        decision.distance_mi,
                        decision.decision,
                        decision.reason,
                    ]
                )

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
        self.stdout.write(
            self.style.SUCCESS(
                f"Dedup: tight={report.tight_tier_match_count} "
                f"city={report.city_tier_match_count} no_match={report.no_match_count}"
            )
        )
        self.stdout.write(self.style.SUCCESS(f"Decisions written to {decisions_path}"))
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"--dry-run: station CSV NOT written ({output_path} untouched)"
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Station CSV written to {output_path}"))

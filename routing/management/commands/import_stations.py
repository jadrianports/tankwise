"""Import the raw fuel-price CSV into deduped Station rows.

Idempotent upsert keyed on opis_id: a second run against the same
CSV creates 0, updates 0, and leaves every row unchanged.
"""

import csv
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from routing.models import GeocodeStatus, Station
from routing.pipeline.dedupe import collapse_duplicates

logger = logging.getLogger(__name__)

DEFAULT_CSV_PATH = Path(settings.BASE_DIR) / "fuel-prices-for-be-assessment.csv"
DEDUPE_REPORT_PATH = Path(settings.BASE_DIR) / "data" / "dedupe-report.md"


def _read_valid_rows(csv_path):
    """Read the source CSV, skipping (and logging) rows whose OPIS ID or
    price fields can't be parsed, rather than aborting the whole import
    (one malformed row must not abort the run).

    Returns (rows, skipped_count).
    """
    rows = []
    skipped = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for line_num, row in enumerate(reader, start=2):
            try:
                int(row["OPIS Truckstop ID"])
                Decimal(row["Retail Price"])
            except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
                skipped += 1
                logger.warning(
                    "Skipping malformed row %d: %r (%s)", line_num, row, exc
                )
                continue
            rows.append(row)
    return rows, skipped


def _render_dedupe_report(report, created, updated, unchanged, skipped, out_of_scope_count):
    median_spread = report.median_conflicting_spread
    max_spread = report.max_conflicting_spread
    lines = [
        "# Dedupe Report",
        "",
        f"- Total source rows read: {report.total_rows}",
        f"- Malformed rows skipped: {skipped}",
        f"- Distinct OPIS Truckstop IDs (Station rows persisted): {report.total_groups}",
        f"- Duplicate-ID groups detected: {report.duplicate_group_count}",
        f"  - Exact-duplicate groups (identical price across observations): {report.exact_duplicate_group_count}",
        f"  - Conflicting-price groups (prices differ across observations): {report.conflicting_price_group_count}",
        f"  - Conflicting-group price spread (median: {median_spread}, max: {max_spread})",
        "",
        "## Import Result",
        "",
        f"- Created: {created}",
        f"- Updated: {updated}",
        f"- Unchanged: {unchanged}",
        f"- Out of scope (non-lower-48): {out_of_scope_count}",
        "",
    ]
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Import the fuel-price CSV into Station rows, deduped by OPIS Truckstop ID."

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            type=str,
            nargs="?",
            default=str(DEFAULT_CSV_PATH),
            help="Path to the source fuel-price CSV (default: repo-root fuel-prices-for-be-assessment.csv)",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]

        rows, skipped = _read_valid_rows(csv_path)
        groups, report = collapse_duplicates(rows)

        created = updated = unchanged = 0
        out_of_scope_count = 0

        with transaction.atomic():
            existing_by_opis_id = {s.opis_id: s for s in Station.objects.all()}

            for group in groups:
                if group.out_of_scope:
                    out_of_scope_count += 1

                new_fields = group.mutable_fields()
                # Snapshot the pre-update row (if any) BEFORE calling
                # update_or_create, so location_changed/fields_changed
                # compare against the prior state, not the post-write state.
                existing = existing_by_opis_id.get(group.opis_id)

                if existing is None:
                    defaults = dict(new_fields)
                    defaults["geocode_status"] = (
                        GeocodeStatus.OUT_OF_SCOPE
                        if group.out_of_scope
                        else GeocodeStatus.PENDING
                    )
                    Station.objects.update_or_create(
                        opis_id=group.opis_id, defaults=defaults
                    )
                    created += 1
                    continue

                location_changed = (
                    existing.address != group.address
                    or existing.city != group.city
                    or existing.state != group.state
                )
                fields_changed = any(
                    getattr(existing, field_name) != value
                    for field_name, value in new_fields.items()
                )

                if not fields_changed and not location_changed:
                    unchanged += 1
                    continue

                defaults = dict(new_fields)
                if location_changed:
                    # A real address/city/state change
                    # invalidates any prior geocode result.
                    defaults["latitude"] = None
                    defaults["longitude"] = None
                    defaults["geocode_precision"] = None
                    defaults["geocode_status"] = (
                        GeocodeStatus.OUT_OF_SCOPE
                        if group.out_of_scope
                        else GeocodeStatus.PENDING
                    )

                Station.objects.update_or_create(
                    opis_id=group.opis_id, defaults=defaults
                )
                updated += 1

        DEDUPE_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        report_text = _render_dedupe_report(
            report, created, updated, unchanged, skipped, out_of_scope_count
        )
        DEDUPE_REPORT_PATH.write_text(report_text, encoding="utf-8")

        if skipped:
            self.stdout.write(self.style.WARNING(f"Skipped {skipped} malformed row(s)"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {report.total_groups} distinct stations "
                f"({created} created, {updated} updated, {unchanged} unchanged)"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Duplicate groups: {report.exact_duplicate_group_count} exact-duplicate, "
                f"{report.conflicting_price_group_count} conflicting-price "
                f"(median spread ${report.median_conflicting_spread}, "
                f"max spread ${report.max_conflicting_spread})"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(f"Out of scope (non-lower-48): {out_of_scope_count}")
        )
        self.stdout.write(self.style.SUCCESS(f"Dedupe report written to {DEDUPE_REPORT_PATH}"))

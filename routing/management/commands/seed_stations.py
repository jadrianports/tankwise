"""Replay the committed derived CSV into the Station table.

This is the Docker-facing seed path: it performs NO geocoding and NO
network call -- it is a straight idempotent upsert of already-persisted
values (opis_id, coordinates, precision, status) from
`data/stations_geocoded.csv` (the `geocode_stations` export).

Semantics: idempotent upsert on opis_id, every run -- NOT
skip-if-already-populated, NOT truncate-and-reload. A first run against an
empty DB creates every row; a second run changes nothing; a run against a
drifted table converges it back to the CSV.
"""

import csv
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from routing.models import Station

logger = logging.getLogger(__name__)

DEFAULT_CSV_PATH = Path(settings.BASE_DIR) / "data" / "stations_geocoded.csv"

# Fields copied straight from the derived CSV onto Station, mapped 1:1 by
# column name -- these mirror EXPORT_HEADER in geocode_stations.py minus
# opis_id (the upsert key) and the coordinate/precision columns (handled
# separately below since blank cells must coerce to None).
STRAIGHT_FIELDS = ["name", "address", "city", "state", "rack_id"]
DECIMAL_FIELDS = ["retail_price", "price_min", "price_max"]


def _parse_decimal(value):
    return Decimal(value)


def _parse_optional_decimal(value):
    """Coerce a blank cell (failed/out_of_scope rows have no coordinates)
    to None rather than raising on an empty string."""
    if value is None or value.strip() == "":
        return None
    return Decimal(value)


def _row_to_defaults(row):
    """Map one derived-CSV row to a Station.update_or_create defaults dict.
    Raises on malformed required fields so the caller can log-and-skip
    rather than aborting the whole seed."""
    defaults = {field: row[field] for field in STRAIGHT_FIELDS}
    for field in DECIMAL_FIELDS:
        defaults[field] = _parse_decimal(row[field])
    defaults["observation_count"] = int(row["observation_count"])
    defaults["latitude"] = _parse_optional_decimal(row["latitude"])
    defaults["longitude"] = _parse_optional_decimal(row["longitude"])
    defaults["geocode_precision"] = row["geocode_precision"] or None
    defaults["geocode_status"] = row["geocode_status"]
    return defaults


class Command(BaseCommand):
    help = (
        "Seed the Station table from the committed derived CSV "
        "(data/stations_geocoded.csv) via idempotent upsert on opis_id. "
        "Performs NO geocoding and NO network call -- the Docker replay path."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            type=str,
            nargs="?",
            default=str(DEFAULT_CSV_PATH),
            help="Path to the derived geocoded CSV (default: data/stations_geocoded.csv)",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]

        created = updated = unchanged = 0
        skipped = 0

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            with transaction.atomic():
                # Snapshot existing rows BEFORE upserting so the
                # created/updated/unchanged counts compare against
                # pre-write state, not the post-write values
                # update_or_create just assigned (mirrors import_stations).
                existing_by_opis_id = {s.opis_id: s for s in Station.objects.all()}

                for line_num, row in enumerate(reader, start=2):
                    try:
                        opis_id = int(row["opis_id"])
                        defaults = _row_to_defaults(row)
                    except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
                        skipped += 1
                        logger.warning(
                            "Skipping malformed derived-CSV row %d: %r (%s)",
                            line_num,
                            row,
                            exc,
                        )
                        continue

                    existing = existing_by_opis_id.get(opis_id)

                    if existing is None:
                        Station.objects.update_or_create(opis_id=opis_id, defaults=defaults)
                        created += 1
                        continue

                    changed = any(
                        getattr(existing, field_name) != value
                        for field_name, value in defaults.items()
                    )
                    Station.objects.update_or_create(opis_id=opis_id, defaults=defaults)
                    if changed:
                        updated += 1
                    else:
                        unchanged += 1

        if skipped:
            self.stdout.write(self.style.WARNING(f"Skipped {skipped} malformed row(s)"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded from {csv_path}: {created} created, "
                f"{updated} updated, {unchanged} unchanged"
            )
        )

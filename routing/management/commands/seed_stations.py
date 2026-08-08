"""Replay the committed derived CSV into the Station table.

This is the Docker-facing seed path: it performs NO geocoding and NO
network call -- it is a straight idempotent upsert of already-persisted
values (opis_id, coordinates, precision, status) from
`data/stations_geocoded.csv` (the `geocode_stations` export) and, once
plan 22-05 wires it in, `data/overture_stations.csv` as well.

Semantics: idempotent upsert on opis_id, every run -- NOT
skip-if-already-populated, NOT truncate-and-reload. A first run against an
empty DB creates every row; a second run changes nothing; a run against a
drifted table converges it back to the CSV. The upsert itself is issued in
two batched write calls rather than one write per row, so the query count
stays flat as the CSV grows -- see the write phase below.
"""

import csv
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import Station
from routing.services.corridor import reset_index

logger = logging.getLogger(__name__)

DEFAULT_CSV_PATH = Path(settings.BASE_DIR) / "data" / "stations_geocoded.csv"

# Fields copied straight from the derived CSV onto Station, mapped 1:1 by
# column name -- these mirror EXPORT_HEADER in geocode_stations.py minus
# opis_id (the upsert key) and the coordinate/precision columns (handled
# separately below since blank cells must coerce to None). gers_id is
# NOT here even though it is a straight string column: it is
# blank-tolerant (see _row_to_defaults), and a required-column CommandError
# on a blank-tolerant field would be wrong, so it lives in UPDATE_FIELDS'
# hand-set tail below instead, alongside geocode_precision.
STRAIGHT_FIELDS = ["name", "address", "city", "state", "rack_id", "price_source", "source"]
DECIMAL_FIELDS = ["retail_price", "price_min", "price_max"]

# Every key _row_to_defaults returns, derived from the two field lists above
# plus the columns it sets by hand -- kept as a derivation (not a
# hand-retyped literal) so a future column added to _row_to_defaults can't
# silently drift out of the batched update's field set below.
UPDATE_FIELDS = STRAIGHT_FIELDS + DECIMAL_FIELDS + [
    "observation_count",
    "latitude",
    "longitude",
    "geocode_precision",
    "geocode_status",
    "gers_id",
]


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
    # gers_id is optional and blank for OPIS rows -- a missing/absent
    # column would already have been caught by required_fields below if it
    # were required, so this only needs to coerce a blank cell to None.
    defaults["gers_id"] = (row.get("gers_id") or "").strip() or None
    return defaults


class Command(BaseCommand):
    help = (
        "Seed the Station table from the committed derived CSV "
        "(data/stations_geocoded.csv, and, once plan 22-05 wires it in, "
        "data/overture_stations.csv) via idempotent upsert on opis_id. "
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

            # A CSV missing a required column would otherwise KeyError
            # inside _row_to_defaults' try/except and be silently skipped
            # row-by-row, exiting 0 with an empty (or unchanged) Station
            # table. Fail loudly instead, naming the missing column(s).
            required_fields = set(STRAIGHT_FIELDS) | set(DECIMAL_FIELDS)
            present_fields = set(reader.fieldnames or [])
            missing_fields = required_fields - present_fields
            if missing_fields:
                raise CommandError(
                    f"Derived CSV {csv_path!r} is missing required column(s): "
                    f"{', '.join(sorted(missing_fields))}"
                )

            with transaction.atomic():
                # Snapshot existing rows BEFORE upserting so the
                # created/updated/unchanged counts compare against
                # pre-write state, not the post-write values
                # update_or_create just assigned (mirrors import_stations).
                existing_by_opis_id = {s.opis_id: s for s in Station.objects.all()}

                to_create = []
                to_update = []

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
                        to_create.append(Station(opis_id=opis_id, **defaults))
                        created += 1
                        continue

                    changed = any(
                        getattr(existing, field_name) != value
                        for field_name, value in defaults.items()
                    )
                    if changed:
                        for field_name, value in defaults.items():
                            setattr(existing, field_name, value)
                        to_update.append(existing)
                        updated += 1
                    else:
                        unchanged += 1

                # Rejected the simpler single-pass upsert (passing
                # update_conflicts=True, unique_fields=["opis_id"], and
                # update_fields=UPDATE_FIELDS to the create call below): it
                # writes every row on every idempotent replay, which would
                # destroy the unchanged count above entirely. batch_size=500
                # is a deliberate choice -- SQLite has a bound-parameter
                # limit and CI runs this command against both SQLite and
                # Postgres.
                Station.objects.bulk_create(to_create, batch_size=500)
                Station.objects.bulk_update(to_update, UPDATE_FIELDS, batch_size=500)

        # The corridor STRtree is a process-level cache of Station rows
        # (routing.services.corridor._INDEX); a reseed inside a long-lived
        # process must not serve a stale tree.
        reset_index()

        if skipped:
            self.stdout.write(self.style.WARNING(f"Skipped {skipped} malformed row(s)"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded from {csv_path}: {created} created, "
                f"{updated} updated, {unchanged} unchanged"
            )
        )

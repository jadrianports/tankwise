"""Replay the committed derived CSV(s) into the Station table.

This is the Docker-facing seed path: it performs NO geocoding and NO
network call -- it is a straight idempotent upsert of already-persisted
values (opis_id, coordinates, precision, status) from one or more derived
CSVs -- `data/stations_geocoded.csv` (the `geocode_stations` export) and,
as of plan 22-12, `data/overture_stations.csv` (the Overture gap-fill
import) too.

Semantics: idempotent upsert on opis_id, every run -- NOT
skip-if-already-populated, NOT truncate-and-reload. A first run against an
empty DB creates every row; a second run changes nothing; a run against a
drifted table converges it back to the CSV(s). Accepts a variable number of
paths (zero or more), defaulting to `routing.services.station_csv_paths`'s
`CANONICAL_STATION_CSV_PATHS`, and replays every file inside ONE
transaction, against ONE `existing_by_opis_id` snapshot taken before the
first file is read -- so a row present in two files never sees a
half-written intermediate state, and the write phase issues two batched
calls total for the whole run, not two per file. When the same opis_id
appears in more than one file, the LATER file (by argument order) wins --
see the write phase below for exactly where that resolution happens.
"""

import csv
import logging
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.cache import reset_dataset_vintage_token
from routing.models import Station
from routing.services.corridor import reset_index
from routing.services.station_csv_paths import CANONICAL_STATION_CSV_PATHS

# Checked before adding the import above: routing.cache is imported
# nowhere in this command's own import chain (only routing/tests/test_cache.py
# and routing/views.py import it), so a module-scope import here creates no
# cycle -- unlike _vehicle_token/_dispatch_policy_token's own local imports,
# which defer specifically to keep routing.cache's import order irrelevant
# relative to routing.serializers/routing.services.dp.

logger = logging.getLogger(__name__)

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

REQUIRED_FIELDS = set(STRAIGHT_FIELDS) | set(DECIMAL_FIELDS)


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
    # column would already have been caught by REQUIRED_FIELDS below if it
    # were required, so this only needs to coerce a blank cell to None.
    defaults["gers_id"] = (row.get("gers_id") or "").strip() or None
    return defaults


def _read_csv_rows(csv_path):
    """Open `csv_path`, validate its header carries every REQUIRED_FIELDS
    column -- raising a CommandError naming both the file and the missing
    column(s) otherwise -- and return the parsed rows as plain dicts. Fully
    materialized (not left as a lazy reader) so the file handle closes
    before the caller classifies/writes, keeping each file's read phase
    independent of the others."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        present_fields = set(reader.fieldnames or [])
        missing_fields = REQUIRED_FIELDS - present_fields
        if missing_fields:
            raise CommandError(
                f"Derived CSV {csv_path!r} is missing required column(s): "
                f"{', '.join(sorted(missing_fields))}"
            )
        return list(reader)


class Command(BaseCommand):
    help = (
        "Seed the Station table from one or more committed derived CSVs "
        "(default: routing.services.station_csv_paths."
        "CANONICAL_STATION_CSV_PATHS, currently data/stations_geocoded.csv) "
        "via idempotent upsert on opis_id -- idempotent every run, NOT "
        "skip-if-already-populated, NOT truncate-and-reload. Replays every "
        "file inside one transaction. Performs NO geocoding and NO network "
        "call -- the Docker replay path. When the same opis_id appears in "
        "more than one file, the LATER file (by argument order) wins."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_paths",
            type=str,
            nargs="*",
            default=[str(p) for p in CANONICAL_STATION_CSV_PATHS],
            help=(
                "Path(s) to the derived geocoded CSV(s) (default: the "
                "canonical station CSV list). Replayed in one transaction, "
                "in argument order; a shared opis_id resolves to the last "
                "file's values."
            ),
        )

    def handle(self, *args, **options):
        csv_paths = options["csv_paths"]

        total_skipped = 0
        per_file_reports = []

        with transaction.atomic():
            # ONE snapshot, taken before the first file is read, so every
            # file's created/updated/unchanged classification -- and the
            # final write below -- compares against the same pre-write
            # state, not a state partially mutated by an earlier file in
            # this same run.
            existing_by_opis_id = {s.opis_id: s for s in Station.objects.all()}

            # Final resolved defaults per opis_id across every file, in
            # argument order. A plain dict assignment keyed by opis_id
            # naturally makes a LATER file's row overwrite an EARLIER
            # file's for the same key -- the later-file-wins rule (a
            # silent first-wins would make the canonical ordering
            # meaningless). Per-file counts below report what each file's
            # OWN rows would do against the pre-write snapshot,
            # independent of any other file; the write phase below then
            # applies only the resolved (last-file-wins) values.
            resolved_defaults_by_opis_id = {}

            for csv_path in csv_paths:
                rows = _read_csv_rows(csv_path)

                file_created = file_updated = file_unchanged = file_skipped = 0

                for line_num, row in enumerate(rows, start=2):
                    try:
                        opis_id = int(row["opis_id"])
                        defaults = _row_to_defaults(row)
                    except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
                        file_skipped += 1
                        logger.warning(
                            "Skipping malformed derived-CSV row %d in %s: %r (%s)",
                            line_num,
                            csv_path,
                            row,
                            exc,
                        )
                        continue

                    existing = existing_by_opis_id.get(opis_id)
                    if existing is None:
                        file_created += 1
                    else:
                        changed = any(
                            getattr(existing, field_name) != value
                            for field_name, value in defaults.items()
                        )
                        if changed:
                            file_updated += 1
                        else:
                            file_unchanged += 1

                    resolved_defaults_by_opis_id[opis_id] = defaults

                total_skipped += file_skipped
                per_file_reports.append(
                    (csv_path, file_created, file_updated, file_unchanged, file_skipped)
                )

            # Write phase: classify the FINAL resolved (last-file-wins)
            # state against the same pre-write snapshot, and issue exactly
            # two batched write calls for the whole run -- not two per
            # file.
            to_create = []
            to_update = []
            total_created = total_updated = total_unchanged = 0

            for opis_id, defaults in resolved_defaults_by_opis_id.items():
                existing = existing_by_opis_id.get(opis_id)

                if existing is None:
                    to_create.append(Station(opis_id=opis_id, **defaults))
                    total_created += 1
                    continue

                changed = any(
                    getattr(existing, field_name) != value
                    for field_name, value in defaults.items()
                )
                if changed:
                    for field_name, value in defaults.items():
                        setattr(existing, field_name, value)
                    to_update.append(existing)
                    total_updated += 1
                else:
                    total_unchanged += 1

            # Rejected the simpler single-pass upsert (passing
            # update_conflicts=True, unique_fields=["opis_id"], and
            # update_fields=UPDATE_FIELDS to the create call below): it
            # writes every row on every idempotent replay, which would
            # destroy the unchanged count above entirely. batch_size=500
            # is a deliberate choice -- SQLite has a bound-parameter limit
            # and CI runs this command against both SQLite and Postgres.
            Station.objects.bulk_create(to_create, batch_size=500)
            Station.objects.bulk_update(to_update, UPDATE_FIELDS, batch_size=500)

        # The corridor STRtree is a process-level cache of Station rows
        # (routing.services.corridor._INDEX); a reseed inside a long-lived
        # process must not serve a stale tree. Called exactly once, after
        # every file has been processed -- not once per file.
        reset_index()
        # Same reasoning, one layer over in routing/cache.py: the dataset-
        # vintage token (`_DATASET_VINTAGE_TOKEN`) is a process-level memo
        # of the canonical CSVs' content; a reseed inside a long-lived
        # process must not leave that memo describing the dataset the
        # process started with rather than the one it just replayed.
        reset_dataset_vintage_token()

        if total_skipped:
            self.stdout.write(self.style.WARNING(f"Skipped {total_skipped} malformed row(s)"))

        for csv_path, created, updated, unchanged, skipped in per_file_reports:
            skipped_note = f", {skipped} skipped" if skipped else ""
            self.stdout.write(
                f"  {csv_path}: {created} created, {updated} updated, "
                f"{unchanged} unchanged{skipped_note}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded from {len(csv_paths)} file(s): {total_created} created, "
                f"{total_updated} updated, {total_unchanged} unchanged"
            )
        )

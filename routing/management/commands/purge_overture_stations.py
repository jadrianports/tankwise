"""Delete every Overture-sourced Station row -- the DATA half of a rollback.

Reverting the code that introduced Overture-sourced stations does NOT
remove those rows: `seed_stations` is an idempotent upsert on `opis_id`,
never a wipe-and-reload of the whole table (see its own module docstring),
so a plain `git revert` of the merge leaves a database whose rows carry a
`source` of `overture` and a `gers_id`, plus `retail_price` values priced
at their region's EIA baseline -- a provenance field and a price basis
that the reverted code neither understands nor discloses to a user. This
command is what turns "revert the merge" into a complete rollback: it
deletes exactly the rows the reverted code was never written to serve, and
nothing else.

Dry-run by default, deliberately: this is a destructive command that a
person may run during an incident, and reporting what WOULD be deleted
without deleting anything is the safer default for someone who is already
having a bad day. Pass `--confirm` to actually delete.

Filters on the stored `source` field only -- never on the reserved
Overture id span, never a bare row count, never an unscoped query of the
whole table, and never a full-table wipe. The id span is a derived
property; `source` is a stored fact, and filtering on the derived one is
exactly the fragility the provenance field exists to remove.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from routing.cache import reset_dataset_vintage_token
from routing.models import Station, StationSource
from routing.services.corridor import reset_index


class Command(BaseCommand):
    help = (
        "Delete every Overture-sourced Station row (source=overture). "
        "Dry-run by default -- reports the count that would be deleted and "
        "exits 0 without deleting. Pass --confirm to actually delete. "
        "This is the DATA half of reverting the Overture gap-fill import: "
        "reverting the code alone leaves these rows behind, because "
        "seed_stations is an idempotent upsert, never a full-table "
        "wipe-and-reload."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            default=False,
            help=(
                "Actually delete the Overture-sourced rows. Without this "
                "flag, the command reports what it would delete and exits "
                "0 without deleting anything."
            ),
        )

    def handle(self, *args, **options):
        confirm = options["confirm"]

        total_before = Station.objects.count()
        opis_before = Station.objects.filter(source=StationSource.OPIS).count()
        overture_before = Station.objects.filter(source=StationSource.OVERTURE).count()

        self.stdout.write(
            f"Before: total={total_before} opis={opis_before} "
            f"overture={overture_before}"
        )

        if overture_before == 0:
            self.stdout.write(
                self.style.WARNING(
                    "No Overture-sourced rows found -- either this database "
                    "was already rolled back, or it never had Overture rows "
                    "seeded in the first place."
                )
            )

        if not confirm:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {overture_before} row(s) would be deleted. "
                    "Pass --confirm to actually delete."
                )
            )
            return

        with transaction.atomic():
            deleted_count, _ = Station.objects.filter(
                source=StationSource.OVERTURE
            ).delete()

        # Same reasoning as seed_stations: a write to the Station table
        # inside a long-lived process must not leave the corridor STRtree
        # or the dataset-vintage token memo describing a state that no
        # longer matches the table.
        reset_index()
        reset_dataset_vintage_token()

        total_after = Station.objects.count()
        opis_after = Station.objects.filter(source=StationSource.OPIS).count()
        overture_after = Station.objects.filter(source=StationSource.OVERTURE).count()

        self.stdout.write(
            f"After: total={total_after} opis={opis_after} "
            f"overture={overture_after}"
        )
        self.stdout.write(
            self.style.SUCCESS(f"Deleted {deleted_count} Overture-sourced row(s)")
        )

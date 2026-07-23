"""Fetch the current-week EIA regional diesel factor table now and print
the result.

This is the demo/deploy warm-up path (D-21): running it once after a
fresh deploy or before a demo populates the `current`/`last_known` cache
keys so no visitor pays the first blocking EIA fetch. Performs exactly
one EIA HTTP call (or none, if a cooldown is active) -- never a
per-request call.
"""
from django.core.management.base import BaseCommand

from routing.services import eia


class Command(BaseCommand):
    help = (
        "Fetch the current-week EIA regional diesel factor table now and "
        "print the resolved status, week, and per-region factors. "
        "Frozen-snapshot output is expected/acceptable when EIA_API_KEY "
        "is unset or EIA is unreachable -- this command never errors."
    )

    def handle(self, *args, **options):
        table, status = eia.get_factor_table(force_refresh=True)

        if status == "current":
            self.stdout.write(self.style.SUCCESS(f"Status: {status}"))
        else:
            self.stdout.write(self.style.WARNING(f"Status: {status}"))

        self.stdout.write(f"EIA week: {table['week']}")

        if not table["factors"]:
            self.stdout.write(
                "No regional factors available -- every station prices at "
                "its frozen snapshot value (factor 1.0)."
            )
            return

        for region in sorted(table["factors"]):
            factor = table["factors"][region]
            delta = table["deltas_cents"].get(region)
            self.stdout.write(f"  {region}: factor={factor} delta_cents={delta}")

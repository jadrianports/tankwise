"""Single source of truth for which committed CSVs `seed_stations` replays
into the `Station` table.

`seed_stations`' own `nargs="*"` argument default and `routing/cache.py`'s
dataset-vintage token (`s:`, plan 22-08) both derive from
`CANONICAL_STATION_CSV_PATHS` below rather than each independently listing
the files -- one source of truth is what stops a third CSV, added in a
future phase, desyncing the cache token from what the seed command actually
replays into the table.

Never a wildcard enumeration of every `data/*.csv` file:
`data/gazetteer_places_trimmed.csv` never feeds the station table, and
hashing it into the dataset-vintage token would invalidate the entire route
cache on an edit to a file the solver never reads. Never
`data/overture_raw_extract.csv` either: that file is a build input to the
import pipeline, not a seed source, and including it would invalidate every
cached plan on an extract refresh that happened to produce a byte-identical
station CSV.
"""

from pathlib import Path

from django.conf import settings

DATA_DIR = Path(settings.BASE_DIR) / "data"

# Ordered tuple -- the ordering is part of the contract, because
# `routing/cache.py`'s dataset-vintage token hashes these files in list
# order. `data/overture_stations.csv` joined this tuple in THIS commit,
# alongside the file's own first appearance and the `route:v11:` cache
# bump (the standing same-commit rule, now discharged rather than
# forward-looking).
CANONICAL_STATION_CSV_PATHS = (
    DATA_DIR / "stations_geocoded.csv",
    DATA_DIR / "overture_stations.csv",
)


def reseed_all(stdout=None):
    """Replay every member of `CANONICAL_STATION_CSV_PATHS` through
    `seed_stations` in one invocation. Every reseed in this repository must
    go through this function or rely on `seed_stations`' own canonical
    default -- `SeedStationsCallSiteGateTest`
    (`routing/tests/test_boundaries.py`) enforces that statically, because a
    command that reseeds only the original OPIS file would report a
    pre-import result as post-import, with no error and no log line.

    `call_command` is imported locally, not at module scope, for the same
    reason `routing/cache.py`'s `_dispatch_policy_token` defers its own
    import: keeps this module cheap to import from `routing/cache.py` and
    its import order irrelevant.
    """
    from django.core.management import call_command

    call_command(
        "seed_stations",
        *[str(p) for p in CANONICAL_STATION_CSV_PATHS],
        stdout=stdout,
    )

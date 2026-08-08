#!/bin/sh
set -e

# migrate --noinput runs against DB_MIGRATE_HOST (Neon's direct,
# non-pooled endpoint) when it's set, overriding DB_HOST for this one
# command only -- Neon documents transaction-mode pooling as error-prone
# for schema migrations. Every later step in this script (the seed check
# below, gunicorn itself) keeps using the normal DB_HOST from the
# container's own environment, which is the pooled endpoint in
# production. Locally, DB_MIGRATE_HOST is unset and migrate runs against
# whatever DB_HOST already resolves to (SQLite has no host at all).
if [ -n "${DB_MIGRATE_HOST}" ]; then
  DB_HOST="${DB_MIGRATE_HOST}" python manage.py migrate --noinput
else
  python manage.py migrate --noinput
fi

# seed_stations runs unconditionally, on every boot, not only when the
# station table is empty. The command is an idempotent upsert on opis_id
# (see its own docstring) -- never skip-if-already-populated, never
# truncate-and-reload -- so a boot against an already-converged table costs
# one full-table read plus two batched writes that Django short-circuits to
# no-ops when nothing changed. The previous empty-table guard was a
# cold-start optimization that became a correctness hole the moment the
# committed CSV(s) became something that changes: production's database is
# never empty after the first boot, so under that guard a dataset edit would
# ship, pass every test, bump the cache prefix, and never reach a single
# served request. This step still uses the pooled DB_HOST, not
# DB_MIGRATE_HOST -- it is the seed step, not the migrate step, per the
# distinction the comment above already establishes. A token-comparison
# reseed was rejected because it needs new database state and a write path;
# a row-count guard was rejected because it misses any change that keeps the
# count identical, such as an edited price or a moved coordinate; a one-off
# manual seed was rejected because it is exactly the human bump-on-change
# step this project's provenance requirement forbids.
python manage.py seed_stations

# Worker/timeout/recycling defaults below are measurement-backed, not
# guessed: a fully-warmed worker of this codebase (Django loaded, shapely
# imported, the STRtree built over the routable stations, steady-state
# solves run) measures roughly 70 MB RSS locally. Allowing ~30 MB more for
# gunicorn, psycopg and the Redis client's TLS connection puts a real
# worker near 100 MB, so two workers plus the arbiter sit near 225 MB
# against Render free's 512 MB with wide headroom. The STRtree is built
# lazily per worker (see routing/services/corridor.py), so each worker
# holds its own copy -- --preload would not share it, and worker count
# must be sized as if nothing is shared. max-requests with jitter recycles
# workers against slow memory creep. PORT is injected by the hosting
# platform; the local compose stack falls back to 8000.
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-30}" \
  --max-requests "${GUNICORN_MAX_REQUESTS:-500}" \
  --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-50}"

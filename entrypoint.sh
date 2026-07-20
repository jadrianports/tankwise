#!/bin/sh
set -e

python manage.py migrate --noinput

# --verbosity 0 suppresses the "N objects imported automatically" banner the
# Django shell prints on startup, which would otherwise contaminate the count;
# tail -n1 keeps only the printed number so the -eq test gets a clean integer.
STATION_COUNT=$(python manage.py shell --verbosity 0 -c "from routing.models import Station; print(Station.objects.count())" | tail -n1)

if [ "$STATION_COUNT" -eq "0" ]; then
  echo "Station table empty -- seeding from committed CSV..."
  python manage.py seed_stations
fi

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

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

exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3

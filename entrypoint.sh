#!/bin/sh
set -e

python manage.py migrate --noinput

STATION_COUNT=$(python manage.py shell -c "from routing.models import Station; print(Station.objects.count())")

if [ "$STATION_COUNT" -eq "0" ]; then
  echo "Station table empty -- seeding from committed CSV..."
  python manage.py seed_stations
fi

exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3

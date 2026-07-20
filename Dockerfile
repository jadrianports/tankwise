# Stage 1: build the static Vite SPA bundle.
FROM node:24-alpine AS build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build

# Stage 2: the Django/DRF service. Debian/glibc slim base -- required so
# the shapely wheel installs prebuilt instead of compiling from source.
FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY manage.py .
COPY config/ config/
COPY routing/ routing/
COPY data/ data/
COPY entrypoint.sh .

RUN chmod +x entrypoint.sh

# The frontend is built in this same Dockerfile, in stage 1 above, rather
# than a separate frontend/Dockerfile -- D-07 drops the nginx sidecar
# entirely, so WhiteNoise inside this one gunicorn process now serves what
# nginx used to, leaving only one deployable image. Render's Blueprint
# model also expects exactly one Dockerfile per declared web service.
# WHITENOISE_ROOT (config/settings/base.py) expects the build at this path.
COPY --from=build /app/dist frontend/dist

# Collectstatic runs at build time, not container start, so the image is
# immutable and the work comes off the cold-start path -- this matters on
# a platform that spins the service down when idle (Render free).
RUN python manage.py collectstatic --noinput --settings=config.settings.production

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]

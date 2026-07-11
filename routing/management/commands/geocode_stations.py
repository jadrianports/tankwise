"""Geocode pending Station rows offline in two passes.

Pass 1 submits pending (US) rows to the Census Bulk Geocoder `addressbatch`
endpoint in chunks -- given this dataset's highway-exit
"addresses" it resolves only a handful of rows. Pass 2 joins everything
still unresolved against the committed Gazetteer Places centroid lookup,
delivering the bulk of routable stations.

Every coordinate, from either pass, is validated against the continental-US
bounding box before it is ever persisted -- the sole path a
coordinate enters the DB. Rows that fail both passes get
geocode_status='failed' with null coordinates, excluded from
`StationQuerySet.routable()`. `out_of_scope` rows are never selected by
either pass.

The command always runs to completion, prints/writes a rooftop/city/
failed/out_of_scope breakdown, and exports the committed
derived dataset that Docker/`seed_stations` replays with no
network call.
"""

import csv
import logging
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count

from routing.models import GeocodePrecision, GeocodeStatus, Station
from routing.pipeline import bbox, census_client, gazetteer

logger = logging.getLogger(__name__)

DEFAULT_EXPORT_PATH = Path(settings.BASE_DIR) / "data" / "stations_geocoded.csv"
DEFAULT_REPORT_PATH = Path(settings.BASE_DIR) / "data" / "geocode-report.md"

# Superset of the CSV export's illustrative column list: adds the geocode
# provenance columns so seed_stations reconstructs a table byte-identical to
# the pipelined one, with no NOT-NULL fields left unpopulated.
EXPORT_HEADER = [
    "opis_id",
    "name",
    "address",
    "city",
    "state",
    "rack_id",
    "retail_price",
    "observation_count",
    "price_min",
    "price_max",
    "latitude",
    "longitude",
    "geocode_precision",
    "geocode_status",
]

GEOCODE_UPDATE_FIELDS = ["latitude", "longitude", "geocode_status", "geocode_precision"]


class Command(BaseCommand):
    help = (
        "Geocode pending Station rows offline: Census addressbatch (pass 1) "
        "then Gazetteer city-centroid join (pass 2). Always runs to "
        "completion and reports a rooftop/city/failed/out_of_scope "
        "breakdown."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help=(
                "Also re-attempt rows with geocode_status=failed. "
                "out_of_scope rows are never touched by either path."
            ),
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=census_client.CHUNK_SIZE,
            help="Census addressbatch submission / bulk_update chunk size.",
        )
        parser.add_argument(
            "--export-path",
            type=str,
            default=str(DEFAULT_EXPORT_PATH),
            help="Where to write the derived CSV export. Default: data/stations_geocoded.csv",
        )
        parser.add_argument(
            "--report-path",
            type=str,
            default=str(DEFAULT_REPORT_PATH),
            help="Where to write the geocode quality report. Default: data/geocode-report.md",
        )

    def handle(self, *args, **options):
        retry_failed = options["retry_failed"]
        chunk_size = options["chunk_size"]
        export_path = Path(options["export_path"])
        report_path = Path(options["report_path"])

        statuses = [GeocodeStatus.PENDING]
        if retry_failed:
            statuses.append(GeocodeStatus.FAILED)

        # NEVER select out_of_scope rows -- they are simply not
        # in this filter regardless of --retry-failed.
        working_set = list(Station.objects.filter(geocode_status__in=statuses))

        self.stdout.write(
            f"Selected {len(working_set)} station(s) to geocode "
            f"({'pending + failed (--retry-failed)' if retry_failed else 'pending only'})"
        )

        resolved_ids = self._run_census_pass(working_set, chunk_size)
        remaining = [s for s in working_set if s.opis_id not in resolved_ids]
        unmatched_samples = self._run_gazetteer_pass(remaining, chunk_size)

        self._write_report(report_path, unmatched_samples)
        self._export_csv(export_path)

    def _run_census_pass(self, stations, chunk_size):
        """Pass 1: chunked Census addressbatch submission, persisted
        per-chunk. Any transport error on a chunk is logged and that
        chunk's rows are simply left for the Gazetteer pass / a later
        resumed run -- never a tight retry loop against the free public API.
        """
        resolved_ids = set()

        for start in range(0, len(stations), chunk_size):
            chunk = stations[start : start + chunk_size]
            if not chunk:
                continue

            rows = [(str(s.opis_id), s.address, s.city, s.state, "") for s in chunk]

            try:
                records = census_client.submit_chunk(rows)
            except Exception:
                logger.warning(
                    "Census addressbatch chunk failed (transport error); "
                    "leaving %d row(s) for the Gazetteer pass / a later "
                    "resumed run",
                    len(chunk),
                    exc_info=True,
                )
                continue

            by_id = {s.opis_id: s for s in chunk}
            to_update = []

            for record in records:
                if record.get("match_status") != "Match":
                    # No_Match (or any unrecognized shape, e.g. Tie) falls
                    # through to the Gazetteer pass.
                    continue
                try:
                    opis_id = int(record["id"])
                except (KeyError, ValueError, TypeError):
                    continue

                station = by_id.get(opis_id)
                if station is None:
                    continue

                lat, lng = record.get("latitude"), record.get("longitude")
                if lat is None or lng is None:
                    continue

                if not bbox.is_valid(lat, lng):
                    # The persistence gate: a bad/transposed
                    # coordinate is never written -- the row is left for the
                    # Gazetteer pass instead of being persisted as "ok".
                    logger.warning(
                        "Census match for opis_id=%s rejected by bbox gate "
                        "(lat=%s, lng=%s) -- deferred to Gazetteer pass",
                        opis_id,
                        lat,
                        lng,
                    )
                    continue

                station.latitude = Decimal(str(lat))
                station.longitude = Decimal(str(lng))
                station.geocode_status = GeocodeStatus.OK
                station.geocode_precision = GeocodePrecision.ROOFTOP
                to_update.append(station)
                resolved_ids.add(opis_id)

            if to_update:
                Station.objects.bulk_update(to_update, GEOCODE_UPDATE_FIELDS)

        self.stdout.write(f"Census addressbatch pass: {len(resolved_ids)} rooftop match(es)")
        return resolved_ids

    def _run_gazetteer_pass(self, stations, chunk_size):
        """Pass 2: normalize + alias city match against the
        committed Gazetteer centroid lookup. Every hit is bbox-validated
        before being marked ok/city; a miss or bbox-reject becomes
        failed with null coordinates. Persisted via bulk_update in
        chunks (the chunking pattern is reused here for efficiency, not
        resumability -- this pass is a local file join with nothing to
        resume from).
        """
        unmatched_samples = []
        to_update = []
        unmatched_count = 0

        for station in stations:
            hit = gazetteer.lookup_city(station.city, station.state)
            lat = lng = None
            if hit is not None:
                lat, lng = hit["lat"], hit["lng"]

            if hit is None or not bbox.is_valid(lat, lng):
                if hit is not None:
                    logger.warning(
                        "Gazetteer centroid for opis_id=%s rejected by bbox "
                        "gate (lat=%s, lng=%s)",
                        station.opis_id,
                        lat,
                        lng,
                    )
                else:
                    logger.info(
                        "No Gazetteer match for opis_id=%s (city=%r, state=%s)",
                        station.opis_id,
                        station.city,
                        station.state,
                    )
                station.geocode_status = GeocodeStatus.FAILED
                station.geocode_precision = None
                station.latitude = None
                station.longitude = None
                unmatched_samples.append((station.city, station.state))
                unmatched_count += 1
            else:
                station.latitude = Decimal(str(lat))
                station.longitude = Decimal(str(lng))
                station.geocode_status = GeocodeStatus.OK
                station.geocode_precision = GeocodePrecision.CITY

            to_update.append(station)

        for start in range(0, len(to_update), chunk_size):
            batch = to_update[start : start + chunk_size]
            Station.objects.bulk_update(batch, GEOCODE_UPDATE_FIELDS)

        self.stdout.write(
            f"Gazetteer pass: {len(to_update) - unmatched_count} city match(es), "
            f"{unmatched_count} unmatched"
        )
        return unmatched_samples

    def _write_report(self, report_path, unmatched_samples):
        """Breakdown to stdout AND a written artifact."""
        rooftop_count = Station.objects.filter(
            geocode_status=GeocodeStatus.OK, geocode_precision=GeocodePrecision.ROOFTOP
        ).count()
        city_count = Station.objects.filter(
            geocode_status=GeocodeStatus.OK, geocode_precision=GeocodePrecision.CITY
        ).count()
        failed_count = Station.objects.filter(geocode_status=GeocodeStatus.FAILED).count()
        out_of_scope_count = Station.objects.filter(
            geocode_status=GeocodeStatus.OUT_OF_SCOPE
        ).count()
        pending_count = Station.objects.filter(geocode_status=GeocodeStatus.PENDING).count()
        total = Station.objects.count()

        out_of_scope_by_state = (
            Station.objects.filter(geocode_status=GeocodeStatus.OUT_OF_SCOPE)
            .values("state")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        # Dedupe while preserving order, cap the sample for report readability.
        sample_pairs = list(dict.fromkeys(unmatched_samples))[:20]

        lines = [
            "# Geocode Report",
            "",
            f"- Total stations: {total}",
            f"- Rooftop (Census addressbatch): {rooftop_count}",
            f"- City centroid (Gazetteer): {city_count}",
            f"- Failed: {failed_count}",
            f"- Out of scope (non-lower-48): {out_of_scope_count}",
            f"- Still pending: {pending_count}",
            "",
            "## Out of scope by state",
            "",
        ]
        if out_of_scope_by_state:
            for row in out_of_scope_by_state:
                lines.append(f"- {row['state']}: {row['count']}")
        else:
            lines.append("- (none)")

        lines += ["", "## Sample unmatched (city, state) pairs", ""]
        if sample_pairs:
            for city, state in sample_pairs:
                lines.append(f"- {city}, {state}")
        else:
            lines.append("- (none)")
        lines.append("")

        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines), encoding="utf-8")

        self.stdout.write(
            self.style.SUCCESS(
                f"Breakdown: rooftop={rooftop_count} city={city_count} "
                f"failed={failed_count} out_of_scope={out_of_scope_count} "
                f"pending={pending_count}"
            )
        )
        self.stdout.write(self.style.SUCCESS(f"Geocode report written to {report_path}"))

    def _export_csv(self, export_path):
        """Export the committed derived dataset that seed_stations
        replays with zero network calls."""
        export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(export_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(EXPORT_HEADER)
            for station in Station.objects.order_by("opis_id").iterator():
                writer.writerow(
                    [
                        station.opis_id,
                        station.name,
                        station.address,
                        station.city,
                        station.state,
                        station.rack_id,
                        station.retail_price,
                        station.observation_count,
                        station.price_min,
                        station.price_max,
                        station.latitude if station.latitude is not None else "",
                        station.longitude if station.longitude is not None else "",
                        station.geocode_precision or "",
                        station.geocode_status,
                    ]
                )
        self.stdout.write(self.style.SUCCESS(f"Exported derived dataset to {export_path}"))

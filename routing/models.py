from django.db import models


class GeocodeStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    OK = "ok", "OK"
    FAILED = "failed", "Failed"
    OUT_OF_SCOPE = "out_of_scope", "Out of scope"


class GeocodePrecision(models.TextChoices):
    ROOFTOP = "rooftop", "Rooftop"
    CITY = "city", "City centroid"


class PriceSource(models.TextChoices):
    OPIS_INDEXED = "opis_indexed", "OPIS indexed"
    EIA_REGIONAL_ESTIMATE = "eia_regional_estimate", "EIA regional estimate"


class StationSource(models.TextChoices):
    """The dataset a Station row came from -- deliberately distinct from
    price_source (which is how the row's price was derived), so a licence
    audit can separate the two without inferring either from an id range."""

    OPIS = "opis", "OPIS"
    OVERTURE = "overture", "Overture"


class StationQuerySet(models.QuerySet):
    def routable(self):
        """Stations eligible as routing candidates: geocoded successfully
        with non-null coordinates."""
        return self.filter(
            geocode_status=GeocodeStatus.OK,
            latitude__isnull=False,
            longitude__isnull=False,
        )


class Station(models.Model):
    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    rack_id = models.CharField(max_length=32)
    retail_price = models.DecimalField(max_digits=11, decimal_places=8)
    price_source = models.CharField(
        max_length=32,
        choices=PriceSource.choices,
        default=PriceSource.OPIS_INDEXED,
    )

    geocode_status = models.CharField(
        max_length=16,
        choices=GeocodeStatus.choices,
        default=GeocodeStatus.PENDING,
    )
    # geocode_precision answers "how precise is this coordinate", not "where
    # did this row come from" -- source (below) answers the second question.
    # Overture rows carry ROOFTOP: their coordinates are real points snapped
    # to a lot or building centroid roughly 50-150m out, which is
    # rooftop-class against the 5-mile corridor window. No new
    # GeocodePrecision value may be added: routing/services/corridor.py
    # branches on equality with ROOFTOP at two places with a 20-mile else,
    # so any third value would silently inherit the wider window at both.
    geocode_precision = models.CharField(
        max_length=16,
        choices=GeocodePrecision.choices,
        null=True,
        blank=True,
    )
    latitude = models.DecimalField(
        max_digits=11, decimal_places=8, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=11, decimal_places=8, null=True, blank=True
    )

    # Dedupe provenance, audits the collapse of duplicate OPIS rows
    # into a single Station without retaining raw observations in the DB.
    observation_count = models.PositiveIntegerField(default=1)
    price_min = models.DecimalField(max_digits=11, decimal_places=8)
    price_max = models.DecimalField(max_digits=11, decimal_places=8)

    source = models.CharField(
        max_length=16,
        choices=StationSource.choices,
        default=StationSource.OPIS,
    )
    # Overture's own stable entity identifier (GERS id): a 32-hex-digit
    # UUID plus 4 hyphens. Blank for OPIS rows, persisted so a licence or
    # provenance audit has a direct row-to-upstream-entity trace. Never
    # serialized into any API response -- the same scoping discipline
    # applied to price_source, which is confined to fuel_stops[].
    gers_id = models.CharField(max_length=36, null=True, blank=True)

    objects = StationQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) [{self.opis_id}]"

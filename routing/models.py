from django.db import models


class GeocodeStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    OK = "ok", "OK"
    FAILED = "failed", "Failed"
    OUT_OF_SCOPE = "out_of_scope", "Out of scope"


class GeocodePrecision(models.TextChoices):
    ROOFTOP = "rooftop", "Rooftop"
    CITY = "city", "City centroid"


class StationQuerySet(models.QuerySet):
    def routable(self):
        """Stations eligible as routing candidates: geocoded successfully
        with non-null coordinates (DATA-04)."""
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

    geocode_status = models.CharField(
        max_length=16,
        choices=GeocodeStatus.choices,
        default=GeocodeStatus.PENDING,
    )
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

    # Dedupe provenance (D-10) — audits the collapse of duplicate OPIS rows
    # into a single Station without retaining raw observations in the DB.
    observation_count = models.PositiveIntegerField(default=1)
    price_min = models.DecimalField(max_digits=11, decimal_places=8)
    price_max = models.DecimalField(max_digits=11, decimal_places=8)

    objects = StationQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) [{self.opis_id}]"

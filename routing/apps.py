import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class RoutingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "routing"

    def ready(self):
        # DB-free, import-light -- ready() runs before migrations. Fires
        # exactly once at startup when EIA_API_KEY is unset (D-20): logs a
        # warning and never raises, so the app boots in permanent
        # frozen-snapshot mode instead of failing loud like MAPBOX_TOKEN.
        if not settings.EIA_API_KEY:
            logger.warning(
                "EIA_API_KEY is not set -- running in permanent frozen-snapshot "
                "mode (station prices will not be indexed to current EIA "
                "regional diesel averages). Register a free key at "
                "https://www.eia.gov/opendata/ to enable live indexing."
            )

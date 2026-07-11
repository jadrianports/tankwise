"""Tests for `GET /api/health` -- the Docker Compose liveness probe.

Deliberately no Mapbox token override and no station data: the whole
point of this endpoint is that it never touches DB/cache/Mapbox, so it
must succeed with none of those configured.
"""
from rest_framework import status
from rest_framework.test import APITestCase

HEALTH_URL = "/api/health"


class HealthTests(APITestCase):
    def test_health_returns_200_ok_status(self):
        response = self.client.get(HEALTH_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"status": "ok"})

    def test_health_resolves_under_api_prefix(self):
        """Regression for the ALLOWED_HOSTS/routing bug class: the health
        route must resolve through the same `api/` include as `/api/route`
        (config/urls.py), not a bare top-level path."""
        response = self.client.get("/api/health")

        self.assertNotEqual(response.status_code, status.HTTP_404_NOT_FOUND)

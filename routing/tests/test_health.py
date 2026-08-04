"""Tests for `GET /api/health` -- the Docker Compose liveness probe.

Deliberately no Mapbox token override and no station data: the whole
point of this endpoint is that it never touches DB/cache/Mapbox, so it
must succeed with none of those configured.
"""
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

HEALTH_URL = "/api/health"


class HealthTests(APITestCase):
    def test_health_returns_200_ok_status(self):
        response = self.client.get(HEALTH_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ok")

    @override_settings(BUILD_COMMIT=None)
    def test_health_reports_a_null_commit_when_the_env_var_is_absent(self):
        """Local, Docker Compose and CI never set `RENDER_GIT_COMMIT`. The key
        must still be PRESENT and null rather than missing, so `smoke.yml`'s
        poll can distinguish "this build predates the commit field" from "the
        deploy has not landed yet" -- those need different handling, and a
        missing key conflates them."""
        response = self.client.get(HEALTH_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("commit", response.data)
        self.assertIsNone(response.data["commit"])

    @override_settings(BUILD_COMMIT="0123456789abcdef0123456789abcdef01234567")
    def test_health_reports_the_running_builds_commit_when_render_injects_it(self):
        """The whole point of the field: a post-deploy check must be able to
        tell WHICH build answered, because Render keeps serving the old
        container until the new one is ready and a liveness-only poll cannot
        tell the two apart."""
        response = self.client.get(HEALTH_URL)

        self.assertEqual(
            response.data["commit"], "0123456789abcdef0123456789abcdef01234567"
        )

    @override_settings(BUILD_COMMIT="deadbeef")
    def test_health_stays_dependency_free_with_the_commit_field_present(self):
        """Guards this endpoint's defining property against the new field
        having quietly introduced a dependency: still 200 with no DB rows, no
        cache and no MAPBOX_TOKEN configured."""
        response = self.client.get(HEALTH_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ok")

    def test_health_resolves_under_api_prefix(self):
        """Regression for the ALLOWED_HOSTS/routing bug class: the health
        route must resolve through the same `api/` include as `/api/route`
        (config/urls.py), not a bare top-level path."""
        response = self.client.get("/api/health")

        self.assertNotEqual(response.status_code, status.HTTP_404_NOT_FOUND)

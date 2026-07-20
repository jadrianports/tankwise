"""Tests for the SPA deep-link fallback catch-all (config/urls.py,
routing.views.SpaFallbackView).

Uses `override_settings(WHITENOISE_ROOT=<tmp dir>)` with a sentinel
`index.html` so these tests never depend on a real `frontend/dist` build
being present in the environment.
"""
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

SENTINEL_BODY = "<html><body>spa-fallback-sentinel</body></html>"


def _body(response):
    """Read a response body regardless of whether it's a regular
    HttpResponse or a streaming FileResponse. Fully consuming
    `streaming_content` also triggers Django's `closing_iterator_wrapper`,
    which closes the underlying file handle -- required on Windows, where
    an open handle blocks the TemporaryDirectory's cleanup."""
    if getattr(response, "streaming", False):
        return b"".join(response.streaming_content).decode()
    return response.content.decode()


class SpaFallbackWithBuildTests(TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        index_path = Path(self.tmp_dir.name) / "index.html"
        index_path.write_text(SENTINEL_BODY)
        self.override = override_settings(WHITENOISE_ROOT=Path(self.tmp_dir.name))
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_deep_link_returns_spa_index(self):
        """A client-side route that is not a real file and not under
        /api/ falls through to the catch-all and gets the SPA shell."""
        response = self.client.get("/trip/abc123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(SENTINEL_BODY, _body(response))

    def test_api_health_not_shadowed(self):
        """The catch-all's negative lookahead never intercepts a real
        /api/ route."""
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)

    def test_unknown_api_path_returns_real_404_not_spa_shell(self):
        """An unknown /api/ path stays a genuine Django 404 -- the
        catch-all must never turn it into a 200 SPA shell (T-08-13)."""
        response = self.client.get("/api/does-not-exist")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(SENTINEL_BODY, _body(response))

    def test_static_path_not_swallowed_by_catchall(self):
        """A /static/ path is excluded from the catch-all's regex, so it
        never resolves to the SPA sentinel body."""
        response = self.client.get("/static/nonexistent-asset.css")

        self.assertNotIn(SENTINEL_BODY, _body(response))


class SpaFallbackMissingBuildTests(TestCase):
    def test_missing_index_returns_404_rather_than_raising(self):
        """A fresh clone with no SPA build present must degrade to a
        clear 404, never an unhandled exception."""
        with tempfile.TemporaryDirectory() as empty_dir:
            with override_settings(WHITENOISE_ROOT=Path(empty_dir)):
                response = self.client.get("/trip/abc123")

        self.assertEqual(response.status_code, 404)
        self.assertIn("spa_build_missing", _body(response))

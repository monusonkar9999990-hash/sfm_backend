"""Tests for the routes that serve the built portal.

The portal itself is a Flutter application and is tested in that project. What
is tested here is only this project's half of the deal: that the entry point is
served, that assets beside it are served, that a path the front-end invented
falls back to the entry point rather than 404ing, and that a genuinely missing
file still 404s.

`portal/web/` is a build artefact and is not in the repository, so every test
builds a temporary one and points the views at it.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse


class PortalViewTests(TestCase):
    """Serving a portal that has been built."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        (self.root / 'index.html').write_text(
            '<!DOCTYPE html><base href="/portal/">', encoding='utf-8'
        )
        (self.root / 'main.dart.js').write_text('// app', encoding='utf-8')
        (self.root / 'assets').mkdir()
        (self.root / 'assets' / 'logo.png').write_bytes(b'\x89PNG')

        patcher = patch('portal.views.PORTAL_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

        index_patcher = patch('portal.views.INDEX', self.root / 'index.html')
        index_patcher.start()
        self.addCleanup(index_patcher.stop)

    def _body(self, response):
        return b''.join(response.streaming_content).decode()

    def test_the_entry_point_is_served(self):
        response = self.client.get(reverse('portal:index'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/html')
        self.assertIn('<base href="/portal/">', self._body(response))

    def test_the_entry_point_is_not_cached(self):
        # index.html keeps its name across builds while everything it loads is
        # renamed, so a cached copy asks for files that no longer exist.
        response = self.client.get(reverse('portal:index'))

        self.assertIn('no-store', response['Cache-Control'])

    def test_an_asset_beside_it_is_served(self):
        response = self.client.get('/portal/main.dart.js')

        self.assertEqual(response.status_code, 200)

    def test_an_asset_in_a_subdirectory_is_served(self):
        response = self.client.get('/portal/assets/logo.png')

        self.assertEqual(response.status_code, 200)

    def test_a_route_the_front_end_owns_falls_back_to_the_entry_point(self):
        # Bookmarking a screen, or reloading on one, asks this project for a
        # path only the Flutter router knows. Answering 404 would break both;
        # the app reads the path itself once it has loaded.
        response = self.client.get('/portal/dashboard')

        self.assertEqual(response.status_code, 200)
        self.assertIn('<base href="/portal/">', self._body(response))

    def test_a_nested_front_end_route_also_falls_back(self):
        response = self.client.get('/portal/orders/123')

        self.assertEqual(response.status_code, 200)

    def test_a_missing_file_is_still_missing(self):
        # The fallback must not turn every typo into a page. An asset name that
        # does not exist is a real error — usually a stale build — and saying
        # 200 with HTML would leave the browser parsing markup as JavaScript.
        response = self.client.get('/portal/does-not-exist.js')

        self.assertEqual(response.status_code, 404)

    def test_a_missing_asset_in_a_subdirectory_is_still_missing(self):
        response = self.client.get('/portal/assets/absent.png')

        self.assertEqual(response.status_code, 404)

    def test_the_api_is_not_shadowed(self):
        # The catch-all is mounted under /portal/ only. If it ever widened, the
        # API would start answering with HTML.
        response = self.client.get('/api/v1/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')


class PortalNotBuiltTests(TestCase):
    """Serving a portal that has not been built yet."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        patcher = patch('portal.views.PORTAL_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

        index_patcher = patch('portal.views.INDEX', self.root / 'index.html')
        index_patcher.start()
        self.addCleanup(index_patcher.stop)

    def test_it_says_so_rather_than_serving_nothing(self):
        # A fresh clone has no build. 404 is the honest answer, and the message
        # carries the command that fixes it — the alternative is a blank page
        # and a reader guessing.
        response = self.client.get(reverse('portal:index'))

        self.assertEqual(response.status_code, 404)

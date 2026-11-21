"""Retired supplier routes stay disconnected, including with old keys and stored data."""
from copy import deepcopy
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import api_server as api
from ingestion import Catalog
from location_context import set_location
from provider_comparison import Comparison
from search_cache import SearchCache

RETIRED = ('mealme_satsuma', 'vercel_kroger', 'vercel_square')
LEGACY_ENV = {
    'CONNECTOR_ENABLED': 'true',
    'SATSUMA_API_KEY': 'synthetic-retired-key',
    'SATSUMA_READ_BUDGET': '100',
    'VERCEL_MEALME_API_KEY': 'synthetic-retired-proxy-key',
    'VERCEL_MEALME_READ_BUDGET': '100',
}


class RetiredProviderApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = api.ThreadingHTTPServer(('127.0.0.1', 0), api.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(set_location, None)
        self.catalog = Catalog(self.temp.name + '/db.sqlite')
        self.browser = Mock(side_effect=AssertionError('Retired-provider request reached browser'))
        self.comparison = Comparison(self.catalog, SearchCache(), threading.Lock(), self.browser)
        for patcher in (patch.object(api, 'CATALOG', self.catalog),
                        patch.object(api, 'COMPARISON', self.comparison),
                        patch.object(api, 'search_browser', self.browser),
                        patch.dict(os.environ, LEGACY_ENV)):
            patcher.start()
            self.addCleanup(patcher.stop)
        # HTTP clients below use http.client for loopback requests; this patch
        # catches every urllib supplier read without blocking the local test server.
        upstream = patch('urllib.request.OpenerDirector.open',
                         side_effect=AssertionError('Unexpected supplier request'))
        self.upstream = upstream.start()
        self.addCleanup(upstream.stop)
        api.RATE.clear()
        api.RESULTS.clear()
        self.addCleanup(api.RESULTS.clear)

    def request(self, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               body=json.dumps(body) if body is not None else None,
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def seed(self, provider):
        payload = {
            'provider': provider,
            'items': [{'id': provider + '-milk', 'provider': provider, 'name': 'Milk',
                       'price': 3.99, 'retailer': 'aldi', 'query': 'milk',
                       'provider_location_id': provider + '-store'}],
            'location': {'requested_zip': '63129', 'observed_zip': '63129',
                         'verified': True, 'context_id': 'zip:63129'},
        }
        self.catalog.ingest('63129', 'milk', payload, 300)
        cache = SearchCache().attach(self.catalog, provider)
        cache.put(cache.key('63129', ['milk'], 'aldi', True), deepcopy(payload))

    def test_removed_comparison_routes_return_gone_without_supplier_reads(self):
        for provider in RETIRED:
            for providers in ([provider], ['instacart', provider]):
                with self.subTest(providers=providers):
                    code, body = self.request('/api/v1/compare', {
                        'zip': '63129', 'queries': ['milk'], 'mode': 'live',
                        'providers': providers,
                    })
                    self.assertEqual(code, 410)
                    self.assertEqual(body['error'], 'provider_removed')
        self.upstream.assert_not_called()
        self.browser.assert_not_called()

    def test_removed_catalog_and_export_filters_return_gone(self):
        for provider in RETIRED:
            self.seed(provider)
            for route in ('/api/v1/catalog', '/api/v1/catalog/export'):
                with self.subTest(provider=provider, route=route):
                    code, body = self.request(route + '?provider=' + provider + '&zip=63129')
                    self.assertEqual(code, 410)
                    self.assertEqual(body['error'], 'provider_removed')
        self.upstream.assert_not_called()
        self.browser.assert_not_called()

    def test_all_catalog_and_export_exclude_retired_stored_offers(self):
        self.seed('instacart')
        for provider in RETIRED:
            self.seed(provider)
        for route in ('/api/v1/catalog', '/api/v1/catalog/export'):
            with self.subTest(route=route):
                code, body = self.request(route + '?provider=all&zip=63129&query=milk')
                self.assertEqual(code, 200)
                self.assertEqual([item['id'] for item in body['items']], ['instacart-milk'])
                self.assertEqual(body['catalog']['products'], 1)
        # Retirement filters public reads without deleting the evidence archive.
        with self.catalog.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM offers').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM search_snapshots').fetchone()[0], 4)
        self.upstream.assert_not_called()
        self.browser.assert_not_called()

    def test_default_comparison_cannot_rehydrate_retired_cache(self):
        for provider in RETIRED:
            self.seed(provider)
        code, body = self.request('/api/v1/compare', {
            'zip': '63129', 'queries': ['milk'], 'mode': 'cache_only',
        })
        self.assertEqual(code, 200)
        self.assertEqual([result['provider'] for result in body['providers']], ['instacart'])
        self.assertEqual(body['providers'][0]['items'], [])
        self.assertEqual(body['providers'][0]['status'], 'not_cached')
        self.upstream.assert_not_called()
        self.browser.assert_not_called()

    def test_status_advertises_only_active_capabilities_and_no_legacy_access_state(self):
        code, body = self.request('/api/v1/providers/status')
        self.assertEqual(code, 200)
        self.assertFalse(set(RETIRED).intersection(body['capabilities']))
        for field in ('satsuma_configured', 'satsuma_base_url', 'satsuma_read_budget',
                      'satsuma_background_ingestion', 'read_attempts_in_database', 'vercel_proxy'):
            self.assertNotIn(field, body)
        serialized = json.dumps(body)
        self.assertNotIn(LEGACY_ENV['SATSUMA_API_KEY'], serialized)
        self.assertNotIn(LEGACY_ENV['VERCEL_MEALME_API_KEY'], serialized)
        self.upstream.assert_not_called()
        self.browser.assert_not_called()

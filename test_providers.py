"""Active browser comparison behavior and retired-provider rejection."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from connector import ConnectorError
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


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = Catalog(self.temp.name + '/db.sqlite')
        set_location({'postal_code': '63129', 'country_code': 'US'})
        self.payload = {
            'items': [{'id': 'browser-milk', 'name': 'Milk', 'price': 3.2,
                       'retailer': 'aldi', 'query': 'milk',
                       'url': 'https://www.instacart.com/products/1'}],
            'location': {'requested_zip': '63129', 'observed_zip': '63129',
                         'verified': True, 'context_id': 'zip:63129'},
            'warnings': [],
        }
        self.browser = Mock(return_value=self.payload)
        self.comparison = Comparison(self.catalog, SearchCache(), threading.Lock(), self.browser)

    def tearDown(self):
        set_location(None)
        self.temp.cleanup()

    def test_browser_comparison_reuses_cache_and_retains_offer_identity(self):
        with patch.dict(os.environ, {'CONNECTOR_ENABLED': 'true'}):
            first = self.comparison.compare('63129', ['milk'], '', True, 'live', ['instacart'])
            second = self.comparison.compare('63129', ['milk'], '', True, 'live', ['instacart'])
        self.browser.assert_called_once()
        self.assertEqual(first['providers'][0]['status'], 'ok')
        self.assertTrue(second['providers'][0]['cache_hit'])
        item = second['providers'][0]['items'][0]
        self.assertEqual((item['id'], item['provider']), ('browser-milk', 'instacart'))
        self.assertFalse(item['selectable'])
        self.assertEqual(len(self.catalog.search('milk', provider='instacart')), 1)

    def test_failed_browser_read_is_not_cached_as_success(self):
        self.browser.side_effect = ConnectorError('provider_timeout', 'Synthetic timeout')
        with patch.dict(os.environ, {'CONNECTOR_ENABLED': 'true'}):
            result = self.comparison.one('instacart', '63129', ['milk'], '', True, 'live')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['code'], 'provider_timeout')
        self.assertEqual(result['items'], [])
        self.assertEqual(self.catalog.search(provider='instacart'), [])
        self.assertFalse(self.comparison.icache.entries)

    def test_catalog_comparison_reads_stored_rows_without_live_lookup(self):
        empty = self.comparison.one('instacart', '63129', ['milk'], '', True, 'catalog')
        self.assertEqual(empty['status'], 'not_cached')
        self.catalog.ingest('63129', 'milk', self.payload, 300)
        cached = self.comparison.one('instacart', '63129', ['milk'], '', True, 'catalog')
        self.assertEqual(cached['status'], 'cached')
        self.assertEqual(cached['items'][0]['id'], 'browser-milk')
        self.browser.assert_not_called()

    def test_retired_provider_rejected_with_legacy_credentials_present(self):
        with patch.dict(os.environ, LEGACY_ENV), patch('urllib.request.OpenerDirector.open') as upstream:
            for provider in RETIRED:
                with self.subTest(provider=provider), self.assertRaises(ConnectorError) as error:
                    self.comparison.one(provider, '63129', ['milk'], '', True, 'live')
                self.assertEqual(error.exception.status, 'provider_removed')
        upstream.assert_not_called()
        self.browser.assert_not_called()

    def test_mixed_comparison_rejected_before_any_active_provider_is_scheduled(self):
        with patch.dict(os.environ, LEGACY_ENV), patch('urllib.request.OpenerDirector.open') as upstream:
            for provider in RETIRED:
                with self.subTest(provider=provider), self.assertRaises(ConnectorError) as error:
                    self.comparison.compare('63129', ['milk'], '', True, 'live', ['instacart', provider])
                self.assertEqual(error.exception.status, 'provider_removed')
        upstream.assert_not_called()
        self.browser.assert_not_called()

    def test_old_catalog_migrates_without_losing_active_records(self):
        path = self.temp.name + '/old.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE products(key TEXT PRIMARY KEY,zip TEXT,retailer TEXT,terms TEXT,seen REAL,expires REAL,payload TEXT)')
            db.execute('INSERT INTO products VALUES(?,?,?,?,?,?,?)',
                       ('old', '63129', 'aldi', '["milk"]', 0, 1,
                        json.dumps({'id': 'old', 'name': 'Milk', 'price': 2})))
        row = Catalog(path).search('milk')[0]
        self.assertEqual((row['provider'], row['id']), ('instacart', 'old'))

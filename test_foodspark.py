"""Provider routing compatibility and provenance; all supplier responses are mocked."""
from copy import deepcopy
import io
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import foodspark
from connector import ConnectorError
from ingestion import Catalog
from location_context import set_location
from search_cache import SearchCache
from search_service import SearchService, merge_parts


class FoodSparkRoutingTests(unittest.TestCase):
    def setUp(self):
        self.location = {
            'postal_code': '63129', 'country_code': 'US',
            'address': {'line1': '100 Test Street', 'city': 'Test City', 'region': 'MO'},
        }
        set_location(deepcopy(self.location))
        self.raw = {
            'request_log': {'status': '200'},
            'response': {
                'store_details': {
                    'Delivery Zip Code': '63129', 'Store Slug': 'aldi',
                    'Delivery Address': '100 Test Street', 'Store Location ID': 'store-one',
                },
                'results': [{'Product Name': 'Milk', 'Unit Size': '1 gal',
                             'Price': '$3.99', 'Availability': 'In Stock'}],
            },
        }
        self.browser_payload = {
            'items': [{'id': 'existing-browser-product', 'name': 'Milk', 'price': 3.99,
                       'retailer': 'aldi', 'query': 'milk'}],
            'location': {'requested_zip': '63129', 'observed_zip': '63129',
                         'verified': True, 'context_id': 'zip:63129'},
        }
        self.browser = Mock(side_effect=lambda *args: deepcopy(self.browser_payload))

    def tearDown(self):
        set_location(None)

    def response(self, *args, **kwargs):
        return io.BytesIO(json.dumps(self.raw).encode())

    def test_configured_address_and_retailer_prefer_xbyte_with_explicit_provenance(self):
        with patch.dict(os.environ, {'FOODSPARK_API_KEY': 'synthetic-xbyte-key'}), \
                patch.object(foodspark, 'build_opener') as factory:
            factory.return_value.open.side_effect = self.response
            result = foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
        self.browser.assert_not_called()
        request = factory.return_value.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).netloc, 'foodsparkfastapiframework.xbyte.io')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)['brand_slug'], ['aldi'])
        self.assertEqual(result['source_provider'], 'xbyte_foodspark')
        self.assertEqual(result['items'][0]['source_provider'], 'xbyte_foodspark')
        self.assertEqual(result['items'][0]['source'], 'foodspark')
        self.assertEqual(result['items'][0]['price'], 3.99)

    def test_ineligible_requests_keep_browser_compatibility_and_original_ids(self):
        cases = (
            ('missing key', '', 'aldi', self.location),
            ('missing retailer', 'synthetic-xbyte-key', '', self.location),
            ('ZIP only', 'synthetic-xbyte-key', 'aldi', {'postal_code': '63129', 'country_code': 'US'}),
            ('coordinates only', 'synthetic-xbyte-key', 'aldi', {
                'postal_code': '63129', 'country_code': 'US',
                'coordinates': {'latitude': 38.4, 'longitude': -90.3},
            }),
        )
        for label, key, retailer, location in cases:
            with self.subTest(case=label), patch.dict(os.environ, {'FOODSPARK_API_KEY': key}), \
                    patch.object(foodspark, 'build_opener') as factory:
                set_location(deepcopy(location))
                self.browser.reset_mock()
                result = foodspark.search_live('63129', ['milk'], retailer, True, self.browser)
                self.browser.assert_called_once_with('63129', ['milk'], retailer, True)
                factory.assert_not_called()
                self.assertEqual(result['source_provider'], 'instacart_browser')
                self.assertEqual(result['items'][0]['source_provider'], 'instacart_browser')
                self.assertEqual(result['items'][0]['id'], 'existing-browser-product')

    def test_xbyte_quota_or_timeout_never_silently_switches_to_browser(self):
        failures = (
            (HTTPError('https://example.invalid/search', 429, 'Synthetic quota', None, None), 'provider_quota'),
            (TimeoutError('Synthetic timeout'), 'provider_timeout'),
        )
        for failure, expected in failures:
            with self.subTest(expected=expected), patch.dict(os.environ, {'FOODSPARK_API_KEY': 'synthetic-xbyte-key'}), \
                    patch.object(foodspark, 'build_opener') as factory:
                factory.return_value.open.side_effect = failure
                with self.assertRaises(ConnectorError) as error:
                    foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
                self.assertEqual(error.exception.status, expected)
        self.browser.assert_not_called()

    def test_location_mismatch_never_falls_back_or_returns_an_offer(self):
        self.raw['response']['store_details']['Delivery Zip Code'] = '10001'
        with patch.dict(os.environ, {'FOODSPARK_API_KEY': 'synthetic-xbyte-key'}), \
                patch.object(foodspark, 'build_opener') as factory:
            factory.return_value.open.side_effect = self.response
            with self.assertRaises(ConnectorError) as error:
                foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
        self.assertEqual(error.exception.status, 'location_unsupported')
        self.assertEqual(error.exception.message, 'FoodSpark did not confirm the requested ZIP.')
        self.browser.assert_not_called()

    def test_full_address_acknowledgment_accepts_case_and_spacing_only(self):
        self.raw['response']['store_details']['Delivery Address'] = ' 100 TEST STREET,   TEST CITY, MO 63129 '
        with patch.dict(os.environ, {'FOODSPARK_API_KEY': 'synthetic-xbyte-key'}), \
                patch.object(foodspark, 'build_opener') as factory:
            factory.return_value.open.side_effect = self.response
            result = foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
        self.assertTrue(result['location']['verified'])
        self.assertEqual(result['items'][0]['source_provider'], 'xbyte_foodspark')
        self.browser.assert_not_called()

    def test_unit_must_be_acknowledged_in_street_or_full_address(self):
        self.location['address']['line2'] = 'Unit 2'
        set_location(deepcopy(self.location))
        accepted = ('100 Test Street, Unit 2', '100 Test Street, Unit 2, Test City, MO 63129')
        rejected = ('100 Test Street', '100 Test Street, Unit 3',
                    '100 Test Street, Unit 2, Other City, MO 63129',
                    '100 Test Street, Unit 2, Test City, MO 10001',
                    '101 Test Street, Unit 2, Test City, MO 63129')
        with patch.dict(os.environ, {'FOODSPARK_API_KEY': 'synthetic-xbyte-key'}), \
                patch.object(foodspark, 'build_opener') as factory:
            factory.return_value.open.side_effect = self.response
            for echoed_address in accepted:
                with self.subTest(address=echoed_address):
                    self.raw['response']['store_details']['Delivery Address'] = echoed_address
                    result = foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
                    self.assertTrue(result['location']['verified'])
            for echoed_address in rejected:
                with self.subTest(address=echoed_address):
                    self.raw['response']['store_details']['Delivery Address'] = echoed_address
                    with self.assertRaises(ConnectorError) as error:
                        foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
                    self.assertEqual(error.exception.status, 'location_unsupported')
                    self.assertEqual(error.exception.message, 'FoodSpark did not confirm the requested street address.')
                    self.assertNotIn(echoed_address, error.exception.message)
        self.browser.assert_not_called()

    def test_xbyte_identity_is_stable_and_remains_scoped_to_returned_store(self):
        with patch.dict(os.environ, {'FOODSPARK_API_KEY': 'synthetic-xbyte-key'}), \
                patch.object(foodspark, 'build_opener') as factory:
            factory.return_value.open.side_effect = self.response
            first = foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
            second = foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
            self.raw['response']['store_details']['Store Location ID'] = 'store-two'
            other_store = foodspark.search_live('63129', ['milk'], 'aldi', False, self.browser)
        self.assertEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertNotEqual(first['items'][0]['id'], other_store['items'][0]['id'])

    def test_source_and_ids_survive_persistent_cache_without_changing_namespace(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {'FOODSPARK_API_KEY': 'synthetic-xbyte-key'}), \
                patch.object(foodspark, 'build_opener') as factory:
            factory.return_value.open.side_effect = self.response
            catalog = Catalog(folder + '/db.sqlite')
            cache = SearchCache().attach(catalog, 'instacart')
            fetch = lambda *args: foodspark.search_live(*args, self.browser)
            first = SearchService(catalog, cache, threading.Lock(), fetch).resolve(
                '63129', ['milk'], 'aldi', False, 'refresh')
            first_payload, _ = merge_parts(*first[:3])
            restarted_cache = SearchCache().attach(catalog, 'instacart')
            forbidden_fetch = Mock(side_effect=AssertionError('Cache-only lookup reached provider'))
            cached = SearchService(catalog, restarted_cache, threading.Lock(), forbidden_fetch).resolve(
                '63129', ['milk'], 'aldi', False, 'cache_only')
            cached_payload, metadata = merge_parts(*cached[:3])
            self.assertEqual(cached_payload['source_providers'], ['xbyte_foodspark'])
            self.assertEqual(cached_payload['items'][0]['source_provider'], 'xbyte_foodspark')
            self.assertEqual(cached_payload['items'][0]['id'], first_payload['items'][0]['id'])
            self.assertEqual(metadata['sources'], ['persistent_cache'])
            self.assertEqual(catalog.search('milk', provider='instacart')[0]['source_provider'], 'xbyte_foodspark')
            with catalog.db() as db:
                self.assertEqual(db.execute('SELECT DISTINCT namespace FROM search_snapshots').fetchall(), [('instacart',)])
            forbidden_fetch.assert_not_called()
            factory.return_value.open.assert_called_once()
            self.browser.assert_not_called()

    def test_legacy_foodspark_snapshot_reports_source_without_rewriting_item_id(self):
        legacy = deepcopy(self.browser_payload)
        legacy['items'][0]['source'] = 'foodspark'
        payload, _ = merge_parts(
            [(legacy, {'age_seconds': 5, 'expires_in': 295, 'source': 'persistent_cache'})],
            ['milk'], [],
        )
        self.assertEqual(payload['source_providers'], ['xbyte_foodspark'])
        self.assertEqual(payload['items'][0]['id'], 'existing-browser-product')

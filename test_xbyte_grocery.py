"""XBYTE-01 fixture tests; no live vendor calls.

Every fixture and 63131 offer below is synthetic. None establishes current
vendor coverage, product identity, source freshness, or real inventory.
The existing 63129 browser/basket regression remains in test_api/test_local_offers.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import foodspark as f
from connector import ConnectorError
from location_context import set_location
from search_cache import SearchCache
from ingestion import Catalog
from search_service import SearchService
from work_coordinator import PriorityLock
from provider_comparison import Comparison
import api_server as api
import test_api

FIXTURES = Path(__file__).parent / 'fixtures' / 'xbyte'
LOCATION = {'postal_code': '63131', 'country_code': 'US',
            'address': {'line1': '100 Fixture Avenue', 'city': 'Fixture City', 'region': 'MO'}}
ENV = {'FOODSPARK_ENABLED': 'true', 'FOODSPARK_API_KEY': 'fixture-grocery-secret',
       'FOODSPARK_RESTAURANTS_ENABLED': 'true', 'CONNECTOR_ENABLED': 'true'}


def sample():
    now = datetime.now(timezone.utc)
    return {'request_log': {'status': 200, 'request_time': '2026-11-06 12:10:17'},
            'response': {'store_details': {'Store Name': 'Fixture Market', 'Store Slug': 'target-corp',
                                         'Store Location ID': 'fixture-branch', 'Delivery Zip Code': '63131',
                                         'Delivery Address': '100 Fixture Avenue'},
                         'results': [{'Product Name': 'Fixture Brand Whole Milk', 'Brand': 'Fixture Brand',
                                      'Product ID': '10001', 'UPC': '012345678905',
                                      'Product URL': 'https://www.instacart.com/products/10001-fixture-milk?retailerSlug=target-corp',
                                      'Unit Size': '1 gal', 'Pack Count': 1, 'Price': '$3.25 each',
                                      'Price Per Unit': '$0.025390625/fl oz', 'Availability': 'inStock',
                                      'Captured At': (now-timedelta(minutes=1)).isoformat(),
                                      'Valid Until': (now+timedelta(minutes=10)).isoformat()}]}}


def normalized(data=None):
    data = data or sample()
    return f.normalize(data['response']['results'][0], data['response']['store_details'],
                       'milk', 'target-corp', '63131')


class GroceryAdapterTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        set_location(deepcopy(LOCATION))
        self.addCleanup(set_location, {'postal_code': '63129', 'country_code': 'US'})

    def test_disabled_missing_key_and_zip_only_preserve_browser(self):
        browser = Mock(return_value={'items': []})
        with patch.object(f.transport, 'request_json') as vendor:
            with patch.dict(os.environ, {'FOODSPARK_ENABLED': 'false'}):
                f.search_live('63131', ['milk'], 'target-corp', False, browser)
            with patch.dict(os.environ, {'FOODSPARK_API_KEY': ''}):
                f.search_live('63131', ['milk'], 'target-corp', False, browser)
            set_location({'postal_code': '63131', 'country_code': 'US'})
            f.search_live('63131', ['milk'], 'target-corp', False, browser)
        self.assertEqual(browser.call_count, 3)
        vendor.assert_not_called()

    def test_synthetic_search_preserves_brand_pack_unit_and_unknown_identity(self):
        data = json.loads((FIXTURES/'grocery_synthetic_search.json').read_text())
        item = f.normalize(data['response']['results'][0], data['response']['store_details'],
                           'chips', 'target-corp', '63131')
        self.assertEqual((item['price'], item['pack_count'], item['unit_price']['amount']), (12.50, 18, .69))
        self.assertEqual(item['brand'], 'Synthetic Snacks')
        self.assertEqual(item['provider_location_id'], 'fixture-market')
        self.assertEqual(item['price_source'], 'instacart_marketplace')
        self.assertIsNone(item['provider_product_id'])
        self.assertIsNone(item['upc'])
        self.assertIsNone(item['url'])
        self.assertIsNone(item['captured_at'])
        self.assertEqual(item['source_freshness'], 'unknown')
        self.assertFalse(item['price_fresh'])
        self.assertFalse(item['selectable'])
        self.assertFalse(item['availability_verified'])

    def test_request_uses_exact_grocery_contract(self):
        with patch.object(f.transport, 'request_json', return_value=sample()) as request:
            result = f.search_live('63131', ['milk'], 'target-corp', False, Mock())
        path, params = request.call_args.args
        self.assertEqual(path, '/search')
        self.assertEqual(params['endpoint_type'], 'dl295_search_by_addr_brand_slug_and_keyword')
        self.assertEqual(params['address'], '100 Fixture Avenue, Fixture City, MO 63131')
        self.assertEqual(params['brand_slug'], 'target-corp')
        self.assertEqual(params['search_term'], 'milk')
        self.assertNotIn('zipcode', params)
        self.assertNotIn('api_key', params)
        item = result['items'][0]
        self.assertEqual((item['provider_product_id'], item['upc'], item['pack_count']), ('10001', '012345678905', 1))
        self.assertTrue(item['selectable'])
        self.assertFalse(result['location']['store_branch_verified'])

    def test_unrelated_recommendations_are_not_matches(self):
        with patch.object(f.transport, 'request_json', return_value=sample()):
            with self.assertRaises(ConnectorError) as caught:
                f.search_live('63131', ['nonsensexyz'], 'target-corp', False, Mock())
        self.assertEqual(caught.exception.status, 'no_match')

    def test_empty_result_and_malformed_rows_are_distinct(self):
        for response, expected in [({'results': []}, 'no_match'), ({'results': [{'unexpected': 'shape'}]}, 'invalid_response')]:
            with patch.object(f.transport, 'request_json', return_value={'response': response}):
                with self.assertRaises(ConnectorError) as caught:
                    f.search_live('63131', ['milk'], 'target-corp', False, Mock())
            self.assertEqual(caught.exception.status, expected)

    def test_store_location_mismatch_is_distinct(self):
        data = sample()
        data['response']['store_details']['Delivery Zip Code'] = '63129'
        with patch.object(f.transport, 'request_json', return_value=data):
            with self.assertRaises(ConnectorError) as caught:
                f.search_live('63131', ['milk'], 'target-corp', False, Mock())
        self.assertEqual(caught.exception.status, 'location_unsupported')

    def test_missing_and_huge_prices_never_become_zero_or_infinity(self):
        data = sample()
        for value in (None, '', 'call for price', '$'+'9'*400+' each'):
            data['response']['results'][0]['Price'] = value
            item = normalized(data)
            self.assertIsNone(item['price'])
            self.assertFalse(item['selectable'])
        self.assertIsNone(f._unit_price('$'+'9'*400+'/oz'))
        self.assertEqual(f._money('$0.00 each')[0], 0)

    def test_starting_and_estimated_prices_are_explicit(self):
        self.assertEqual(f._money('Starting at $4.50'), (4.5, 'starting_at', 'unspecified'))
        self.assertEqual(f._money('$22.00 /pkg (est.)'), (22.0, 'estimated', 'unspecified'))
        self.assertEqual(f._money('$4.19/lb'), (4.19, 'exact', 'per_lb'))

    def test_unknown_or_expired_capture_cannot_be_renewed_by_retrieval(self):
        item = normalized()
        item['captured_at'] = None
        item['price_expires_in'] = 300
        f.refresh_evidence(item)
        self.assertFalse(item['price_fresh'])
        self.assertEqual(item['price_expires_in'], 0)
        self.assertEqual(item['cache_expires_in'], 300)
        stale = normalized()
        future = time.time()+1000
        f.refresh_evidence(stale, future)
        self.assertEqual(stale['source_freshness'], 'stale')
        self.assertFalse(stale['selectable'])
        self.assertIsNone(f._timestamp('2026-11-06 12:10:17'))

    def test_url_validation_does_not_expose_tokens_or_wrong_merchants(self):
        base = 'https://www.instacart.com/products/10001-milk'
        for url in (base+'#api_key=fixture', base+'?token=fixture', base+'?retailerSlug=other',
                    'https://user:fixture@www.instacart.com/products/10001-milk', 'https://evil.test/products/10001-milk'):
            self.assertIsNone(f.product_url(url, 'target-corp'))
        self.assertIsNone(f._image_url('https://user:fixture@cdn.instacart.com/image'))
        self.assertIsNone(f._image_url('https://cdn.instacart.com/image?token=fixture'))

    def test_missing_detail_link_never_calls_provider(self):
        item = normalized()
        item['url'] = None
        with patch.object(f.transport, 'request_json') as request:
            with self.assertRaises(ConnectorError) as caught:
                f.details(item, LOCATION)
        self.assertEqual(caught.exception.status, 'product_link_unavailable')
        request.assert_not_called()

    def test_synthetic_detail_remains_estimated_and_location_unverified(self):
        data = json.loads((FIXTURES/'grocery_synthetic_detail.json').read_text())
        row = data['response']['results'][0]['Result']
        item = {'data_provider': 'xbyte_foodspark', 'url': row['Product URL'], 'query': 'chicken',
                'retailer': 'fixture-wholesale', 'id': 'original-observation', 'provider_location_id': 'original-branch'}
        location = {'postal_code': '63131', 'address': {'line1': '100 Fixture Avenue', 'city': 'Fixture City', 'region': 'MO'}}
        with patch.object(f.transport, 'request_json', return_value=data) as request:
            value = f.details(item, location)
        self.assertEqual(request.call_args.args[1]['endpoint_type'], 'dl295_pdp_by_addr_and_product_url')
        self.assertEqual(request.call_args.args[1]['product_url'], row['Product URL'])
        self.assertEqual(value['item']['price_kind'], 'estimated')
        self.assertEqual(value['item']['price'], 21)
        self.assertFalse(value['item']['selectable'])
        self.assertIsNone(value['item']['provider_location_id'])
        self.assertFalse(value['quote_updated'])
        self.assertFalse(value['provider_cart_updated'])

    def test_cache_source_and_credential_rotation_are_isolated(self):
        cache = SearchCache()
        pilot = cache.key('63131', ['milk'], 'target-corp', False)
        with patch.dict(os.environ, {'FOODSPARK_ENABLED': 'false'}):
            browser = cache.key('63131', ['milk'], 'target-corp', False)
        with patch.dict(os.environ, {'FOODSPARK_API_KEY': 'different-fixture-key'}):
            rotated = cache.key('63131', ['milk'], 'target-corp', False)
        self.assertNotEqual(pilot, browser)
        self.assertNotEqual(pilot, rotated)
        self.assertNotIn('fixture-grocery-secret', json.dumps(pilot))


class GroceryFlowTests(unittest.TestCase):
    setUpClass = classmethod(test_api.MiddlewareTests.setUpClass.__func__)
    tearDownClass = classmethod(test_api.MiddlewareTests.tearDownClass.__func__)
    post = test_api.MiddlewareTests.post

    def contract(self, path, value):
        import jsonschema
        schema = api.SPEC['paths'][path]['post']['responses']['200']['content']['application/json']['schema']
        jsonschema.Draft202012Validator({**schema, 'components': api.SPEC['components']}).validate(value)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.catalog = Catalog(folder.name+'/catalog.sqlite')
        self.cache = SearchCache(independent_expiry=True).attach(self.catalog, 'instacart')
        self.lock = PriorityLock()
        self.env = patch.dict(os.environ, {**ENV, 'FOODSPARK_BUDGET_PATH': folder.name+'/budget.sqlite'})
        self.env.start()
        self.addCleanup(self.env.stop)
        service = SearchService(self.catalog, self.cache, self.lock, lambda *args: f.search_live(*args, api.search_browser))
        comparison = Comparison(self.catalog, self.cache, self.lock, lambda *args: f.search_live(*args, api.search_browser))
        for field, value in (('CATALOG', self.catalog), ('CACHE', self.cache), ('SEARCH', service),
                             ('COMPARISON', comparison), ('PLANNER', None), ('INGESTOR', None)):
            p = patch.object(api, field, value)
            p.start()
            self.addCleanup(p.stop)
        api.RESULTS.clear()
        api.RATE.clear()
        self.request = {'location': deepcopy(LOCATION), 'query': 'milk', 'retailer': 'target-corp'}

    def search(self, data=None):
        with patch.object(f.transport, 'request_json', return_value=data or sample()):
            return self.post('/api/v1/search', self.request)

    def test_63131_fixture_search_details_select_basket_and_link_handoff(self):
        code, search = self.search()
        self.assertEqual(code, 200)
        self.contract('/api/v1/search', search)
        item = search['items'][0]
        ref = {'search_id': search['search_id'], 'item_id': item['id']}
        original = sample()['response']['results'][0]
        detail = {'response': {'results': [{'Store Details': {'ShopName': 'Fixture Market', 'Zipcode': '63131',
                                                            'Street': 'Unverified branch address'}, 'Result': original}]}}
        with patch.object(f.transport, 'request_json', return_value=detail):
            code, details = self.post('/api/v1/products/details', ref)
        self.assertEqual(code, 200)
        self.assertFalse(details['quote_updated'])
        self.contract('/api/v1/products/details', details)
        self.assertFalse(details['handoff']['cart_transfer_supported'])
        self.assertEqual(details['handoff']['kind'], 'merchant_link')
        code, selection = self.post('/api/v1/select', {'search_id': ref['search_id'], 'item_ids': [ref['item_id']]})
        self.assertEqual(code, 200)
        self.assertEqual(selection['subtotal'], 3.25)
        code, cart = self.post('/api/v1/cart', {'lines': [{**ref, 'quantity': 3}]})
        self.assertEqual(code, 200)
        self.assertEqual((cart['subtotal'], cart['item_count']), (9.75, 3))
        self.assertFalse(cart['provider_cart_updated'])
        self.contract('/api/v1/cart', cart)

    def test_unknown_capture_and_missing_price_cannot_enter_basket(self):
        data = sample()
        data['response']['results'][0].pop('Captured At')
        data['response']['results'][0]['Price'] = None
        code, search = self.search(data)
        self.assertEqual(code, 200)
        self.assertIsNone(search['items'][0]['price'])
        self.contract('/api/v1/search', search)
        self.assertFalse(search['items'][0]['selectable'])
        ref = {'search_id': search['search_id'], 'item_id': search['items'][0]['id']}
        code, value = self.post('/api/v1/cart', {'lines': [{**ref, 'quantity': 1}]})
        self.assertEqual(code, 409)
        self.assertEqual(value['error'], 'source_freshness_unknown')
        code, value = self.post('/api/v1/select', {'search_id': ref['search_id'], 'item_ids': [ref['item_id']]})
        self.assertEqual(code, 409)

    def test_source_expiry_is_rechecked_at_cart_use(self):
        code, search = self.search()
        held = api.RESULTS.get(search['search_id'])['payload']['items'][0]
        held['source_expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        code, value = self.post('/api/v1/cart', {'lines': [{'search_id': search['search_id'], 'item_id': held['id'], 'quantity': 1}]})
        self.assertEqual(code, 409)
        self.assertEqual(value['error'], 'source_stale')

    def test_cache_hit_preserves_unknown_capture_and_comparison_source(self):
        data = sample()
        data['response']['results'][0].pop('Captured At')
        with patch.object(f.transport, 'request_json', return_value=data) as request:
            code, first = self.post('/api/v1/search', self.request)
            code, second = self.post('/api/v1/search', self.request)
            code, comparison = self.post('/api/v1/compare', {**self.request, 'providers': ['instacart'], 'mode': 'auto'})
        self.assertEqual(request.call_count, 1)
        self.assertTrue(second['cache']['hit'])
        self.assertFalse(second['items'][0]['price_fresh'])
        self.assertEqual(second['items'][0]['retrieved_at'], first['items'][0]['retrieved_at'])
        observed = comparison['providers'][0]['items'][0]
        self.assertEqual(observed['data_provider'], 'xbyte_foodspark')
        self.assertFalse(observed['price_fresh'])

    def test_details_rejects_client_url_and_missing_vendor_url(self):
        data = sample()
        data['response']['results'][0].pop('Product URL')
        _, search = self.search(data)
        ref = {'search_id': search['search_id'], 'item_id': search['items'][0]['id']}
        with patch.object(f.transport, 'request_json') as request:
            code, result = self.post('/api/v1/products/details', ref)
            self.assertEqual((code, result['error']), (409, 'product_link_unavailable'))
            self.assertEqual(self.post('/api/v1/products/details', {**ref, 'product_url': 'https://evil.test'})[0], 422)
        request.assert_not_called()

    def test_api_error_outcomes_are_distinct(self):
        for error, status in [('provider_auth', 502), ('provider_timeout', 504), ('provider_error', 502),
                              ('no_coverage', 422), ('no_match', 200)]:
            api.RATE.clear()
            with patch.object(f.transport, 'request_json', side_effect=ConnectorError(error, 'Sanitized fixture failure')):
                code, result = self.post('/api/v1/search', self.request)
            self.assertEqual(code, status)
            self.assertEqual(result['status'], error)
            self.assertEqual(result['items'], [])
            if status == 200:self.contract('/api/v1/search', result)

    def test_restaurant_http_discovery_menu_flow_is_separate_from_grocery(self):
        listing = json.loads((FIXTURES/'restaurant_synthetic_listing.json').read_text())
        menu = json.loads((FIXTURES/'restaurant_synthetic_menu.json').read_text())
        # Synthetic membership bridge between intentionally separate fixture
        # examples; this is not a live vendor-chain claim.
        menu_restaurant = menu['response']['results']
        listing['response']['results'].append({'restaurant_name': menu_restaurant['restaurant_name'],
            'restaurant_url': menu_restaurant['restaurant_url'], 'has_menu': True})
        with patch('xbyte_restaurants.request_json', return_value=listing):
            code, found = self.post('/api/v1/restaurants/search', {'zip': '63131', 'query': 'pizza', 'platform': 'yelp', 'page_no': 0})
        self.assertEqual(code, 200)
        restaurant = next(i for i in found['items'] if i['restaurant_url'] == menu_restaurant['restaurant_url'])
        self.contract('/api/v1/restaurants/search', found)
        with patch('xbyte_restaurants.request_json', return_value=menu) as request:
            code, result = self.post('/api/v1/restaurants/menu', {'discovery_id': found['discovery_id'],
                'restaurant_url': restaurant['restaurant_url'], 'platform': 'yelp', 'terms': ['Salad'], 'budget': 15, 'budget_operator': 'lt'})
        self.assertEqual(code, 200)
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(result['budget_assessment']['order_within_budget'])
        self.assertFalse(result['cart_transfer_supported'])
        self.contract('/api/v1/restaurants/menu', result)
        self.assertEqual(self.post('/api/v1/restaurants/search', {'zip': '63131', 'query': 'pizza', 'platform': 'doordash'})[0], 422)


if __name__ == '__main__':
    unittest.main()

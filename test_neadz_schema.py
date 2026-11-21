"""Neadz wire-contract checks through real local HTTP handlers.

All inventory and supplier responses in this module are deliberately synthetic.
The HTTP server is real, while upstream transports are replaced at their boundary.
These checks establish response/schema parity, never vendor availability.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

import api_server as api
import cancellation
from connector import ConnectorError, normalize
from ingestion import Catalog
from location_context import current_location
from provider_comparison import Comparison
from provider_session import request_context_id
from provider_test_adapters import ProviderReadError
from provider_tests import ProviderTests
from result_store import ResultStore
from search_cache import SearchCache
from search_jobs import SearchJobs
from search_service import SearchService


class NeadzSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = api.ThreadingHTTPServer(('127.0.0.1', 0), api.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.spec = json.loads((Path(__file__).parent / 'openapi.json').read_text())
        self.catalog = Catalog(Path(folder) / 'catalog.sqlite')
        self.cache = SearchCache().attach(self.catalog, 'instacart')
        self.lock = threading.Lock()
        self.browser = Mock(side_effect=self.synthetic_browser)
        self.jobs = SearchJobs()
        values = {
            'CATALOG': self.catalog, 'CACHE': self.cache, 'LOCK': self.lock,
            'RESULTS': ResultStore(), 'JOBS': self.jobs, 'PLANNER': None,
            'INGESTOR': None, 'RATE': {}, 'INVALIDATIONS': {},
            'SPEC': self.spec, 'OPERATOR_KEY': 'synthetic-operator-token',
            'search_browser': self.browser,
            'SEARCH': SearchService(self.catalog, self.cache, self.lock,
                                    lambda *args: api.search_live(*args, self.browser)),
            'COMPARISON': Comparison(self.catalog, self.cache, self.lock, self.browser),
            'PROVIDER_TESTS': ProviderTests(Path(folder) / 'test-budget.sqlite'),
        }
        for name, value in values.items():
            self.stack.enter_context(patch.object(api, name, value))
        self.stack.enter_context(patch.dict(os.environ, {
            'CONNECTOR_ENABLED': 'true', 'FOODSPARK_API_KEY': '',
            'INSTACART_STORAGE_STATE_PATH': '',
            'PROVIDER_TESTS_LIVE_ENABLED': 'true',
            'PROVIDER_TEST_TOKEN': 'synthetic-provider-test-token',
            'NORTHFORK_API_KEY': 'synthetic-northfork-key',
            'XBYTE_TEST_READ_LIMIT': '0', 'NORTHFORK_TEST_READ_LIMIT': '0',
        }))
        # Even a mistaken fixture implementation must not reach an upstream.
        self.vendor = self.stack.enter_context(patch(
            'provider_tests.fetch_provider', side_effect=AssertionError('Unexpected upstream call')))
        self.addCleanup(self.jobs.stop)

    @staticmethod
    def synthetic_browser(zip_code, queries, retailer, allow):
        items = []
        for query in queries:
            if query != 'milk':
                continue
            for store, price in [('aldi', '3.99'), ('target', '4.49')]:
                if retailer and retailer != store:
                    continue
                items.extend(normalize([{
                    'name': 'Synthetic Whole Milk',
                    'text': 'Current price: $' + price + ' each\n1 gal',
                    'url': '/products/123456-synthetic-whole-milk?retailerSlug=' + store,
                }], query, store, zip_code))
        return {
            'items': items,
            'location': {'requested_zip': zip_code, 'observed_zip': zip_code,
                         'verified': True,
                         'context_id': request_context_id(current_location(zip_code)),
                         'verification_scope': 'zip_only',
                         'delivery_serviceability_verified': False,
                         'store_branch_verified': False},
            'warnings': ['Synthetic test inventory; not live offers.'],
        }

    def validator(self, schema):
        return Draft202012Validator({**schema, 'components': self.spec['components']},
                                    format_checker=FormatChecker())

    @staticmethod
    def route(path):
        route = urlsplit(path).path
        if route.startswith('/api/v1/search/jobs/'):
            return '/api/v1/search/jobs/{id}/cancel' if route.endswith('/cancel') else '/api/v1/search/jobs/{id}'
        return route

    def request(self, method, path, body=None, token=None, expect=200,
                validate_request=True):
        operation = self.spec['paths'][self.route(path)][method.lower()]
        if body is not None and validate_request:
            schema = operation['requestBody']['content']['application/json']['schema']
            self.validator(schema).validate(body)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = Request(self.url + path, method=method, headers=headers,
                          data=None if body is None else json.dumps(body).encode())
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            data = json.load(response)
            self.assertEqual(response.status, expect, data)
            self.assertTrue(response.headers.get('X-Request-ID'))
            self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
        descriptor = operation['responses'].get(str(expect), operation['responses'].get('default'))
        self.assertIsNotNone(descriptor, f'{method} {path} HTTP {expect} is undocumented')
        if '$ref' in descriptor:
            descriptor = self.resolve(descriptor)
        self.assertIn('schema', descriptor.get('content', {}).get('application/json', {}),
                      f'{method} {path} HTTP {expect} has no response-body schema')
        body_schema = descriptor['content']['application/json']['schema']
        self.validator(body_schema).validate(data)
        self.assertFalse(set(data) - self.documented_properties(body_schema),
                         f'{method} {path} has undocumented top-level fields: '
                         f'{sorted(set(data) - self.documented_properties(body_schema))}')
        return data

    def resolve(self, schema):
        if '$ref' not in schema:
            return schema
        node = self.spec
        for part in schema['$ref'].removeprefix('#/').split('/'):
            node = node[part]
        return node

    def documented_properties(self, schema):
        schema = self.resolve(schema)
        fields = set(schema.get('properties', {}))
        for keyword in ('allOf', 'anyOf', 'oneOf'):
            for alternative in schema.get(keyword, []):
                fields.update(self.documented_properties(alternative))
        return fields

    def assert_model_fields_documented(self, name, value):
        declared = self.documented_properties(self.spec['components']['schemas'][name])
        self.assertFalse(set(value) - declared,
                         f'{name} omits emitted fields: {sorted(set(value) - declared)}')

    def poll(self, accepted, terminal=('succeeded', 'failed', 'cancelled')):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            status = self.request('GET', '/api/v1/search/jobs/' + accepted['job_id'],
                                  token=accepted['job_token'])
            if status['status'] in terminal:
                return status
            time.sleep(.01)
        self.fail('Synthetic job did not reach a terminal state')

    def test_openapi_and_every_json_example_validate(self):
        validate(self.spec)
        for path, methods in self.spec['paths'].items():
            for method, operation in methods.items():
                if method not in ('get', 'post', 'put', 'patch', 'delete'):
                    continue
                media_types = list(operation.get('requestBody', {}).get('content', {}).values())
                for response in operation.get('responses', {}).values():
                    media_types.extend(self.resolve(response).get('content', {}).values())
                for media in media_types:
                    examples = [media['example']] if 'example' in media else []
                    examples.extend(value['value'] for value in media.get('examples', {}).values()
                                    if 'value' in value)
                    for example in examples:
                        with self.subTest(path=path, method=method):
                            self.validator(media['schema']).validate(example)

    def test_search_select_cart_and_verified_savings_flow(self):
        search = self.request('POST', '/api/v1/search', {'zip': '63129', 'queries': ['milk']})
        self.assertEqual(search['source_providers'], ['instacart_browser'])
        self.assertEqual(len(search['items']), 2)
        self.assert_model_fields_documented('Product', search['items'][0])
        self.assert_model_fields_documented('Location', search['location'])
        self.assert_model_fields_documented('Cache', search['cache'])
        ids = [item['id'] for item in search['items']]
        selection = self.request('POST', '/api/v1/select',
                                 {'search_id': search['search_id'], 'item_ids': ids})
        self.assertEqual(selection['subtotal'], 8.48)
        cart = self.request('POST', '/api/v1/cart', {'lines': [
            {'search_id': search['search_id'], 'item_id': ids[0], 'quantity': 2}]})
        self.assertEqual(cart['subtotal'], 7.98)
        self.assertFalse(cart['provider_cart_updated'])
        savings = self.request('POST', '/api/v1/savings', {'offers': [
            {'search_id': search['search_id'], 'item_id': item_id} for item_id in ids], 'quantity': 2})
        self.assertEqual((savings['savings'], savings['replacement_subtotal']), (1.0, 7.98))
        self.assertEqual(savings['lower_price_merchant'], 'aldi')
        self.assertIn('excludes taxes', savings['scope'])

    def test_cache_miss_has_no_quote_and_partial_search_preserves_missing_queries(self):
        miss = self.request('POST', '/api/v1/search',
                            {'zip': '63129', 'queries': ['milk'], 'mode': 'cache_only'})
        self.assertEqual(miss['status'], 'cache_miss')
        self.assertNotIn('search_id', miss)
        self.browser.assert_not_called()
        partial = self.request('POST', '/api/v1/search',
                               {'zip': '63129', 'queries': ['milk', 'eggs']})
        self.assertEqual(partial['status'], 'partial')
        self.assertEqual(partial['missing_queries'], ['eggs'])
        self.assertTrue(partial['items'])

    def test_xbyte_nullable_product_fields_and_actual_source_are_valid(self):
        # Exercise the real FoodSpark adapter against a controlled HTTP response.
        import io
        import foodspark
        raw = {'request_log': {'status': '200'}, 'response': {
            'store_details': {'Delivery Zip Code': '63129', 'Store Slug': 'aldi',
                              'Delivery Address': '123 Synthetic Street', 'Store Location ID': 'fixture'},
            'results': [{'Product Name': 'Synthetic Milk', 'Price': '$3.99', 'Unit Size': None,
                         'Availability': None}],
        }}
        body = {'location': {'postal_code': '63129', 'address': {
            'line1': '123 Synthetic Street', 'city': 'Example City', 'region': 'MO'}},
                'queries': ['milk'], 'retailer': 'aldi'}
        with patch.dict(os.environ, {'FOODSPARK_API_KEY': 'synthetic-xbyte-key'}), \
                patch.object(foodspark, 'build_opener') as opener:
            opener.return_value.open.side_effect = lambda *a, **k: io.BytesIO(json.dumps(raw).encode())
            response = self.request('POST', '/api/v1/search', body)
        self.assertEqual(response['source_providers'], ['xbyte_foodspark'])
        self.assertIsNone(response['items'][0]['url'])
        self.assertIsNone(response['items'][0]['availability'])
        self.assertIsNone(response['items'][0]['size'])
        self.browser.assert_not_called()

    def test_search_errors_are_documented_json_and_do_not_become_empty_success(self):
        for reason, code in [('location_unsupported', 422), ('provider_quota', 502), ('busy', 429)]:
            with self.subTest(reason=reason):
                api.RATE.clear()
                self.browser.side_effect = ConnectorError(reason, 'Synthetic failure')
                error = self.request('POST', '/api/v1/search',
                                     {'zip': '63129', 'queries': ['milk']}, expect=code)
                self.assertEqual(error['error'], reason)
                self.assertEqual(error['items'], [])

    def test_catalog_and_export_do_not_issue_expired_quotes(self):
        self.request('POST', '/api/v1/search', {'zip': '63129', 'queries': ['milk']})
        self.request('POST', '/api/v1/cache/invalidate', {'zip': '63129', 'retailer': '*'})
        for path in ['/api/v1/catalog', '/api/v1/catalog/export']:
            with self.subTest(path=path):
                catalog = self.request('GET', path + '?provider=all&zip=63129&query=milk')
                self.assertTrue(catalog['items'])
                self.assertTrue(all(not item['price_fresh'] and not item['selectable']
                                    for item in catalog['items']))
                self.assertTrue(all(item['last_seen_price'] == item['price'] for item in catalog['items']))
                self.request('POST', '/api/v1/cart', {'lines': [{
                    'search_id': catalog['search_id'], 'item_id': catalog['items'][0]['id'], 'quantity': 1}]}, expect=409)
                unscoped = self.request('GET', path + '?provider=all&query=milk')
                self.assertIsNone(unscoped['location']['requested_zip'])
                self.assertFalse(unscoped['location']['verified'])

    def test_comparison_and_status_endpoints_are_typed(self):
        self.request('POST', '/api/v1/search', {'zip': '63129', 'queries': ['milk']})
        comparison = self.request('POST', '/api/v1/compare',
                                  {'zip': '63129', 'queries': ['milk'], 'mode': 'cache_only'})
        self.assertEqual([part['provider'] for part in comparison['providers']], ['instacart'])
        self.assert_model_fields_documented('ProviderComparison', comparison['providers'][0])
        for path in ['/health', '/ready', '/api/v1/ingestion/status',
                     '/api/v1/providers/status', '/api/v1/provider-tests/status']:
            with self.subTest(path=path):
                self.request('GET', path)

    def test_retired_selection_is_rejected_before_any_fetch(self):
        self.request('POST', '/api/v1/compare', {'zip': '63129', 'queries': ['milk'],
                     'mode': 'live', 'providers': ['instacart', 'mealme_satsuma']},
                     expect=410, validate_request=False)
        self.request('GET', '/api/v1/catalog/export?provider=mealme_satsuma', expect=410)
        self.browser.assert_not_called()
        self.vendor.assert_not_called()

    def test_async_cache_miss_and_partial_result_share_the_search_contract(self):
        miss = self.request('POST', '/api/v1/search/jobs', {'zip': '63129',
                            'queries': ['milk'], 'mode': 'cache_only'}, expect=202)
        self.assertEqual(self.poll(miss)['result']['status'], 'cache_miss')
        partial = self.request('POST', '/api/v1/search/jobs',
                               {'zip': '63129', 'queries': ['milk', 'eggs']}, expect=202)
        result = self.poll(partial)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['result']['status'], 'partial')
        self.assertEqual(result['result']['missing_queries'], ['eggs'])
        self.request('GET', '/api/v1/search/jobs/' + partial['job_id'], expect=403)

    def test_async_failure_is_http200_with_typed_failed_state(self):
        self.browser.side_effect = ConnectorError('location_required', 'Synthetic address gate')
        accepted = self.request('POST', '/api/v1/search/jobs',
                                {'zip': '63129', 'queries': ['milk']}, expect=202)
        failed = self.poll(accepted)
        self.assertEqual(failed['status'], 'failed')
        self.assertIsNone(failed['result'])
        self.assertEqual(failed['error']['code'], 'location_required')

    def test_async_cancellation_is_authenticated_and_reaches_terminal_state(self):
        started = threading.Event()

        def waiting(*args):
            started.set()
            while True:
                cancellation.check()
                time.sleep(.005)

        self.browser.side_effect = waiting
        accepted = self.request('POST', '/api/v1/search/jobs',
                                {'zip': '63129', 'queries': ['milk']}, expect=202)
        self.assertTrue(started.wait(1))
        path = '/api/v1/search/jobs/' + accepted['job_id'] + '/cancel'
        self.request('POST', path, {}, expect=403)
        self.request('POST', path, {}, token=accepted['job_token'])
        result = self.poll(accepted)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['cancel_requested'])
        self.assertEqual(result['error']['code'], 'cancelled')

    def test_supplier_fixtures_are_typed_non_selectable_and_have_no_upstream_reads(self):
        request = {'zip': '63129', 'queries': ['milk'], 'retailer': 'schnucks', 'mode': 'fixture'}
        for provider in ['xbyte_foodspark', 'northfork', 'actowiz']:
            with self.subTest(provider=provider):
                outcome = self.request('POST', '/api/v1/provider-tests/search',
                                       {**request, 'provider': provider})
                self.assertIsNone(outcome['actual_provider'])
                self.assertEqual(outcome['upstream_requests'], 0)
                self.assertTrue(outcome['items'])
                self.assertTrue(all(not item['selectable'] for item in outcome['items']))
        comparison = self.request('POST', '/api/v1/provider-tests/compare',
                                  {**request, 'providers': ['xbyte_foodspark', 'northfork', 'actowiz']})
        self.assertEqual(comparison['status'], 'ok')
        self.assertEqual(len(comparison['providers']), 3)
        self.vendor.assert_not_called()
        self.assertEqual(len(api.RESULTS), 0)

    def test_supplier_auth_and_failure_envelopes_are_typed(self):
        body = {'provider': 'northfork', 'zip': '63129', 'queries': ['milk'],
                'retailer': 'schnucks', 'mode': 'live'}
        auth = self.request('POST', '/api/v1/provider-tests/search', body, expect=403)
        self.assertEqual(auth['error'], 'test_token_required')
        self.vendor.assert_not_called()
        self.vendor.side_effect = ProviderReadError('provider_timeout', 'Synthetic timeout', 504)
        outcome = self.request('POST', '/api/v1/provider-tests/search', body,
                               token='synthetic-provider-test-token', expect=504)
        self.assertEqual(outcome['status'], 'error')
        self.assertEqual(outcome['error']['code'], 'provider_timeout')
        self.assertEqual(outcome['items'], [])
        with patch.dict(os.environ, {'PROVIDER_TESTS_LIVE_ENABLED': 'false'}):
            disabled = self.request('POST', '/api/v1/provider-tests/search', body, expect=503)
        self.assertEqual(disabled['error'], 'live_tests_disabled')


if __name__ == '__main__':
    unittest.main()

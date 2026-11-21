"""HTTP contract checks with local fixtures; never contact an upstream supplier."""
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import jsonschema

import api_server as api
import deadline
from connector import ConnectorError, normalize
from ingestion import Catalog
from location_context import set_location
from provider_comparison import Comparison
from result_store import ResultStore
from search_cache import SearchCache
from search_jobs import SearchJobs
from search_service import SearchService


class RuntimeContractTests(unittest.TestCase):
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
        self.catalog = Catalog(Path(self.temp.name) / 'catalog.sqlite')
        self.cache = SearchCache().attach(self.catalog, 'instacart')
        self.results = ResultStore()
        self.jobs = SearchJobs()
        self.addCleanup(self.jobs.stop)
        self.fetch = Mock(side_effect=self.fetch_products)
        self.direct = Mock(side_effect=AssertionError('No real diagnostic HTTP requests are permitted'))
        lock = threading.Lock()
        self.search = SearchService(self.catalog, self.cache, lock, self.fetch)
        self.comparison = Comparison(self.catalog, self.cache, lock, self.fetch)
        replacements = {
            'CATALOG': self.catalog, 'CACHE': self.cache, 'RESULTS': self.results,
            'SEARCH': self.search, 'JOBS': self.jobs, 'COMPARISON': self.comparison,
            'INGESTOR': None, 'PLANNER': None, 'OPERATOR_KEY': 'synthetic-operator',
            'search_http': self.direct,
        }
        for name, value in replacements.items():
            patcher = patch.object(api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        enabled = patch.dict(os.environ, {'CONNECTOR_ENABLED': 'true'})
        enabled.start()
        self.addCleanup(enabled.stop)
        api.RATE.clear()
        api.INVALIDATIONS.clear()

    def request(self, path, body=None, authorization=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if authorization:
            headers['Authorization'] = authorization
        try:
            client.request('GET' if body is None else 'POST', path,
                           body=None if body is None else json.dumps(body), headers=headers)
            response = client.getresponse()
            self.assertTrue(response.getheader('X-Request-ID'))
            self.assertEqual(response.getheader('Content-Type'), 'application/json')
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def product(self, query='milk', retailer='aldi', amount='4.00'):
        item = normalize([{'name': query.title(), 'text': 'Current price: $' + amount + '\n1 gal',
                           'url': '/products/123-milk?retailerSlug=' + retailer}],
                         query, retailer, '63129')[0]
        return {**item, 'price_basis': 'per_item', 'price_basis_verified': True,
                'price_unit': 'item', 'source_provider': 'instacart_browser'}

    def payload(self, items, postal_code='63129'):
        return {'items': items, 'location': {'requested_zip': postal_code,
                'observed_zip': postal_code, 'verified': True, 'context_id': 'zip:' + postal_code},
                'warnings': []}

    def fetch_products(self, zip_code, queries, retailer, allow):
        return self.payload([self.product(query, retailer or 'aldi') for query in queries], zip_code)

    def quote(self, key='quote', item=None, postal_code='63129'):
        item = item or self.product()
        self.results[key] = {'created': time.time(), 'expires': time.time() + 120,
                             'payload': self.payload([item], postal_code)}
        return {'search_id': key, 'item_id': item['id']}

    def wait_job(self, accepted):
        for _ in range(100):
            code, result = self.request('/api/v1/search/jobs/' + accepted['job_id'],
                                        authorization='Bearer ' + accepted['job_token'])
            self.assertEqual(code, 200)
            if result['status'] not in ('queued', 'running'):
                return result
            time.sleep(.01)
        self.fail('Fixture job did not finish')

    def validate_response(self, path, method, code, response):
        spec = json.loads((Path(api.__file__).parent / 'openapi.json').read_text())
        operation = spec['paths'][path][method]
        self.assertIn(str(code), operation['responses'], (path, code))
        schema = operation['responses'][str(code)]['content']['application/json']['schema']
        jsonschema.Draft202012Validator({**schema, 'components': spec['components']}).validate(response)

    def test_search_request_schema_matches_query_and_address_validation(self):
        spec = json.loads((Path(api.__file__).parent / 'openapi.json').read_text())
        schema = spec['paths']['/api/v1/search']['post']['requestBody']['content']['application/json']['schema']
        validator = jsonschema.Draft202012Validator({**schema, 'components': spec['components']})
        address = {'line1': '100 Test Street', 'line2': '', 'city': 'Test City', 'region': 'MO'}
        valid = [
            {'zip': '63129', 'query': 'milk'},
            {'location': {'postal_code': '63129'}, 'queries': ['milk']},
            {'zip': '63129', 'location': {'address': address}, 'queries': ['milk']},
        ]
        for body in valid:
            with self.subTest(valid=body):
                validator.validate(body)
                self.assertEqual(api.validate(body)[0], '63129')
        invalid = [
            {'zip': '63129'},
            {'zip': '63129', 'queries': []},
            {'zip': '63129', 'query': 'milk', 'location': {'address': {**address, 'line2': 'x' * 101}}},
        ]
        for body in invalid:
            with self.subTest(invalid=body):
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(body)
                with self.assertRaises(ValueError):
                    api.validate(body)

    def test_search_partial_and_cache_miss_are_distinct_success_models(self):
        code, miss = self.request('/api/v1/search', {'zip': '63129', 'queries': ['milk'], 'mode': 'cache_only'})
        self.assertEqual((code, miss['status']), (200, 'cache_miss'))
        self.assertEqual(miss['missing_queries'], ['milk'])
        self.assertNotIn('search_id', miss)
        self.fetch.assert_not_called()
        code, fresh = self.request('/api/v1/search', {'zip': '63129', 'queries': ['milk']})
        self.assertEqual((code, fresh['status']), (200, 'ok'))
        code, partial = self.request('/api/v1/search', {'zip': '63129', 'queries': ['milk', 'eggs'], 'mode': 'cache_only'})
        self.assertEqual((code, partial['status']), (200, 'partial'))
        self.assertEqual(partial['missing_queries'], ['eggs'])
        self.assertEqual(partial['items'][0]['id'], fresh['items'][0]['id'])
        self.assertEqual(self.fetch.call_count, 1)
        for response in (miss, fresh, partial):
            self.validate_response('/api/v1/search', 'post', 200, response)

    def test_job_modes_errors_and_capability_access_match_poll_contract(self):
        for mode in ('catalog', 'live'):
            with self.subTest(mode=mode):
                code, error = self.request('/api/v1/search/jobs', {'zip': '63129', 'query': 'milk', 'mode': mode})
                self.assertEqual((code, error['error']), (422, 'invalid_request'))
        self.fetch.side_effect = ConnectorError('provider_timeout', 'Synthetic supplier timeout')
        code, accepted = self.request('/api/v1/search/jobs', {'zip': '63129', 'query': 'milk'})
        self.assertEqual((code, accepted['status'], accepted['expires_in']), (202, 'queued', 600))
        self.validate_response('/api/v1/search/jobs', 'post', 202, accepted)
        code, denied = self.request('/api/v1/search/jobs/' + accepted['job_id'], authorization='Bearer wrong-token')
        self.assertEqual((code, denied['error']), (403, 'job_access_denied'))
        failed = self.wait_job(accepted)
        self.assertEqual((failed['status'], failed['error']['code']), ('failed', 'provider_timeout'))
        self.assertIsNone(failed['result'])
        self.validate_response('/api/v1/search/jobs/{id}', 'get', 200, failed)
        self.validate_response('/api/v1/search/jobs/{id}', 'get', 403, denied)

    def test_job_cache_miss_result_and_cancel_body_are_explicit(self):
        code, accepted = self.request('/api/v1/search/jobs', {'zip': '63129', 'query': 'milk', 'mode': 'cache_only'})
        self.assertEqual(code, 202)
        finished = self.wait_job(accepted)
        self.assertEqual((finished['status'], finished['result']['status']), ('succeeded', 'cache_miss'))
        self.fetch.assert_not_called()
        self.validate_response('/api/v1/search/jobs/{id}', 'get', 200, finished)
        path = '/api/v1/search/jobs/' + accepted['job_id'] + '/cancel'
        code, error = self.request(path, {'cancel': True}, 'Bearer ' + accepted['job_token'])
        self.assertEqual((code, error['error']), (422, 'invalid_request'))
        code, terminal = self.request(path, {}, 'Bearer ' + accepted['job_token'])
        self.assertEqual((code, terminal['status'], terminal['cancel_requested']), (200, 'succeeded', False))
        self.validate_response('/api/v1/search/jobs/{id}/cancel', 'post', 200, terminal)
        self.validate_response('/api/v1/search/jobs/{id}/cancel', 'post', 422, error)

    def test_selection_expiry_cart_expiry_and_mixed_contexts_remain_distinct(self):
        code, selection = self.request('/api/v1/select', {'search_id': 'expired', 'item_ids': ['milk']})
        self.assertEqual((code, selection['error']), (404, 'search_expired'))
        code, cart = self.request('/api/v1/cart', {'lines': [{'search_id': 'expired', 'item_id': 'milk', 'quantity': 1}]})
        self.assertEqual((code, cart['error']), (409, 'price_expired'))
        first = self.quote('first')
        second = self.quote('second', postal_code='63110')
        code, mixed = self.request('/api/v1/cart', {'lines': [{**first, 'quantity': 1}, {**second, 'quantity': 1}]})
        self.assertEqual((code, mixed['error']), (409, 'mixed_location_context'))
        self.validate_response('/api/v1/select', 'post', 404, selection)
        self.validate_response('/api/v1/cart', 'post', 409, mixed)

    def test_empty_cart_and_deduplicated_selection_preserve_existing_behavior(self):
        for body in ({}, {'lines': []}):
            code, empty = self.request('/api/v1/cart', body)
            self.assertEqual((code, empty['item_count'], empty['subtotal']), (200, 0, 0))
            self.assertFalse(empty['provider_cart_updated'])
            self.validate_response('/api/v1/cart', 'post', 200, empty)
        ref = self.quote()
        code, selected = self.request('/api/v1/select', {'search_id': ref['search_id'], 'item_ids': [ref['item_id'], ref['item_id']]})
        self.assertEqual(code, 200)
        self.assertEqual(len(selected['items']), 1)
        self.assertEqual(selected['subtotal'], 4.0)
        self.validate_response('/api/v1/select', 'post', 200, selected)

    def test_global_invalidation_accepts_null_zip_expires_quotes_and_throttles(self):
        self.quote()
        code, result = self.request('/api/v1/cache/invalidate', {'retailer': '*', 'zip': None})
        self.assertEqual(code, 200)
        self.assertIsNone(result['zip'])
        self.assertEqual(result['expired_price_sets'], 1)
        self.assertEqual(len(self.results), 0)
        self.validate_response('/api/v1/cache/invalidate', 'post', 200, result)
        code, error = self.request('/api/v1/cache/invalidate', {'retailer': '*', 'zip': None})
        self.assertEqual((code, error['error']), (429, 'rate_limited'))
        self.fetch.assert_not_called()

    def test_recovery_requires_literal_true_and_never_probes_on_invalid_acknowledgment(self):
        worker = Mock()
        worker.recover.return_value = {'status': 'already_running', 'message': 'No pause needs recovery.'}
        with patch.object(api, 'INGESTOR', worker):
            for value in (1, 1.0, 'true', False, None):
                with self.subTest(acknowledge=value):
                    code, error = self.request('/api/v1/ingestion/recover', {'acknowledge': value}, 'Bearer synthetic-operator')
                    self.assertEqual((code, error['error']), (422, 'invalid_request'))
            worker.recover.assert_not_called()
            code, result = self.request('/api/v1/ingestion/recover', {'acknowledge': True}, 'Bearer synthetic-operator')
            self.assertEqual((code, result['status']), (200, 'already_running'))
            worker.recover.assert_called_once()

    def test_recovery_auth_disabled_worker_and_probe_failure_are_typed(self):
        code, denied = self.request('/api/v1/ingestion/recover', {'acknowledge': True})
        self.assertEqual((code, denied['error']), (403, 'operator_required'))
        code, disabled = self.request('/api/v1/ingestion/recover', {'acknowledge': True}, 'Bearer synthetic-operator')
        self.assertEqual((code, disabled['error']), (409, 'worker_disabled'))
        worker = Mock()
        worker.recover.side_effect = ConnectorError('provider_blocked', 'Synthetic screening pause')
        with patch.object(api, 'INGESTOR', worker):
            code, blocked = self.request('/api/v1/ingestion/recover', {'acknowledge': True}, 'Bearer synthetic-operator')
        self.assertEqual((code, blocked['status']), (409, 'provider_blocked'))
        self.assertNotIn('error', blocked)
        self.validate_response('/api/v1/ingestion/recover', 'post', 409, blocked)

    def test_search_deadline_returns_provider_timeout_json(self):
        self.fetch.side_effect = deadline.DeadlineExceeded('Synthetic deadline')
        code, error = self.request('/api/v1/search', {'zip': '63129', 'query': 'milk'})
        self.assertEqual((code, error['status'], error['error']), (502, 'provider_timeout', 'provider_timeout'))
        self.assertEqual(error['items'], [])

    def test_diagnostic_deadline_returns_json_instead_of_dropping_connection(self):
        self.direct.side_effect = deadline.DeadlineExceeded('Synthetic deadline')
        code, error = self.request('/api/v1/diagnostics/http-compare', {'zip': '63129', 'query': 'milk'})
        self.assertEqual((code, error['status'], error['error']), (502, 'provider_timeout', 'provider_timeout'))
        self.fetch.assert_not_called()

    def test_diagnostic_cache_only_applies_only_to_browser_branch(self):
        self.direct.side_effect = None
        self.direct.return_value = {'status': 'no_readable_products', 'source': 'direct_http', 'items': [],
            'location': {'requested_zip': '63129', 'observed_zip': None, 'verified': False},
            'cart_supported': False, 'warnings': [], 'duration_ms': 1}
        code, result = self.request('/api/v1/diagnostics/http-compare',
                                    {'zip': '63129', 'query': 'milk', 'browser_mode': 'cache_only'})
        self.assertEqual((code, result['status'], result['browser_mode']), (200, 'compared', 'cache_only'))
        self.direct.assert_called_once_with('63129', 'milk', '')
        self.fetch.assert_not_called()
        self.validate_response('/api/v1/diagnostics/http-compare', 'post', 200, result)

    def test_savings_success_and_validation_errors_have_explicit_wire_shapes(self):
        first = self.quote('original', self.product(retailer='aldi', amount='4.00'))
        second = self.quote('replacement', self.product(retailer='schnucks', amount='3.00'))
        code, result = self.request('/api/v1/savings', {'offers': [first, second], 'quantity': 2})
        self.assertEqual((code, result['savings'], result['original_subtotal'], result['replacement_subtotal']), (200, 2.0, 8.0, 6.0))
        self.assertEqual(result['lower_price_merchant'], 'schnucks')
        self.validate_response('/api/v1/savings', 'post', 200, result)
        code, invalid = self.request('/api/v1/savings', {'offers': [first, second], 'quantity': True})
        self.assertEqual((code, invalid['error']), (422, 'invalid_request'))
        self.results.clear()
        code, expired = self.request('/api/v1/savings', {'offers': [first, second]})
        self.assertEqual((code, expired['error']), (409, 'price_expired'))
        self.validate_response('/api/v1/savings', 'post', 409, expired)
        self.validate_response('/api/v1/savings', 'post', 422, invalid)

    def test_ingestion_pagination_is_clamped_and_invalid_values_are_rejected(self):
        code, disabled = self.request('/api/v1/ingestion/status')
        self.assertEqual((code, disabled['enabled']), (200, False))
        self.validate_response('/api/v1/ingestion/status', 'get', 200, disabled)
        worker = Mock()
        worker.status.return_value = {'enabled': True}
        with patch.object(api, 'INGESTOR', worker):
            self.assertEqual(self.request('/api/v1/ingestion/status?offset=-4&limit=999')[0], 200)
            worker.status.assert_called_once_with(0, 100)
            code, invalid = self.request('/api/v1/ingestion/status?offset=invalid')
            self.assertEqual((code, invalid['error']), (422, 'invalid_request'))
            self.assertEqual(worker.status.call_count, 1)

    def test_liveness_and_storage_readiness_are_distinct(self):
        code, health = self.request('/health')
        self.assertEqual((code, health['status']), (200, 'ok'))
        code, ready = self.request('/ready')
        self.assertEqual((code, ready['status'], ready['storage_available']), (200, 'ready', True))
        self.validate_response('/ready', 'get', 200, ready)
        with patch.object(self.catalog, 'db', side_effect=sqlite3.OperationalError('Synthetic storage error')):
            code, failed = self.request('/ready')
            self.assertEqual((code, failed['status']), (503, 'not_ready'))
        self.validate_response('/ready', 'get', 503, failed)

"""Supplier tests never fall back, reuse quotes, or write shopping state."""
import copy
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from provider_tests import ProviderTests, ReadBudget, TestRequestError, validate_request, fixture
from provider_test_adapters import ProviderReadError


REQUEST = {'provider': 'northfork', 'zip': '63129', 'queries': ['milk'], 'retailer': 'schnucks',
           'mode': 'fixture', 'fallback_policy': 'none'}
LIVE_ENV = {'PROVIDER_TESTS_LIVE_ENABLED': 'true', 'PROVIDER_TEST_TOKEN': 'synthetic-test-token',
            'NORTHFORK_TEST_READ_LIMIT': '2', 'XBYTE_TEST_READ_LIMIT': '2'}


class ProviderTestsUnit(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.lab = ProviderTests(self.folder.name + '/budget.sqlite3')

    def test_fixtures_make_no_calls_or_budget_writes(self):
        body = {k: v for k, v in REQUEST.items() if k != 'provider'}
        body['providers'] = ['xbyte_foodspark', 'northfork', 'actowiz']
        with patch('provider_tests.fetch_provider') as fetch:
            code, result = self.lab.run(body, comparison=True)
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(os.path.exists(self.lab.budget.path))
        fetch.assert_not_called()
        for outcome in result['providers']:
            self.assertIsNone(outcome['actual_provider'])
            self.assertEqual(outcome['upstream_requests'], 0)
            self.assertFalse(outcome['fallback_used'])
            self.assertTrue(outcome['items'])
            self.assertTrue(all(i['source_mode'] == 'fixture' and not i['selectable'] for i in outcome['items']))
            self.assertNotIn('search_id', outcome)

    def test_no_fallback_mode_and_strict_provider_fields(self):
        for update in ({'fallback_policy': 'auto'}, {'provider': 'instacart'}, {'provider': ['northfork']},
                       {'options': {'api_key': 'never-accept-a-client-key'}}, {'mode': 'auto'},
                       {'location': {'postal_code': '90210'}}, {'queries': []}, {'queries': ['x'] * 4}):
            with self.subTest(update=update), self.assertRaises(TestRequestError):
                validate_request({**REQUEST, **update})

    def test_live_requires_flag_and_dedicated_token(self):
        with patch.dict(os.environ, {}, clear=True), patch('provider_tests.fetch_provider') as fetch:
            with self.assertRaises(TestRequestError) as error:
                self.lab.run({**REQUEST, 'mode': 'live'})
            self.assertEqual(error.exception.code, 'live_tests_disabled')
            fetch.assert_not_called()
        with patch.dict(os.environ, LIVE_ENV, clear=True), patch('provider_tests.fetch_provider') as fetch:
            with self.assertRaises(TestRequestError) as error:
                self.lab.run({**REQUEST, 'mode': 'live'}, 'Bearer incorrect')
            self.assertEqual(error.exception.http_status, 403)
            fetch.assert_not_called()

    def test_failed_supplier_does_not_switch_and_counts_attempts(self):
        def failure(provider, request, reserve):
            self.assertEqual(provider, 'northfork')
            reserve()
            raise ProviderReadError('provider_timeout', 'Selected supplier timed out.', 504)
        with patch.dict(os.environ, LIVE_ENV, clear=True), patch('provider_tests.fetch_provider', side_effect=failure) as fetch:
            code, result = self.lab.run({**REQUEST, 'mode': 'live'}, 'Bearer synthetic-test-token')
        self.assertEqual(code, 504)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result['actual_provider'], 'northfork')
        self.assertEqual(result['error']['code'], 'provider_timeout')
        self.assertEqual(result['upstream_requests'], 1)
        self.assertEqual(result['items'], [])
        self.assertFalse(result['fallback_used'])

    def test_allowance_persists_and_is_atomic(self):
        with patch.dict(os.environ, LIVE_ENV, clear=True):
            errors = []
            def reserve():
                try:
                    ReadBudget(self.lab.budget.path).reserve('northfork')
                except ProviderReadError as e:
                    errors.append(e.code)
            threads = [threading.Thread(target=reserve) for _ in range(6)]
            for t in threads:t.start()
            for t in threads:t.join()
            self.assertEqual(ReadBudget(self.lab.budget.path).used('northfork'), 2)
            self.assertEqual(errors, ['read_budget_exhausted'] * 4)
            self.assertEqual(self.lab.budget.used('xbyte_foodspark'), 0)

    def test_comparison_preserves_provider_error_and_context(self):
        import deadline
        observed = []
        def fetch(provider, request, reserve):
            observed.append((provider, deadline.remaining(), request['options']))
            if provider == 'actowiz':
                raise ProviderReadError('unsupported_operation', 'Keyword search unavailable.', 422)
            reserve()
            return fixture(provider)
        body = {k: v for k, v in REQUEST.items() if k != 'provider'}
        body.update(mode='live', providers=['northfork', 'actowiz'], options={'northfork': {'store_id': 'synthetic'}})
        token = deadline.begin(15)
        try:
            with patch.dict(os.environ, LIVE_ENV, clear=True), patch('provider_tests.fetch_provider', side_effect=fetch):
                code, result = self.lab.run(body, 'Bearer synthetic-test-token', comparison=True)
        finally:
            deadline.reset(token)
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['providers'][1]['error']['code'], 'unsupported_operation')
        self.assertTrue(all(0 < remaining <= 15 for _, remaining, _ in observed))
        self.assertIn(('northfork', {'store_id': 'synthetic'}), [(p, o) for p, _, o in observed])

    def test_unexpected_exception_is_sanitized(self):
        with patch.dict(os.environ, LIVE_ENV, clear=True), patch('provider_tests.fetch_provider', side_effect=ValueError('SECRET / full address')):
            code, result = self.lab.run({**REQUEST, 'mode': 'live'}, 'Bearer synthetic-test-token')
        self.assertEqual(code, 502)
        self.assertNotIn('SECRET', json.dumps(result))


class ProviderTestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api_server
        cls.api = api_server
        cls.server = api_server.ThreadingHTTPServer(('127.0.0.1', 0), api_server.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, path, body):
        try:
            result = urlopen(Request(self.url + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}))
        except HTTPError as error:
            result = error
        return result.status, json.load(result)

    def test_http_fixture_cannot_enter_quote_selection_or_cart(self):
        api = self.api
        before = (len(api.RESULTS), copy.deepcopy(api.CACHE.entries), api.CATALOG.counts())
        with patch.object(api, 'INGESTOR') as ingestor, patch.object(api, 'PLANNER') as planner:
            code, outcome = self.post('/api/v1/provider-tests/search', REQUEST)
            self.assertEqual(code, 200)
            ingestor.observe.assert_not_called()
            planner.promote.assert_not_called()
        self.assertEqual(before, (len(api.RESULTS), api.CACHE.entries, api.CATALOG.counts()))
        item = outcome['items'][0]
        self.assertEqual(self.post('/api/v1/select', {'search_id': outcome['test_id'], 'item_ids': [item['id']]})[0], 404)
        self.assertEqual(self.post('/api/v1/cart', {'lines': [{'search_id': outcome['test_id'], 'item_id': item['id'], 'quantity': 1}]})[0], 409)

    def test_status_and_fixture_contract(self):
        import jsonschema
        status = json.load(urlopen(self.url + '/api/v1/provider-tests/status'))
        self.assertEqual(status['fallback_policy'], 'none')
        code, outcome = self.post('/api/v1/provider-tests/search', REQUEST)
        self.assertEqual(code, 200)
        jsonschema.Draft202012Validator({'$ref': '#/components/schemas/ProviderTestOutcome', 'components': self.api.SPEC['components']}).validate(outcome)


if __name__ == '__main__':
    unittest.main()

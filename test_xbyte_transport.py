"""Offline XBYTE-01 transport tests. Every response and credential is synthetic."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from connector import ConnectorError
from telemetry import REQUEST_ID
import xbyte_transport as transport


class Response(io.BytesIO):
    def __init__(self, payload=None, status=200, raw=None):
        super().__init__(raw if raw is not None else json.dumps(payload).encode())
        self.status = status
        self.read_limits = []

    def read(self, size=-1):
        self.read_limits.append(size)
        return super().read(size)


class XByteTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'calls.sqlite')
        self.env = patch.dict(os.environ, {
            'FOODSPARK_ENABLED': 'true', 'FOODSPARK_RESTAURANTS_ENABLED': 'false',
            'FOODSPARK_API_KEY': 'fixture-secret-DO-NOT-LOG',
            'FOODSPARK_READ_BUDGET': '20', 'FOODSPARK_MAX_ATTEMPTS': '2',
            'FOODSPARK_BUDGET_PATH': self.path,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.trace = REQUEST_ID.set('fixture-safe-request-id')
        self.addCleanup(REQUEST_ID.reset, self.trace)
        self.pause = patch.object(transport.time, 'sleep')
        self.pause.start()
        self.addCleanup(self.pause.stop)

    def rows(self):
        if not Path(self.path).exists():
            return []
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT operation,status,request_id FROM xbyte_calls ORDER BY id').fetchall()

    def error(self, code, params=None, path='/search'):
        with self.assertRaises(ConnectorError) as caught:
            transport.request_json(path, params or {'search_term': 'milk'})
        self.assertEqual(caught.exception.status, code)
        return caught.exception

    def success(self, **extra):
        return {'request_log': {'status': 200}, 'response': {'results': []}, **extra}

    def test_checked_in_default_is_disabled_without_environment_override(self):
        os.environ.pop('FOODSPARK_ENABLED')
        with patch.object(transport, 'build_opener') as build:
            self.assertFalse(transport.enabled())
            self.error('provider_disabled')
        build.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_missing_key_and_zero_budget_make_no_http_request(self):
        with patch.object(transport, 'build_opener') as build:
            with patch.dict(os.environ, {'FOODSPARK_API_KEY': ''}):
                self.error('not_configured')
            with patch.dict(os.environ, {'FOODSPARK_READ_BUDGET': '0'}):
                self.error('budget_exhausted')
        build.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_unsupported_route_or_caller_credential_is_rejected_before_http(self):
        with patch.object(transport, 'build_opener') as build:
            self.error('unsupported_operation', path='/cart')
            self.error('unsupported_operation', params={'api_key': 'caller-supplied-secret'})
        build.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_documented_get_shape_takes_key_only_from_server_configuration(self):
        params = {'country': 'usa', 'platform': 'web', 'domain': 'instacart',
                  'endpoint_type': 'dl295_search_by_addr_brand_slug_and_keyword',
                  'brand_slug': 'target', 'search_term': 'milk', 'zipcode': '63131',
                  'address': 'Synthetic Address, St. Louis, MO 63131'}
        response = Response(self.success())
        opener = Mock()
        opener.open.return_value = response
        with patch.object(transport, 'build_opener', return_value=opener) as build:
            transport.request_json('/search', params)
        request = opener.open.call_args.args[0]
        parsed = urlsplit(request.full_url)
        sent = parse_qs(parsed.query)
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.netloc, 'foodsparkfastapiframework.xbyte.io')
        self.assertEqual(parsed.path, '/search')
        self.assertEqual(sent, {**{k: [v] for k, v in params.items()},
                                'api_key': ['fixture-secret-DO-NOT-LOG']})
        self.assertNotIn('api_key', params)
        self.assertEqual(request.get_header('X-request-id'), 'fixture-safe-request-id')
        self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 45)
        self.assertIsInstance(build.call_args.args[0], transport.NoRedirect)
        self.assertEqual(response.read_limits, [1_000_001])
        self.assertEqual(self.rows(), [('/search', 'ok', 'fixture-safe-request-id')])

    def test_http_200_nested_auth_is_a_nonretryable_failure(self):
        payload = {'request_log': {'status': 200}, 'response': {
            'result': [], 'error': {'status_code': 401, 'message': 'Invalid API key'}}}
        opener = Mock()
        opener.open.return_value = Response(payload)
        with patch.object(transport, 'build_opener', return_value=opener):
            self.error('provider_auth')
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(self.rows()[0][1], 'provider_auth')

    def test_http_200_nested_menu_500_is_not_empty_success(self):
        payload = {'request_log': {'status': 200}, 'response': {
            'result': [], 'error': {'code': 500, 'message': 'Internal server error'}}}
        opener = Mock()
        opener.open.side_effect = [Response(payload), Response(payload)]
        with patch.object(transport, 'build_opener', return_value=opener):
            self.error('provider_error', path='/restaurant_menu')
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual([r[1] for r in self.rows()], ['provider_error', 'provider_error'])

    def test_http_502_retries_at_most_twice_and_counts_failed_attempts(self):
        opener = Mock()
        opener.open.side_effect = [HTTPError(transport.BASE_URL + '/search?api_key=fixture-secret-DO-NOT-LOG',
                                             502, 'Bad Gateway', {}, io.BytesIO(b'upstream unavailable'))
                                   for _ in range(2)]
        with patch.dict(os.environ, {'FOODSPARK_MAX_ATTEMPTS': '99'}), \
                patch.object(transport, 'build_opener', return_value=opener):
            self.error('provider_error')
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(transport.status()['attempts_in_database'], 2)

    def test_retry_cannot_exceed_budget_even_after_failed_call(self):
        opener = Mock()
        opener.open.return_value = Response({'response': {'error': {'code': 502}}})
        with patch.dict(os.environ, {'FOODSPARK_READ_BUDGET': '1'}), \
                patch.object(transport, 'build_opener', return_value=opener):
            self.error('budget_exhausted')
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual([r[1] for r in self.rows()], ['provider_error'])

    def test_no_match_and_no_coverage_remain_distinct(self):
        opener = Mock()
        opener.open.side_effect = [
            Response({'request_log': {'status': 200}, 'response': {'message': 'No results found'}}),
            Response({'request_log': {'status': 200}, 'response': {'error': 'Store not found'}}),
        ]
        with patch.object(transport, 'build_opener', return_value=opener):
            self.error('no_match')
            self.error('no_coverage')
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual([r[1] for r in self.rows()], ['no_match', 'no_coverage'])

    def test_no_match_in_error_field_is_not_retried_as_provider_failure(self):
        opener = Mock()
        opener.open.side_effect = lambda *a, **k: Response({
            'request_log': {'status': 200}, 'response': {'error': 'No results found'}})
        with patch.object(transport, 'build_opener', return_value=opener):
            self.error('no_match')
        self.assertEqual(opener.open.call_count, 1)

    def test_timeout_and_connectivity_failure_are_typed_and_bounded(self):
        for exception, code in ((TimeoutError('upstream timeout'), 'provider_timeout'),
                                (URLError(TimeoutError('socket timeout')), 'provider_timeout'),
                                (URLError('DNS lookup failed'), 'provider_unavailable')):
            with self.subTest(code=code, exception=type(exception).__name__):
                opener = Mock()
                opener.open.side_effect = exception
                with patch.dict(os.environ, {'FOODSPARK_MAX_ATTEMPTS': '1'}), \
                        patch.object(transport, 'build_opener', return_value=opener):
                    self.error(code)
                self.assertEqual(opener.open.call_count, 1)
                self.assertEqual(self.rows()[-1][1], code)

    def test_malformed_oversized_or_missing_envelope_never_retries(self):
        responses = [Response(raw=b'<html>not json</html>'),
                     Response(raw=b'x' * 1_000_001), Response([]), Response({'unexpected': True})]
        for response in responses:
            with self.subTest(case=responses.index(response)):
                opener = Mock()
                opener.open.return_value = response
                with patch.object(transport, 'build_opener', return_value=opener):
                    self.error('invalid_response')
                self.assertEqual(opener.open.call_count, 1)

    def test_redirect_handler_does_not_follow_and_transport_does_not_accept_it(self):
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, 302, 'Found', {},
                                                                'https://other.example/'))
        opener = Mock()
        opener.open.return_value = Response(self.success(), status=302)
        with patch.dict(os.environ, {'FOODSPARK_MAX_ATTEMPTS': '1'}), \
                patch.object(transport, 'build_opener', return_value=opener) as build:
            self.error('provider_error')
        self.assertEqual(opener.open.call_count, 1)
        self.assertIsInstance(build.call_args.args[0], transport.NoRedirect)

    def test_sanitized_response_telemetry_and_budget_exclude_request_secrets(self):
        address, query = 'PRIVATE-FIXTURE-ADDRESS', 'PRIVATE-FIXTURE-QUERY'
        raw_url = transport.BASE_URL + '/search?api_key=fixture-secret-DO-NOT-LOG&address=' + address
        payload = {'request_log': {'status': 200, 'process_time': 1.2, 'request_url': raw_url,
                                   'api_key': 'fixture-secret-DO-NOT-LOG'},
                   'response': {'results': []}}
        opener = Mock()
        opener.open.return_value = Response(payload)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(transport, 'build_opener', return_value=opener):
            result = transport.request_json('/search', {'address': address, 'search_term': query})
        self.assertEqual(result['request_log'], {'status': 200, 'process_time': 1.2})
        event = json.loads(output.getvalue())
        self.assertEqual(event['event'], 'xbyte_read')
        self.assertEqual(event['request_id'], 'fixture-safe-request-id')
        self.assertEqual(event['http_status'], 200)
        self.assertGreaterEqual(event['duration_ms'], 0)
        artifact = json.dumps({'response': result, 'log': event, 'calls': self.rows(), 'status': transport.status()})
        for forbidden in ('fixture-secret-DO-NOT-LOG', address, query, raw_url):
            self.assertNotIn(forbidden, artifact)

    def test_exception_messages_and_error_logs_do_not_echo_sensitive_urls(self):
        secret_message = 'fixture-secret-DO-NOT-LOG PRIVATE-FIXTURE-ADDRESS PRIVATE-FIXTURE-QUERY'
        opener = Mock()
        opener.open.return_value = Response({'request_log': {'status': 200}, 'response': {
            'error': {'code': 401, 'message': secret_message}}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(transport, 'build_opener', return_value=opener):
            failure = self.error('provider_auth')
        for forbidden in secret_message.split():
            self.assertNotIn(forbidden, str(failure) + output.getvalue() + json.dumps(self.rows()))


if __name__ == '__main__':
    unittest.main()

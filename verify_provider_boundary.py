#!/usr/bin/env python3
"""Verify the supplier-test API as an external, credential-free HTTP consumer.

This makes 16 requests on a healthy deployment. All successful searches use
explicitly synthetic fixtures. The only live-mode request selects unsupported
Actowiz without an Authorization header to verify the access gate; it cannot
make a vendor call in this contract. No application modules are imported.

Example:
    python verify_provider_boundary.py \
      --base-url https://YOUR-RENDER-SERVICE.onrender.com \
      --expected-version 0.9.0 --output boundary-evidence.json

Requires jsonschema (development/client verification dependency only).
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jsonschema import Draft202012Validator


PROVIDERS = ('xbyte_foodspark', 'northfork', 'actowiz')
SEARCH = '/api/v1/provider-tests/search'
COMPARE = '/api/v1/provider-tests/compare'
MAX_RESPONSE_BYTES = 2_000_000
MAX_REQUESTS = 16
BASE_REQUEST = {
    'provider': 'northfork', 'zip': '63129', 'queries': ['1 gallon whole milk'],
    'retailer': 'schnucks', 'mode': 'fixture', 'fallback_policy': 'none',
}
_SECRET_KEYS = {
    'authorization', 'apikey', 'securitykey', 'accesstoken', 'refreshtoken',
    'password', 'secret', 'credential', 'credentials', 'token', 'cookie',
    'setcookie', 'requestheaders', 'requestlog', 'requesturl',
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _redact(value):
    """Defensive evidence filtering; no credentials are accepted or sent."""
    if isinstance(value, dict):
        return {
            key: '[redacted]' if ''.join(c for c in key.lower() if c.isalnum()) in _SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _schema_errors(value, schema, components=None):
    complete = {**schema, 'components': components or {}}
    try:
        errors = list(Draft202012Validator(complete).iter_errors(value))
    except Exception as error:
        return ['Schema could not be evaluated: ' + type(error).__name__]
    # Report paths and failed constraints, not values that might be sensitive.
    return ['/' + '/'.join(str(p) for p in e.absolute_path) + ': ' + str(e.validator)
            for e in errors[:10]]


def _base_url(raw):
    value = raw.rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError('Use an HTTP(S) service URL without credentials, query parameters or a fragment.')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise argparse.ArgumentTypeError('Use HTTPS outside a loopback development server.')
    return value


class BoundaryVerifier:
    def __init__(self, base_url, expected_version=None, timeout=25):
        self.base_url = base_url
        self.expected_version = expected_version
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())
        self.spec = None
        self.cases = []
        self.request_count = 0
        self.started_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    def request(self, case_name, method, path, payload=None, expected_status=200, request_should_validate=True):
        if self.request_count >= MAX_REQUESTS:
            raise RuntimeError('Verification request allowance exhausted.')
        self.request_count += 1
        expected = [expected_status] if isinstance(expected_status, int) else list(expected_status)
        started = time.monotonic()
        case = {'case': case_name, 'request': {'method': method, 'path': path, 'body': payload},
                'expected_http_status': expected, 'http_status': None, 'body': None,
                'schema_ok': None, 'schema_errors': [], 'checks': {}}
        self.cases.append(case)
        headers = {'Accept': 'application/json', 'User-Agent': 'Alambda-Contract-Verification/1.0'}
        raw_payload = None if payload is None else json.dumps(payload, allow_nan=False).encode('utf-8')
        if raw_payload is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.base_url + path, data=raw_payload, method=method, headers=headers)
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                case['http_status'] = response.status
                # Header allowlist: no authentication, cookie or complete header dumps.
                case['response_headers'] = {
                    key: response.headers.get(key)
                    for key in ('Content-Type', 'Cache-Control', 'X-Request-ID')
                }
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError('response_size_limit')
            case['response_sha256'] = hashlib.sha256(raw).hexdigest()
            body = json.loads(raw)
            case['body'] = body
            case['checks']['json_object'] = isinstance(body, dict)
            case['checks']['http_status'] = case['http_status'] in expected
            case['checks']['no_store'] = 'no-store' in (case['response_headers']['Cache-Control'] or '').lower()
        except (OSError, URLError, ValueError, UnicodeError) as error:
            case['transport_error'] = type(error).__name__
            case['checks']['http_transport_and_json'] = False
            body = None
        case['duration_ms'] = round((time.monotonic() - started) * 1000)
        if self.spec is not None:
            operation = self.spec.get('paths', {}).get(path, {}).get(method.lower(), {})
            response_schema = operation.get('responses', {}).get(str(case['http_status']), {}).get('content', {}).get('application/json', {}).get('schema')
            if response_schema is None:
                case['schema_errors'] = ['No response schema is declared for this HTTP status.']
            else:
                case['schema_errors'] = _schema_errors(body, response_schema, self.spec.get('components'))
            case['schema_ok'] = not case['schema_errors']
            if payload is not None:
                request_schema = operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')
                if request_schema is None:
                    case['request_schema_ok'] = None
                    case['checks']['request_contract'] = False
                else:
                    errors = _schema_errors(payload, request_schema, self.spec.get('components'))
                    case['request_schema_ok'] = not errors
                    case['request_schema_errors'] = errors
                    case['checks']['request_contract'] = bool(not errors) == request_should_validate
        return case

    def _fixture_checks(self, outcome, provider):
        if not isinstance(outcome, dict):
            return {'fixture_object': False}
        items = outcome.get('items')
        if not isinstance(items, list):
            items = []
        raw_expected = {'northfork': 399, 'xbyte_foodspark': '$3.99', 'actowiz': '3.99'}[provider]
        unit_expected = {'northfork': 'minor_currency_units', 'xbyte_foodspark': 'formatted_major_currency',
                         'actowiz': 'major_currency_units'}[provider]
        valid_items = bool(items) and all(isinstance(i, dict) for i in items)
        return {
            'requested_supplier_retained': outcome.get('requested_provider') == provider,
            'no_live_supplier_claim': outcome.get('actual_provider') is None,
            'fixture_mode_explicit': outcome.get('mode') == 'fixture' and outcome.get('operation') == 'schema_fixture',
            'successful_synthetic_sample': outcome.get('status') == 'ok' and outcome.get('error') is None,
            'no_fallback': outcome.get('fallback_used') is False,
            'no_cache': outcome.get('cache_used') is False,
            'no_upstream_requests': type(outcome.get('upstream_requests')) is int and outcome['upstream_requests'] == 0,
            'test_id_not_quote_id': isinstance(outcome.get('test_id'), str) and outcome['test_id'].startswith('test-') and 'search_id' not in outcome,
            'nonempty_items': valid_items,
            'supplier_identity_isolated': valid_items and all(i.get('provider') == provider and str(i.get('id', '')).startswith('test:' + provider + ':') for i in items),
            'synthetic_display_only': valid_items and all(i.get('source_mode') == 'fixture' and i.get('live') is False and i.get('selectable') is False for i in items),
            'no_live_location_claim': valid_items and all(i.get('location_evidence') == 'unknown' and 'synthetic_fixture' in i.get('quality_flags', []) for i in items),
            'price_units_preserved': valid_items and all(type(i.get('raw_price')) is type(raw_expected) and i['raw_price'] == raw_expected and i.get('raw_price_unit') == unit_expected for i in items),
            'canonical_usd_amount': valid_items and all(i.get('price') == 3.99 and i.get('price_decimal') == '3.99' and i.get('price_minor') == 399 and i.get('currency') == 'USD' for i in items),
            'no_transfer_claim': valid_items and all(i.get('transfer_verified') is False for i in items),
        }

    def run(self):
        doc = self.request('served_openapi', 'GET', '/openapi.json')
        document_schema = {
            'type': 'object', 'required': ['openapi', 'info', 'paths', 'components'],
            'properties': {'openapi': {'const': '3.1.0'}, 'info': {'type': 'object', 'required': ['version']},
                           'paths': {'type': 'object'}, 'components': {'type': 'object', 'required': ['schemas']}}
        }
        doc['schema_errors'] = _schema_errors(doc['body'], document_schema)
        doc['schema_ok'] = not doc['schema_errors']
        if not doc['schema_ok'] or doc['http_status'] != 200:
            return self.report()
        self.spec = doc['body']
        version = self.spec['info']['version']
        doc['checks']['required_test_routes'] = all(p in self.spec['paths'] for p in (SEARCH, COMPARE, '/api/v1/provider-tests/status'))
        if self.expected_version is not None:
            doc['checks']['expected_version'] = version == self.expected_version
        if not doc['checks']['required_test_routes']:
            return self.report()

        health = self.request('health_matches_contract', 'GET', '/health')
        health['checks']['version_matches_contract'] = isinstance(health['body'], dict) and health['body'].get('version') == version
        health['checks']['healthy'] = isinstance(health['body'], dict) and health['body'].get('status') == 'ok'
        status = self.request('supplier_capabilities', 'GET', '/api/v1/provider-tests/status')
        status_body = status['body'] if isinstance(status['body'], dict) else {}
        capabilities = status_body.get('providers', [])
        status['checks']['three_supplier_capabilities'] = isinstance(capabilities, list) and {p.get('id') for p in capabilities if isinstance(p, dict)} == set(PROVIDERS)
        status['checks']['no_cache_or_fallback_policy'] = status_body.get('cache_policy') == 'disabled' and status_body.get('fallback_policy') == 'none'

        samples = {}
        for provider in PROVIDERS:
            case = self.request('fixture_' + provider, 'POST', SEARCH, {**BASE_REQUEST, 'provider': provider})
            case['checks'].update(self._fixture_checks(case['body'], provider))
            samples[provider] = case['body']

        compare_request = {k: v for k, v in BASE_REQUEST.items() if k != 'provider'}
        compare_request['providers'] = list(PROVIDERS)
        comparison = self.request('fixture_comparison', 'POST', COMPARE, compare_request)
        combined = comparison['body'] if isinstance(comparison['body'], dict) else {}
        outcomes = combined.get('providers', [])
        outcomes = outcomes if isinstance(outcomes, list) else []
        comparison['checks']['comparison_is_fixture'] = combined.get('mode') == 'fixture' and combined.get('status') == 'ok'
        comparison['checks']['no_fallback_or_cache'] = combined.get('fallback_used') is False and combined.get('cache_used') is False
        comparison['checks']['supplier_order_and_count'] = [p.get('requested_provider') for p in outcomes if isinstance(p, dict)] == list(PROVIDERS)
        for index, provider in enumerate(PROVIDERS):
            outcome = outcomes[index] if index < len(outcomes) else None
            comparison['checks'].update({provider + '.' + key: value for key, value in self._fixture_checks(outcome, provider).items()})

        invalid = (
            ('invalid_supplier', SEARCH, {**BASE_REQUEST, 'provider': 'unknown'}, 'invalid_provider'),
            ('invalid_mode', SEARCH, {**BASE_REQUEST, 'mode': 'auto'}, 'invalid_mode'),
            ('fallback_rejected', SEARCH, {**BASE_REQUEST, 'fallback_policy': 'auto'}, 'fallback_not_allowed'),
            ('invalid_zip', SEARCH, {**BASE_REQUEST, 'zip': 'ABCDE'}, 'invalid_location'),
            ('supplier_options_rejected', SEARCH, {**BASE_REQUEST, 'provider': 'xbyte_foodspark', 'options': {'marketplace': 'instacart'}}, 'invalid_request'),
            ('unselected_supplier_options_rejected', COMPARE, {**compare_request, 'providers': ['northfork'], 'options': {'xbyte_foodspark': {}}}, 'invalid_request'),
        )
        for name, path, payload, error_code in invalid:
            case = self.request(name, 'POST', path, payload, expected_status=422, request_should_validate=False)
            body = case['body'] if isinstance(case['body'], dict) else {}
            case['checks']['explicit_validation_error'] = body.get('error') == error_code
            case['checks']['no_products_returned'] = not body.get('items')

        reference = samples.get('northfork')
        reference = reference if isinstance(reference, dict) else {}
        products = reference.get('items') or []
        reference_available = bool(products) and isinstance(products[0], dict) and bool(reference.get('test_id'))
        test_id = reference.get('test_id', 'test-unavailable-fixture')
        item_id = products[0].get('id') if reference_available else 'test:northfork:unavailable-fixture'
        selection = self.request('test_id_rejected_by_selection', 'POST', '/api/v1/select', {'search_id': test_id, 'item_ids': [item_id]}, expected_status=404)
        selection['checks']['used_actual_fixture_reference'] = reference_available
        selection['checks']['quote_unavailable'] = isinstance(selection['body'], dict) and selection['body'].get('error') == 'search_expired'
        cart = self.request('test_id_rejected_by_cart', 'POST', '/api/v1/cart', {'lines': [{'search_id': test_id, 'item_id': item_id, 'quantity': 1}]}, expected_status=409)
        cart['checks']['used_actual_fixture_reference'] = reference_available
        cart['checks']['quote_unavailable'] = isinstance(cart['body'], dict) and cart['body'].get('error') == 'price_expired'

        # Actowiz has no live adapter: even an unexpectedly open gate cannot
        # send credentials or test data to a vendor through this operation.
        live_enabled = status_body.get('live_tests_enabled') is True
        gate = self.request('live_access_requires_server_token', 'POST', SEARCH,
                            {**BASE_REQUEST, 'provider': 'actowiz', 'mode': 'live'},
                            expected_status=403 if live_enabled else 503)
        gate_body = gate['body'] if isinstance(gate['body'], dict) else {}
        gate['checks']['explicit_access_gate'] = gate_body.get('error') == ('test_token_required' if live_enabled else 'live_tests_disabled')
        gate['checks']['no_supplier_outcome'] = 'test_id' not in gate_body and 'actual_provider' not in gate_body and not gate_body.get('items')
        gate['checks']['no_upstream_attempts'] = gate_body.get('upstream_requests', 0) == 0
        return self.report()

    def report(self):
        failures = []
        for case in self.cases:
            case['passed'] = case['schema_ok'] is True and bool(case['checks']) and all(case['checks'].values())
            if not case['passed']:
                failures.append({'case': case['case'], 'checks': [key for key, passed in case['checks'].items() if not passed],
                                 'schema_errors': case['schema_errors'], 'transport_error': case.get('transport_error')})
        return _redact({
            'verification': 'External HTTP provider-test contract', 'base_url': self.base_url,
            'started_at': self.started_at, 'finished_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'contract_version': self.spec.get('info', {}).get('version') if self.spec else None,
            'expected_version': self.expected_version, 'request_count': self.request_count,
            'credentials_sent': False, 'live_supplier_checks_performed': False,
            'scope': 'Synthetic supplier fixtures, validation, access gate, and isolation from shopping quotes. This does not establish vendor health or live data quality.',
            'passed': not failures and self.request_count == MAX_REQUESTS,
            'passed_cases': sum(case['passed'] for case in self.cases), 'total_cases': len(self.cases),
            'failures': failures, 'cases': self.cases,
        })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', required=True, type=_base_url)
    parser.add_argument('--output', required=True, type=Path, help='JSON evidence path; written with mode 0600.')
    parser.add_argument('--expected-version', help='Require this served API version, e.g. 0.9.0.')
    parser.add_argument('--timeout', type=float, default=25, help='Per-request timeout, 1–60 seconds (default 25).')
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 60:
        parser.error('--timeout must be between 1 and 60 seconds.')
    report = BoundaryVerifier(args.base_url, args.expected_version, args.timeout).run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(report, output, indent=2, ensure_ascii=False, allow_nan=False)
        output.write('\n')
    print(json.dumps({key: report[key] for key in ('passed', 'contract_version', 'request_count', 'passed_cases', 'total_cases', 'failures')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

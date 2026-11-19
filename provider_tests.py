"""Read-only supplier experiments, isolated from catalog, quotes and shopping carts.

Live reads require a dedicated bearer token, an explicit enable flag and separate
persistent per-supplier allowances. Fixtures never make network requests.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from location_context import normalize_location
from provider_test_adapters import ProviderReadError, fetch_provider
from provider_test_normalize import normalize

PROVIDERS = {
    'xbyte_foodspark': {'name': 'X-Byte / FoodSpark', 'key': 'FOODSPARK_API_KEY',
                       'limit': 'XBYTE_TEST_READ_LIMIT', 'live_search_supported': True,
                       'requirements': ['Retailer slug', 'Full street address', 'ZIP code']},
    'northfork': {'name': 'Northfork', 'key': 'NORTHFORK_API_KEY',
                  'limit': 'NORTHFORK_TEST_READ_LIMIT', 'live_search_supported': True,
                  'requirements': ['ZIP code', 'Exact retailer or discovered store', 'Ingredient list']},
    'actowiz': {'name': 'Actowiz', 'key': None, 'limit': None,
                'live_search_supported': False,
                'requirements': ['Keyword search has not been delivered; trial is a known Walmart product lookup']},
}


class TestRequestError(Exception):
    def __init__(self, code, message, http_status=422):
        self.code, self.message, self.http_status = code, message, http_status
        super().__init__(message)


def _limit(provider):
    name = PROVIDERS[provider]['limit']
    try:
        value = int(os.getenv(name, '0')) if name else 0
    except ValueError:
        return 0
    return max(0, min(value, 10000))


class ReadBudget:
    """Count attempted upstream calls atomically; an exhausted allowance never resets implicitly."""
    def __init__(self, path):
        self.path = str(path)

    def used(self, provider):
        if not Path(self.path).exists():
            return 0
        with sqlite3.connect(self.path, timeout=5) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_reads'").fetchone()
            row = db.execute('SELECT used FROM provider_reads WHERE provider=?', (provider,)).fetchone() if exists else None
        return row[0] if row else 0

    def reserve(self, provider):
        limit = _limit(provider)
        if limit <= 0:
            raise ProviderReadError('read_budget_exhausted', 'No live read allowance is configured for this supplier.', 429)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS provider_reads (provider TEXT PRIMARY KEY, used INTEGER NOT NULL)')
            row = db.execute('SELECT used FROM provider_reads WHERE provider=?', (provider,)).fetchone()
            used = row[0] if row else 0
            if used >= limit:
                raise ProviderReadError('read_budget_exhausted', 'This supplier has reached its live read allowance.', 429)
            db.execute('INSERT INTO provider_reads(provider,used) VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET used=excluded.used', (provider, used + 1))


def validate_request(data, comparison=False):
    allowed = {'provider', 'providers', 'zip', 'location', 'queries', 'retailer', 'mode', 'fallback_policy', 'options'}
    if not isinstance(data, dict) or set(data) - allowed:
        raise TestRequestError('invalid_request', 'Use only fields documented for provider tests.')
    if comparison:
        providers = data.get('providers')
        if 'provider' in data or not isinstance(providers, list) or not 1 <= len(providers) <= 3:
            raise TestRequestError('invalid_provider', 'Send providers with one to three distinct supplier IDs.')
    else:
        providers = [data.get('provider')]
        if 'providers' in data:
            raise TestRequestError('invalid_provider', 'Send one provider, or use the comparison route.')
    if any(not isinstance(p, str) or p not in PROVIDERS for p in providers) or len(set(providers)) != len(providers):
        raise TestRequestError('invalid_provider', 'Choose xbyte_foodspark, northfork or actowiz without duplicates.')
    if data.get('mode', 'fixture') not in ('fixture', 'live'):
        raise TestRequestError('invalid_mode', 'Provider tests support only fixture or live mode.')
    if data.get('fallback_policy', 'none') != 'none':
        raise TestRequestError('fallback_not_allowed', 'Independent provider tests require fallback_policy: none.')
    queries = data.get('queries')
    if not isinstance(queries, list) or not 1 <= len(queries) <= 3 or any(not isinstance(q, str) or not q.strip() or len(q) > 120 for q in queries):
        raise TestRequestError('invalid_request', 'Send one to three nonempty queries of up to 120 characters.')
    retailer = data.get('retailer', '')
    if not isinstance(retailer, str) or not retailer.strip() or len(retailer) > 100 or any(ord(c) < 32 for c in retailer):
        raise TestRequestError('invalid_request', 'Supply a retailer slug or exact store name, up to 100 characters.')
    try:
        location = normalize_location(data)
    except (ValueError, TypeError):
        raise TestRequestError('invalid_location', 'Use a five-digit ZIP and, if supplied, a complete US address.') from None
    if not re.fullmatch(r'[0-9]{5}', location['postal_code']):
        raise TestRequestError('invalid_location', 'ZIP must contain five ASCII digits.')
    if location.get('coordinates'):
        raise TestRequestError('invalid_location', 'These adapters accept ZIP and optional street address; coordinates are not supported.')
    options = data.get('options', {})
    if not isinstance(options, dict):
        raise TestRequestError('invalid_request', 'options must be an object.')
    if comparison:
        if set(options) - set(providers) or any(not isinstance(v, dict) for v in options.values()):
            raise TestRequestError('invalid_request', 'Comparison options must be keyed by a selected supplier ID.')
        option_map = {p: options.get(p, {}) for p in providers}
    else:
        option_map = {providers[0]: options}
    for provider, opts in option_map.items():
        supported = {'marketplace', 'store_id', 'in_stock'} if provider == 'northfork' else set()
        if set(opts) - supported:
            raise TestRequestError('invalid_request', 'Unsupported supplier options.')
        if 'marketplace' in opts and opts['marketplace'] not in ('instacart', 'target', 'walmart'):
            raise TestRequestError('invalid_request', 'Unsupported Northfork marketplace.')
        if 'store_id' in opts and (not isinstance(opts['store_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', opts['store_id'])):
            raise TestRequestError('invalid_request', 'store_id must be a returned string identifier.')
        if 'in_stock' in opts and type(opts['in_stock']) is not bool:
            raise TestRequestError('invalid_request', 'in_stock must be boolean.')
    common = {'zip': location['postal_code'], 'location': location, 'queries': list(dict.fromkeys(q.strip() for q in queries)),
              'retailer': retailer.strip(), 'mode': data.get('mode', 'fixture'), 'fallback_policy': 'none'}
    return providers, common, option_map


def fixture(provider):
    """Deliberately synthetic schema specimens, not current offers or keyword matches."""
    store = {'id': 'fixture-store', 'provider_store_id': 'fixture-store', 'retailer': 'fixture-retailer',
             'name': 'Synthetic fixture store', 'zip': '00000', 'observed_zip': '00000',
             'platform': 'instacart', 'location_evidence': 'unknown'}
    if provider == 'northfork':
        payload = {'data': {'store': {'identifier': 'fixture-store', 'provider': 'instacart'}, 'ingredients': [
            {'name': 'Fixture milk', 'products': [{'id': 'fixture-northfork-1', 'external_id': 'fixture-sku-1',
                'name': 'Synthetic whole milk', 'brand': 'Fixture Brand', 'price': 399, 'currency_code': 'USD',
                'size': 128, 'unit_of_size': 'fl oz', 'quantity': 1, 'in_stock': True, 'pdp_url': None}]}]}}
    elif provider == 'xbyte_foodspark':
        payload = {'queries': [{'query': 'Fixture milk', 'response': {
            'store_details': {'Store Name': 'Synthetic fixture store', 'Store Slug': 'fixture-retailer',
                              'Store Location ID': 'fixture-store', 'Delivery Zip Code': '00000'},
            'results': [{'Product Name': 'Synthetic whole milk', 'Brand': 'Fixture Brand',
                         'Price': '$3.99', 'Unit Size': '1 gal', 'Price Per Unit': '$0.03 / fl oz',
                         'Availability': 'inStock'}]}}]}
    else:
        store['platform'] = 'walmart'
        payload = {'item_name': 'Synthetic whole milk', 'brand': 'Fixture Brand', 'price': '3.99',
                   'currency': 'USD', 'size': '1 gal', 'availability': None, 'product_id': 'fixture-walmart-1',
                   'upc': '001234567890', 'store_number': 'fixture-store', 'store_zip': '00000'}
    return {'payload': payload, 'store': store, 'requests': 0}


def metrics(items):
    counts = {'items': len(items), 'named': 0, 'priced': 0, 'sized': 0, 'provider_ids': 0,
              'images': 0, 'stock_known': 0, 'location_acknowledged': 0, 'source_timestamps': 0}
    for item in items:
        for key, condition in {
            'named': bool(item.get('name')), 'priced': item.get('price') is not None,
            'sized': bool(item.get('size')), 'provider_ids': bool(item.get('provider_product_id')),
            'images': bool(item.get('image_url')), 'stock_known': item.get('availability') in ('in_stock', 'out_of_stock'),
            'location_acknowledged': item.get('location_evidence') == 'request_acknowledged',
            'source_timestamps': bool(item.get('captured_at')),
        }.items():
            counts[key] += int(condition)
    return counts


class ProviderTests:
    def __init__(self, budget_path):
        self.budget = ReadBudget(budget_path)
        self._slots = threading.BoundedSemaphore(2)

    def status(self):
        enabled = os.getenv('PROVIDER_TESTS_LIVE_ENABLED', 'false').lower() == 'true'
        token_configured = bool(os.getenv('PROVIDER_TEST_TOKEN', '').strip())
        providers = []
        for provider, info in PROVIDERS.items():
            configured = bool(info['key'] and os.getenv(info['key'], '').strip())
            limit = _limit(provider)
            try:
                used = self.budget.used(provider)
                storage = True
            except sqlite3.Error:
                used, storage = None, False
            state = ('unsupported_operation' if not info['live_search_supported'] else
                     'fixture_only' if not enabled or not token_configured else
                     'not_configured' if not configured else
                     'budget_unavailable' if not storage else
                     'read_budget_exhausted' if used >= limit else 'live_ready')
            providers.append({'id': provider, 'name': info['name'], 'configured': configured,
                              'live_search_supported': info['live_search_supported'], 'status': state,
                              'budget_limit': limit, 'budget_used': used, 'requirements': info['requirements']})
        return {'enabled': True, 'live_tests_enabled': enabled and token_configured,
                'token_configured': token_configured, 'providers': providers,
                'cache_policy': 'disabled', 'fallback_policy': 'none', 'test_api_version': '1.0'}

    def _authorize(self, mode, authorization):
        if mode != 'live':
            return
        token = os.getenv('PROVIDER_TEST_TOKEN', '').strip()
        if os.getenv('PROVIDER_TESTS_LIVE_ENABLED', 'false').lower() != 'true' or not token:
            raise TestRequestError('live_tests_disabled', 'Live tests require explicit server configuration. Fixture mode remains available.', 503)
        if not hmac.compare_digest(str(authorization), 'Bearer ' + token):
            raise TestRequestError('test_token_required', 'A dedicated provider-test bearer token is required.', 403)

    def one(self, provider, request):
        started = time.monotonic()
        used = 0
        mode = request['mode']
        result = {'test_id': 'test-' + uuid.uuid4().hex, 'requested_provider': provider, 'actual_provider': None,
                  'mode': mode, 'operation': 'schema_fixture' if mode == 'fixture' else 'grocery_search',
                  'status': 'error', 'fallback_used': False, 'cache_used': False, 'duration_ms': 0,
                  'upstream_requests': 0, 'items': [], 'error': None, 'warnings': [], 'metrics': metrics([])}

        def reserve():
            nonlocal used
            try:
                self.budget.reserve(provider)
            except (sqlite3.Error, OSError):
                raise ProviderReadError('budget_unavailable', 'The live read allowance could not be checked.', 503) from None
            used += 1

        try:
            if mode == 'fixture':
                response = fixture(provider)
                result['warnings'].append('Synthetic schema sample: not a current offer, query match or vendor availability test.')
                if provider == 'actowiz':
                    result['warnings'].append('Actowiz fixture represents a known-product lookup. Delivered trial does not support keyword search.')
            else:
                response = fetch_provider(provider, request, reserve)
                result['actual_provider'] = provider
            retrieved_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            items = normalize(provider, response['payload'], request, response.get('store', {}), retrieved_at, mode=mode)
            # Independent test IDs never enter the shopping quote store.
            for item in items:
                item['selectable'] = False
            result.update(status='ok' if items else 'empty', items=items, metrics=metrics(items))
            result['warnings'].append('Data observations only. Matching names do not establish equivalent products or price savings.')
        except ProviderReadError as error:
            result['error'] = {'code': error.code, 'message': error.message}
            if getattr(error, 'details', None):
                result['error']['details'] = error.details
            result['http_status'] = error.http_status
        except Exception:
            # No upstream exception text, request URLs, credentials or address data are returned.
            result['error'] = {'code': 'provider_test_failed', 'message': 'The selected supplier test could not be completed.'}
            result['http_status'] = 502
        result['upstream_requests'] = used
        result['duration_ms'] = round((time.monotonic() - started) * 1000)
        if mode == 'live' and used:
            result['actual_provider'] = provider
        return result

    def run(self, data, authorization='', comparison=False):
        providers, common, options = validate_request(data, comparison)
        self._authorize(common['mode'], authorization)
        if not self._slots.acquire(blocking=False):
            raise TestRequestError('test_busy', 'Two provider experiments are already running. Try again shortly.', 429)
        try:
            if not comparison:
                outcome = self.one(providers[0], {**common, 'options': options[providers[0]]})
                return outcome.pop('http_status', 200), outcome
            with ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix='provider-test') as pool:
                futures = [pool.submit(copy_context().run, self.one, p, {**common, 'options': options[p]}) for p in providers]
                outcomes = [future.result() for future in futures]
            for outcome in outcomes:
                outcome.pop('http_status', None)
            successes = sum(p['status'] in ('ok', 'empty') for p in outcomes)
            return 200, {'comparison_id': 'comparison-' + uuid.uuid4().hex, 'mode': common['mode'],
                         'status': 'ok' if successes == len(outcomes) else 'partial' if successes else 'error',
                         'providers': outcomes, 'fallback_used': False, 'cache_used': False}
        finally:
            self._slots.release()

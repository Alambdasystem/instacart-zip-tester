"""Bounded, isolated vendor reads for the provider comparison lab.

This module never retries, falls back, follows redirects, or writes a cart.
The controller owns authorization and budgets; ``reserve`` authorizes each
individual network attempt. Product identifiers and prices remain vendor data.
"""
import json
import math
import os
import re
import socket
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import deadline


REQUEST_TIMEOUT_SECONDS = 30.0
TOTAL_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 1_000_000
_BASES = {
    'xbyte_foodspark': 'https://foodsparkfastapiframework.xbyte.io',
    'northfork': 'https://api.northfork.se',
}
_PATHS = {
    'xbyte_foodspark': {'/search'},
    'northfork': {'/api/stores', '/api/smart-cart/ingredients/resolve'},
}
_KEY_NAMES = {
    'xbyte_foodspark': 'FOODSPARK_API_KEY',
    'northfork': 'NORTHFORK_API_KEY',
}
_PRIVATE_FIELDS = {
    'apikey', 'authorization', 'xcustomauth', 'accesstoken', 'refreshtoken',
    'token', 'password', 'secret', 'credential', 'credentials', 'headers',
    'requestheaders', 'requestlog', 'requesturl', 'cookie', 'setcookie',
}
_CREDENTIAL_ASSIGNMENT = re.compile(
    r'(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|'
    r'authorization|x[-_]?custom[-_]?auth|password|secret|'
    r'x-amz-signature|x-goog-signature)\s*[:=]\s*)'
    r'(?:(?:Bearer|Basic)\s+)?[^\s&\"\'<>]+'
)
_FAILURE_MESSAGE = re.compile(
    r'\b(error|failed|failure|forbidden|unauthori[sz]ed|captcha|timeout|'
    r'timed out|no more results|store not found|response issues|proxy|'
    r'quota|exceeded|invalid)\b', re.I
)
_NORTHFORK_STORE_FIELDS = (
    'identifier', 'name', 'provider', 'retailer_key', 'zip', 'country',
    'city', 'state', 'address', 'fulfillment_type',
)
_XBYTE_STORE_FIELDS = (
    'Store Name', 'Store Slug', 'Store Type', 'Store Location ID', 'Shop ID',
    'Zone ID', 'Delivery Address', 'Delivery Zip Code',
    'Delivery Latitude', 'Delivery Longitude',
)


class ProviderReadError(Exception):
    def __init__(self, code, message, http_status=502, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _normalized(value):
    return ' '.join(value.split()).casefold() if isinstance(value, str) else ''


def _field_name(value):
    return re.sub(r'[^a-z0-9]', '', str(value).casefold())


def _safe_string(value, secrets):
    for secret in secrets:
        for encoded in (secret, quote(secret, safe=''), quote_plus(secret)):
            if encoded:
                value = value.replace(encoded, '[redacted]')
    return _CREDENTIAL_ASSIGNMENT.sub(lambda m: m.group(1) + '[redacted]', value)


def _sanitize(value, secrets, depth=0):
    if depth > 20:
        return '[omitted]'
    if isinstance(value, dict):
        return {
            _safe_string(str(key), secrets): _sanitize(item, secrets, depth + 1)
            for key, item in value.items()
            if _field_name(key) not in _PRIVATE_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize(item, secrets, depth + 1) for item in value]
    if isinstance(value, str):
        return _safe_string(value, secrets)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _configured_key(provider):
    key = os.environ.get(_KEY_NAMES[provider], '').strip()
    if not key or '\n' in key or '\r' in key:
        raise ProviderReadError('not_configured', 'Selected provider access is not configured.', 503)
    return key


def _status_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r'\d{3}', value.strip()):
        return int(value)
    return None


def _upstream_error(status=None):
    details = {'upstream_http_status': status} if isinstance(status, int) else {}
    if status in (401, 403):
        return ProviderReadError('provider_authentication_failed', 'Selected provider rejected its configured access.', 502, details)
    if status in (402, 429):
        return ProviderReadError('provider_quota', 'Selected provider rejected the request because of usage limits.', 429, details)
    if status is not None and 300 <= status < 400:
        return ProviderReadError('provider_redirect', 'Selected provider returned a redirect; it was not followed.', 502, details)
    return ProviderReadError('provider_error', 'Selected provider returned an error.', 502, details)


def _check_envelope(payload, provider):
    """Inspect vendor envelopes, never treating HTTP 200 as sufficient."""
    pending = [payload]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        for key in ('status', 'status_code', 'statusCode', 'code', 'error_code'):
            status = _status_number(node.get(key))
            if status is not None and status >= 300:
                raise _upstream_error(status)
        if node.get('success') is False or _normalized(node.get('status')) in ('error', 'failed', 'failure'):
            raise _upstream_error()
        for key in ('error', 'errors'):
            if node.get(key):
                nested = node[key]
                status = None
                entries = nested if isinstance(nested, list) else [nested]
                for entry in entries:
                    if isinstance(entry, dict):
                        for field in ('status', 'status_code', 'statusCode', 'code', 'error_code'):
                            status = _status_number(entry.get(field))
                            if status is not None:
                                break
                    if status is not None:
                        break
                raise _upstream_error(status)
        message = node.get('message')
        if provider == 'xbyte_foodspark' and isinstance(message, str) and _FAILURE_MESSAGE.search(message):
            raise _upstream_error()
        for key in ('meta', 'response', 'request_log', 'message'):
            if isinstance(node.get(key), dict):
                pending.append(node[key])


class _Transport:
    def __init__(self, provider, key, reserve):
        self.provider = provider
        self.key = key
        self.reserve = reserve
        self.requests = 0
        self.end = time.monotonic() + min(TOTAL_TIMEOUT_SECONDS, deadline.remaining())
        self.secrets = tuple({key, *(os.environ.get(name, '').strip() for name in _KEY_NAMES.values())} - {''})

    def _remaining(self, end):
        remaining = min(min(end, self.end) - time.monotonic(), deadline.remaining())
        if remaining <= 0:
            raise ProviderReadError('provider_timeout', 'Selected provider exceeded the read time budget.', 504)
        return remaining

    def _read(self, response, end):
        chunks, total = [], 0
        # read1 returns available bytes rather than waiting to fill a large
        # buffer. Reset the socket timeout to the remaining total read budget.
        reader = getattr(response, 'read1', None) or response.read
        while True:
            remaining = self._remaining(end)
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = reader(min(65536, MAX_RESPONSE_BYTES + 1 - total))
            self._remaining(end)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ProviderReadError('invalid_response', 'Selected provider returned an invalid response.', 502)
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ProviderReadError('response_too_large', 'Selected provider response exceeded the size limit.', 502)
        return b''.join(chunks)

    def request(self, path, params=None, body=None):
        if path not in _PATHS[self.provider]:
            raise ProviderReadError('unsupported_operation', 'This provider operation is not supported.', 422)
        url = _BASES[self.provider] + path
        query = dict(params or {})
        headers = {'Accept': 'application/json'}
        if self.provider == 'xbyte_foodspark':
            query['api_key'] = self.key
        else:
            headers['X-Custom-Auth'] = self.key
        if query:
            url += '?' + urlencode(query)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != urlsplit(_BASES[self.provider]).netloc:
            raise ProviderReadError('unsupported_operation', 'Provider destination is not supported.', 422)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(url, data=data, headers=headers, method='POST' if body is not None else 'GET')
        opener = build_opener(NoRedirect())
        end = min(self.end, time.monotonic() + REQUEST_TIMEOUT_SECONDS)
        timeout = self._remaining(end)
        # Controller exceptions deliberately propagate unchanged. Nothing can
        # contact the provider before this reservation succeeds.
        self.reserve()
        self.requests += 1
        try:
            with opener.open(request, timeout=timeout) as response:
                status = _status_number(getattr(response, 'status', None))
                if status is None or not 200 <= status < 300:
                    raise _upstream_error(status)
                raw = self._read(response, end)
            payload = json.loads(raw, parse_constant=lambda _: None)
        except HTTPError as error:
            status = _status_number(error.code)
            error.close()
            raise _upstream_error(status) from None
        except (TimeoutError, socket.timeout):
            raise ProviderReadError('provider_timeout', 'Selected provider timed out.', 504) from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ProviderReadError('provider_timeout', 'Selected provider timed out.', 504) from None
            raise ProviderReadError('provider_unavailable', 'Selected provider could not be reached.', 502) from None
        except (OSError, HTTPException, ValueError, UnicodeError, RecursionError):
            raise ProviderReadError('invalid_response', 'Selected provider did not return a valid JSON response.', 502) from None
        if not isinstance(payload, dict):
            raise ProviderReadError('invalid_response', 'Selected provider returned an unexpected response structure.', 502)
        _check_envelope(payload, self.provider)
        return payload


def _safe_store(store, fields, zip_code, transport):
    result = {key: store[key] for key in fields if key in store}
    result['requested_zip'] = zip_code
    return _sanitize(result, transport.secrets)


def _fetch_xbyte(request, transport):
    retailer = request.get('retailer')
    if not isinstance(retailer, str) or not retailer.strip():
        raise ProviderReadError('retailer_required', 'FoodSpark requires an exact retailer slug.', 422)
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', retailer):
        raise ProviderReadError('invalid_retailer', 'FoodSpark requires a valid lowercase retailer slug.', 422)
    location = request.get('location') or {}
    address = location.get('address') if isinstance(location, dict) else None
    required = ('line1', 'city', 'region')
    if not isinstance(address, dict) or any(not isinstance(address.get(k), str) or not address[k].strip() for k in required):
        raise ProviderReadError('location_required', 'FoodSpark requires a full street address, city, region, and ZIP.', 422)
    zip_code = request['zip']
    if location.get('postal_code', zip_code) != zip_code:
        raise ProviderReadError('location_mismatch', 'Requested ZIP and address context do not agree.', 422)
    parts = [address['line1'].strip()]
    if address.get('line2'):
        parts.append(address['line2'].strip())
    parts.extend((address['city'].strip(), address['region'].strip() + ' ' + zip_code))
    full_address = ', '.join(parts)
    street_address = ', '.join(parts[:-2])
    expected_addresses = {_normalized(street_address), _normalized(full_address)}
    results, selected_store, store_identity = [], None, None
    for query in request['queries']:
        payload = transport.request('/search', {
            'country': 'usa', 'platform': 'web', 'domain': 'instacart',
            'endpoint_type': 'dl295_search_by_addr_brand_slug_and_keyword',
            'brand_slug': retailer, 'search_term': query,
            'zipcode': zip_code, 'address': full_address,
        })
        response = payload.get('response')
        if not isinstance(response, dict) or not isinstance(response.get('store_details'), dict) or not isinstance(response.get('results'), list):
            raise ProviderReadError('invalid_response', 'FoodSpark did not return store details and a product list.', 502)
        store = response['store_details']
        if (_normalized(store.get('Store Slug')) != _normalized(retailer)
                or str(store.get('Delivery Zip Code', '')) != zip_code
                or _normalized(store.get('Delivery Address')) not in expected_addresses):
            raise ProviderReadError('location_mismatch', 'FoodSpark did not confirm the requested retailer, street address, and ZIP.', 502)
        identity = tuple(store.get(key) for key in ('Store Slug', 'Store Location ID', 'Shop ID', 'Zone ID'))
        if store_identity is not None and identity != store_identity:
            raise ProviderReadError('location_mismatch', 'FoodSpark returned different store contexts for the requested queries.', 502)
        if any(not isinstance(row, dict) for row in response['results']):
            raise ProviderReadError('invalid_response', 'FoodSpark returned an invalid product list.', 502)
        for row in response['results']:
            if row.get('error') or row.get('errors'):
                raise _upstream_error()
        store_identity = identity
        selected_store = _safe_store(store, _XBYTE_STORE_FIELDS, zip_code, transport)
        results.append({'query': _sanitize(query, transport.secrets), 'response': _sanitize(response, transport.secrets)})
    return {'payload': {'queries': results}, 'store': selected_store, 'requests': transport.requests}


def _fetch_northfork(request, transport):
    options = request.get('options') or {}
    marketplace = options.get('marketplace', 'instacart')
    if marketplace not in ('instacart', 'target', 'walmart'):
        raise ProviderReadError('unsupported_operation', 'Northfork marketplace must be instacart, target, or walmart.', 422)
    retailer = request.get('retailer')
    if not isinstance(retailer, str) or not retailer.strip():
        raise ProviderReadError('retailer_required', 'Northfork requires an exact returned retailer key or store name.', 422)
    in_stock = options.get('in_stock', False)
    if not isinstance(in_stock, bool):
        raise ProviderReadError('invalid_request', 'Northfork in_stock must be a boolean.', 422)
    payload = transport.request('/api/stores', {'providers': marketplace, 'zip': request['zip']})
    stores = payload.get('data')
    if not isinstance(stores, list) or any(not isinstance(store, dict) for store in stores):
        raise ProviderReadError('invalid_response', 'Northfork did not return a store list.', 502)
    available = [store for store in stores if store.get('provider') == marketplace
                 and isinstance(store.get('identifier'), (str, int))
                 and not isinstance(store.get('identifier'), bool)
                 and str(store['identifier']).strip()
                 and isinstance(store.get('name'), str) and store['name'].strip()]
    matches = [store for store in available if _normalized(retailer) in
               (_normalized(store.get('retailer_key')), _normalized(store.get('name')))]
    if options.get('store_id') is not None:
        matches = [store for store in matches if str(store['identifier']) == str(options['store_id'])]
    choices = matches if matches else available
    if len(matches) != 1:
        details = {
            'candidates': [_safe_store(store, _NORTHFORK_STORE_FIELDS, request['zip'], transport) for store in choices[:50]],
            'candidate_count': len(choices),
        }
        if not matches:
            raise ProviderReadError('store_not_found', 'The requested Northfork store was not returned for this ZIP and marketplace.', 404, details)
        raise ProviderReadError('ambiguous_store', 'Multiple Northfork stores match; choose a returned store_id.', 409, details)
    store = matches[0]
    safe_store = _safe_store(store, _NORTHFORK_STORE_FIELDS, request['zip'], transport)
    payload = transport.request('/api/smart-cart/ingredients/resolve', body={
        'store': {key: store[key] for key in ('provider', 'identifier', 'name')},
        'ingredients': [{'name': query} for query in request['queries']],
        'options': {'in_stock': in_stock, 'substitute_count': 1},
    })
    data = payload.get('data')
    resolved_store = data.get('store') if isinstance(data, dict) else None
    if not isinstance(resolved_store, dict) or not isinstance(data.get('ingredients'), list):
        raise ProviderReadError('invalid_response', 'Northfork did not return a resolved store and ingredient list.', 502)
    if (resolved_store.get('provider') != marketplace
            or str(resolved_store.get('identifier')) != str(store['identifier'])):
        raise ProviderReadError('location_mismatch', 'Northfork returned products for a different store.', 502)
    for ingredient in data['ingredients']:
        if not isinstance(ingredient, dict) or not isinstance(ingredient.get('products'), list) or any(not isinstance(product, dict) for product in ingredient['products']):
            raise ProviderReadError('invalid_response', 'Northfork returned an invalid ingredient product list.', 502)
    return {'payload': _sanitize(payload, transport.secrets), 'store': safe_store, 'requests': transport.requests}


def fetch_provider(provider, request, reserve):
    """Read exactly one chosen vendor using an already-validated request.

    ``request.options`` contains only the selected provider's options. ``reserve``
    is called immediately before every network attempt and may raise to deny it.
    The successful result contains sanitized vendor data, store context, and
    the number of network attempts. No cart or authentication flow is invoked.
    """
    if provider == 'actowiz':
        raise ProviderReadError('unsupported_operation', 'Actowiz keyword search has not been supplied and its HTTPS access is not verified.', 422)
    if provider not in _BASES:
        raise ProviderReadError('unsupported_operation', 'Selected data provider is not supported.', 422)
    key = _configured_key(provider)
    transport = _Transport(provider, key, reserve)
    if provider == 'xbyte_foodspark':
        return _fetch_xbyte(request, transport)
    return _fetch_northfork(request, transport)

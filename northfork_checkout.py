"""Northfork retailer discovery, product resolution and cart handoff.

Northfork returns the retailers and stores enabled for a requested ZIP. X-Byte
product selections are re-resolved for the chosen Northfork store, and the
returned retailer-specific URL is opened by the app. This module never follows
the 302 and never places an order.
"""
import json
import os
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import deadline

BASE = 'https://api.northfork.se'
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 30


class CheckoutError(Exception):
    def __init__(self, code, message, http_status=502, details=None):
        self.code, self.message, self.http_status = code, message, http_status
        self.details = details or {}
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _key():
    key = os.environ.get('NORTHFORK_API_KEY', '').strip()
    if not key or '\n' in key or '\r' in key:
        raise CheckoutError('checkout_not_configured', 'Northfork checkout access is not configured.', 503)
    return key


class NorthforkClient:
    """Small allowlisted HTTPS client; redirects are never followed."""
    def __init__(self, api_key=None):
        self.api_key = api_key or _key()

    def _request(self, path, params=None, body=None, allow_cart_redirect=False):
        if path not in ('/api/stores', '/api/smart-cart/ingredients/resolve', '/api/carts/push'):
            raise CheckoutError('unsupported_operation', 'Northfork operation is not supported.', 422)
        if path == '/api/carts/push' and not allow_cart_redirect:
            raise CheckoutError('unsupported_operation', 'Cart handoff is not allowed for this request.', 422)
        url = BASE + path
        if params:
            url += '?' + urlencode(params)
        headers = {'Accept': 'application/json', 'X-Custom-Auth': self.api_key}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        method = 'POST' if body is not None else 'GET'
        request = Request(url, data=data, headers=headers, method=method)
        remaining = min(REQUEST_TIMEOUT_SECONDS, deadline.remaining())
        if remaining <= 0:
            raise CheckoutError('provider_timeout', 'Northfork request exceeded its time budget.', 504)
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=remaining) as response:
                status = getattr(response, 'status', None)
                if path == '/api/carts/push':
                    raise CheckoutError('invalid_handoff_response', 'Northfork cart handoff returned an unexpected response.', 502)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise CheckoutError('provider_response_too_large', 'Northfork response exceeded the size limit.', 502)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise CheckoutError('northfork_error', 'Northfork rejected the checkout request.', 502)
            try:
                payload = json.loads(raw, parse_constant=lambda _: None)
            except (ValueError, TypeError):
                raise CheckoutError('invalid_provider_response', 'Northfork returned invalid JSON.', 502) from None
            if not isinstance(payload, dict):
                raise CheckoutError('invalid_provider_response', 'Northfork returned an invalid response.', 502)
            return payload
        except HTTPError as error:
            status = error.code
            location = error.headers.get('Location') if error.headers else None
            error.close()
            if path == '/api/carts/push' and status in (301, 302, 303, 307, 308):
                return {'handoff_status': status, 'location': location}
            if status in (401, 403):
                raise CheckoutError('provider_authentication_failed', 'Northfork rejected its configured access.', 502) from None
            if status in (402, 429):
                raise CheckoutError('provider_quota', 'Northfork rejected the request because of usage limits.', 429) from None
            raise CheckoutError('northfork_error', 'Northfork rejected the checkout request.', 502, {'upstream_http_status': status}) from None
        except (TimeoutError, socket.timeout):
            raise CheckoutError('provider_timeout', 'Northfork timed out during checkout.', 504) from None
        except URLError:
            raise CheckoutError('provider_unavailable', 'Northfork could not be reached.', 502) from None

    def stores(self, zip_code):
        # Omitting `providers` asks Northfork for the full retailer set enabled
        # for this account, rather than constraining discovery to one marketplace.
        return self._request('/api/stores', {'zip': zip_code})

    def resolve(self, store, ingredients):
        return self._request('/api/smart-cart/ingredients/resolve', body={
            'store': {key: store[key] for key in ('provider', 'identifier', 'name')},
            'ingredients': ingredients,
            'options': {'in_stock': True, 'substitute_count': 2},
        })

    def push(self, store, zip_code, product_quantities):
        params = {
            'partner': 'remind-me-app',
            'storeProvider': store['provider'],
            'storeId': str(store['identifier']),
            'retailerKey': store['retailer_key'],
            'zip': zip_code,
            'extraProductIds': ','.join(f'{product_id}_{quantity}' for product_id, quantity in product_quantities),
        }
        return self._request('/api/carts/push', params, allow_cart_redirect=True)


def safe_store(store, zip_code):
    allowed = ('identifier', 'name', 'provider', 'retailer_key', 'zip', 'country', 'city', 'state', 'address', 'fulfillment_type')
    result = {key: store[key] for key in allowed if key in store}
    if result.get('identifier') is not None:
        result['identifier'] = str(result['identifier'])
    result['zip'] = str(result.get('zip') or zip_code)
    return result


def retailer_handoff_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


class CheckoutSessions:
    """Short-lived server-held mappings from X-Byte selections to Northfork IDs."""
    TTL_SECONDS = 300

    def __init__(self, client_factory=NorthforkClient, clock=time.time):
        self.client_factory, self.clock = client_factory, clock
        self.sessions = {}
        self._lock = threading.RLock()

    @staticmethod
    def _text(value):
        return ' '.join(value.split()) if isinstance(value, str) else ''

    def discover_stores(self, zip_code):
        payload = self.client_factory().stores(zip_code)
        data = payload.get('data')
        if not isinstance(data, list):
            raise CheckoutError('invalid_provider_response', 'Northfork did not return a store list.', 502)
        stores = []
        for store in data:
            if (isinstance(store, dict) and self._text(store.get('provider'))
                    and str(store.get('identifier', '')).strip()
                    and self._text(store.get('name'))
                    and self._text(store.get('retailer_key'))):
                stores.append(safe_store(store, zip_code))
        return {'stores': stores, 'zip': zip_code, 'source_provider': 'northfork',
                'purpose': 'checkout_store_candidates', 'retailer_scope': 'all_enabled_northfork_retailers',
                'store_branch_verified': False}

    def resolve(self, search_results, request):
        client = self.client_factory()
        zip_code = search_results.get('location', {}).get('requested_zip')
        if not isinstance(zip_code, str) or len(zip_code) != 5 or not zip_code.isdigit():
            raise CheckoutError('invalid_location', 'The X-Byte search has no usable ZIP code.', 422)
        lines = request.get('lines')
        store_id = request.get('store_id')
        store_provider = self._text(request.get('store_provider'))
        if not isinstance(lines, list) or not 1 <= len(lines) <= 60:
            raise CheckoutError('invalid_request', 'Send 1–60 selected product lines.', 422)
        if not isinstance(store_id, str) or not store_id.strip() or not store_provider:
            raise CheckoutError('invalid_request', 'Choose a store_id and store_provider returned by checkout store discovery.', 422)
        products = {item.get('id'): item for item in search_results.get('items', []) if isinstance(item, dict)}
        chosen, seen = [], set()
        retailer = None
        for line in lines:
            if not isinstance(line, dict) or set(line) - {'item_id', 'quantity'}:
                raise CheckoutError('invalid_request', 'Each line must contain item_id and optional quantity.', 422)
            item_id = line.get('item_id')
            quantity = line.get('quantity', 1)
            if not isinstance(item_id, str) or item_id not in products or type(quantity) is not int or not 1 <= quantity <= 99 or item_id in seen:
                raise CheckoutError('invalid_selection', 'A selected X-Byte product or quantity is invalid.', 422)
            item = products[item_id]
            if item.get('source_provider') != 'xbyte_foodspark':
                raise CheckoutError('xbyte_source_required', 'Northfork checkout requires products returned by X-Byte grocery search.', 409)
            item_retailer = self._text(item.get('retailer'))
            if not item_retailer or (retailer is not None and retailer.casefold() != item_retailer.casefold()):
                raise CheckoutError('mixed_retailers', 'Select X-Byte products from one retailer for checkout.', 409)
            retailer = item_retailer
            if not self._text(item.get('name')):
                raise CheckoutError('invalid_selection', 'A selected X-Byte product has no usable product name.', 422)
            chosen.append({'item_id': item_id, 'quantity': quantity, 'item': item})
            seen.add(item_id)

        store_payload = client.stores(zip_code)
        store_rows = store_payload.get('data')
        if not isinstance(store_rows, list):
            raise CheckoutError('invalid_provider_response', 'Northfork did not return a store list.', 502)
        matching = [s for s in store_rows if isinstance(s, dict)
                    and self._text(s.get('provider')).casefold() == store_provider.casefold()
                    and str(s.get('identifier')) == store_id
                    and self._text(s.get('retailer_key')).casefold() == retailer.casefold()]
        if len(matching) != 1:
            raise CheckoutError('store_not_found', 'That Northfork store does not match the selected X-Byte retailer and ZIP.', 404,
                                {'candidate_count': sum(1 for s in store_rows if isinstance(s, dict) and self._text(s.get('provider')).casefold() == store_provider.casefold() and self._text(s.get('retailer_key')).casefold() == retailer.casefold())})
        store = matching[0]
        ingredients = []
        for line in chosen:
            item = line['item']
            name = ' '.join(x for x in (self._text(item.get('brand')), self._text(item.get('name')), self._text(item.get('size'))) if x)
            ingredients.append({'name': name})
        response = client.resolve(store, ingredients)
        resolved = response.get('data')
        if not isinstance(resolved, dict) or not isinstance(resolved.get('ingredients'), list):
            raise CheckoutError('invalid_provider_response', 'Northfork did not return resolved checkout products.', 502)
        resolved_store = resolved.get('store')
        if (not isinstance(resolved_store, dict)
                or self._text(resolved_store.get('provider')).casefold() != store_provider.casefold()
                or str(resolved_store.get('identifier')) != store_id):
            raise CheckoutError('store_mismatch', 'Northfork resolved products for a different store.', 502)
        if len(resolved['ingredients']) != len(chosen):
            raise CheckoutError('incomplete_resolution', 'Northfork did not resolve every selected product.', 409)

        resolved_lines = []
        for selected, ingredient in zip(chosen, resolved['ingredients']):
            if not isinstance(ingredient, dict) or not isinstance(ingredient.get('products'), list):
                raise CheckoutError('invalid_provider_response', 'Northfork returned an invalid product candidate list.', 502)
            candidates = []
            for product in ingredient['products'][:20]:
                if not isinstance(product, dict):
                    continue
                product_id = product.get('external_id') or product.get('id')
                if not isinstance(product_id, (str, int)) or isinstance(product_id, bool) or not str(product_id).strip():
                    continue
                candidates.append({key: product[key] for key in ('id', 'external_id', 'name', 'brand', 'price', 'currency_code', 'size', 'unit_of_size', 'quantity', 'in_stock', 'pdp_url', 'image_url') if key in product})
            resolved_lines.append({'item_id': selected['item_id'], 'source_product': {
                'name': selected['item'].get('name'), 'brand': selected['item'].get('brand'), 'size': selected['item'].get('size'),
                'source_provider': 'xbyte_foodspark'}, 'quantity': selected['quantity'], 'candidates': candidates,
                'recommended_product_id': (candidates[0].get('external_id') or candidates[0].get('id')) if candidates else None})

        checkout_id = 'co-' + __import__('uuid').uuid4().hex
        with self._lock:
            self.sessions[checkout_id] = {'created': self.clock(), 'zip': zip_code, 'store': safe_store(store, zip_code),
                'lines': resolved_lines,
                'products': {str(p.get('external_id') or p.get('id')): p for row in resolved['ingredients'] for p in row.get('products', []) if isinstance(p, dict) and (p.get('external_id') or p.get('id'))},
                'status': 'resolved'}
        return {'checkout_id': checkout_id, 'status': 'products_resolved', 'store': safe_store(store, zip_code),
                'lines': resolved_lines, 'expires_in': self.TTL_SECONDS, 'cart_updated': False}

    def handoff(self, checkout_id, choices):
        with self._lock:
            session = self.sessions.get(checkout_id) if isinstance(checkout_id, str) else None
            if not session or self.clock() - session['created'] >= self.TTL_SECONDS:
                self.sessions.pop(checkout_id, None)
                raise CheckoutError('checkout_expired', 'Resolve the selected products again; checkout selections expire after five minutes.', 409)
            if session['status'] != 'resolved':
                raise CheckoutError('handoff_already_started', 'This checkout handoff has already been started.', 409)
        if not isinstance(choices, list) or len(choices) != len(session['lines']):
            raise CheckoutError('invalid_selection', 'Choose one Northfork product candidate for each resolved line.', 422)
        expected = {line['item_id']: line for line in session['lines']}
        pairs, seen = [], set()
        for choice in choices:
            if not isinstance(choice, dict) or set(choice) - {'item_id', 'product_id', 'quantity'}:
                raise CheckoutError('invalid_selection', 'Each handoff item must contain item_id, product_id and optional quantity.', 422)
            item_id, product_id = choice.get('item_id'), choice.get('product_id')
            if not isinstance(item_id, str) or item_id not in expected or item_id in seen or not isinstance(product_id, (str, int)) or isinstance(product_id, bool):
                raise CheckoutError('invalid_selection', 'A selected Northfork product is invalid.', 422)
            quantity = choice.get('quantity', expected[item_id]['quantity'])
            if type(quantity) is not int or not 1 <= quantity <= 99:
                raise CheckoutError('invalid_selection', 'Quantity must be an integer from 1 to 99.', 422)
            product_id = str(product_id)
            if product_id not in session['products']:
                raise CheckoutError('invalid_selection', 'The Northfork product was not returned for this checkout.', 422)
            pairs.append((product_id, quantity))
            seen.add(item_id)
        if len(seen) != len(expected):
            raise CheckoutError('invalid_selection', 'Choose a Northfork product for every selected product.', 422)
        with self._lock:
            if session['status'] != 'resolved':
                raise CheckoutError('handoff_already_started', 'This checkout handoff has already been started.', 409)
            session['status'] = 'handoff_started'
        result = self.client_factory().push(session['store'], session['zip'], pairs)
        if result.get('handoff_status') not in (301, 302, 303, 307, 308) or not retailer_handoff_url(result.get('location')):
            session['status'] = 'handoff_failed'
            raise CheckoutError('handoff_failed', 'Northfork did not return a valid retailer cart handoff URL.', 502)
        return {'checkout_id': checkout_id, 'status': 'handoff_ready', 'provider': 'northfork',
                'retailer_provider': session['store']['provider'], 'retailer_key': session['store']['retailer_key'],
                'store': session['store'], 'item_count': len(pairs),
                'handoff_url': result['location'], 'redirect_status': result['handoff_status'],
                'handoff_started': True, 'cart_updated': False, 'order_placed': False,
                'instructions': 'Open handoff_url to continue in the selected retailer’s checkout. Verify transferred products and complete checkout there.'}

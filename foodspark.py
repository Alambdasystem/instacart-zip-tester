"""FoodSpark observations through the existing grocery interface.

Observation IDs are not SKUs/UPCs or evidence of equivalence. Provider capture
freshness is always separate from the lifetime of a local cache entry.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
import time
from urllib.parse import parse_qs, urlsplit

from connector import ConnectorError, matches_query
from location_context import current_location
from provider_session import request_context_id
import xbyte_transport as transport


def selected(zip_code, retailer):
    return bool(transport.enabled() and os.getenv('FOODSPARK_API_KEY')
                and retailer and current_location(zip_code).get('address'))


def cache_scope(zip_code, retailer):
    if not selected(zip_code, retailer):
        return 'browser:xbyte01'
    digest = hashlib.sha256(os.environ['FOODSPARK_API_KEY'].encode()).hexdigest()[:16]
    return 'foodspark:xbyte01:' + digest


def _params(location, retailer):
    address = location.get('address')
    if not address:
        raise ConnectorError('location_required', 'X-Byte grocery demo requires a street address and retailer slug.')
    return {'country': 'usa', 'platform': 'web', 'domain': 'instacart',
            'brand_slug': retailer, 'address': ', '.join(filter(None, (
                address['line1'], address.get('line2'), address['city'],
                address['region'] + ' ' + location['postal_code'])))}


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def source_freshness(item, now=None):
    now = time.time() if now is None else now
    captured, expires = _timestamp(item.get('captured_at')), _timestamp(item.get('source_expires_at'))
    if expires is not None and now >= expires:
        return 'stale'
    if captured is None or expires is None or captured > now or expires <= captured:
        return 'unknown'
    return 'fresh'


def refresh_evidence(item, now=None):
    """Re-evaluate source expiry without renewing its capture timestamp."""
    if item.get('data_provider') != 'xbyte_foodspark' and item.get('source') != 'foodspark':
        return item
    item['data_provider'] = 'xbyte_foodspark'
    freshness = source_freshness(item, now)
    item['source_freshness'] = freshness
    cache_left = item.get('cache_expires_in', item.get('price_expires_in'))
    item['price_fresh'] = freshness == 'fresh' and (cache_left is None or cache_left > 0)
    if cache_left is not None:
        item['cache_expires_in'] = cache_left
        item['price_expires_in'] = min(cache_left, max(0, int((_timestamp(item.get('source_expires_at')) or 0) - (time.time() if now is None else now)))) if item['price_fresh'] else 0
    item['price_status'] = 'stale_snapshot' if freshness == 'stale' else 'snapshot' if freshness == 'fresh' else 'capture_time_unknown'
    item['selectable'] = bool(item['price_fresh'] and item.get('price_kind') == 'exact'
                              and item.get('price') is not None and item.get('currency') == 'USD'
                              and item.get('price_basis_verified') and item.get('price_basis') == 'per_item')
    item['local_offer_eligible'] = bool(item['selectable'] and item.get('location_verified', True) and item.get('context_compatible', True))
    item['discovery_only'] = not item['local_offer_eligible']
    return item


def _money(label):
    if not isinstance(label, str):
        return None, 'unknown', 'unspecified'
    match = re.fullmatch(r'\s*\$([0-9]+(?:\.[0-9]{1,2})?)\s*(.*?)\s*', label)
    if not match:
        start = re.fullmatch(r'\s*(?:starting at|from)\s*\$([0-9]+(?:\.[0-9]{1,2})?)\s*', label, re.I)
        return (float(Decimal(start[1])), 'starting_at', 'unspecified') if start and Decimal(start[1]) <= 1000000 else (None, 'unknown', 'unspecified')
    if Decimal(match[1]) > 1000000:
        return None, 'unknown', 'unspecified'
    suffix = match[2].casefold()
    if re.search(r'est\.?|estimated|approx', suffix):
        kind = 'estimated'
    elif not suffix or re.fullmatch(r'(?:/\s*)?(?:each|ea|item|pkg|package|lb|oz|kg|g)', suffix):
        kind = 'exact'
    else:
        return None, 'unknown', 'unspecified'
    basis = 'per_item' if re.fullmatch(r'(?:/\s*)?(?:each|ea|item|pkg|package)', suffix) else 'unspecified'
    weighted = re.fullmatch(r'/\s*(lb|oz|kg|g)', suffix)
    if weighted:
        basis = 'per_' + weighted[1]
    return float(Decimal(match[1])), kind, basis


def _unit_price(label):
    if not isinstance(label, str):
        return None
    match = re.fullmatch(r'\s*\$([0-9]+(?:\.[0-9]+)?)\s*/\s*(fl oz|oz|lb|g|kg|ml|l|ct|each)\s*', label, re.I)
    return {'amount': float(Decimal(match[1])), 'currency': 'USD', 'unit': match[2].lower(), 'source': 'provider_label'} if match and Decimal(match[1]) <= 1000000 else None


def product_url(value, retailer=None):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\x00-\x20\\]', value):
        return None
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query)
        valid = (parsed.scheme == 'https' and parsed.hostname in ('www.instacart.com', 'instacart.com')
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment
                 and re.match(r'^/products/\d+(?:-|$)', parsed.path)
                 and set(query).issubset({'retailerSlug'})
                 and (not retailer or not query.get('retailerSlug') or query['retailerSlug'] == [retailer]))
        return value if valid else None
    except ValueError:
        return None


def _image_url(value):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\x00-\x20\\]', value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == 'https' and (parsed.hostname or '').endswith(('.instacart.com', '.cloudfront.net'))
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and parsed.port in (None, 443)):
            return value
    except ValueError:
        pass
    return None


def _pack(row, size):
    value = row.get('Pack Count', row.get('pack_count'))
    if type(value) is int and value > 0:
        return value
    match = re.match(r'^\s*([1-9][0-9]*)\s*[x×]\s*\d', str(size or ''), re.I)
    return int(match[1]) if match else None


def normalize(row, store, query, retailer, zip_code, captured=None, expires=None):
    if not isinstance(row, dict):
        return None
    name = row.get('Product Name')
    if not isinstance(name, str) or not name.strip() or not matches_query(name, query):
        return None
    name = name.strip()[:500]
    size = row.get('Unit Size') if isinstance(row.get('Unit Size'), str) else None
    price, kind, basis = _money(row.get('Price'))
    currency = row.get('Currency') or ('USD' if price is not None else None)
    url = product_url(row.get('Product URL'), retailer)
    provider_id = row.get('Product ID', row.get('product_id'))
    if provider_id is None and url:
        provider_id = re.match(r'^/products/(\d+)', urlsplit(url).path)[1]
    provider_id = str(provider_id)[:150] if isinstance(provider_id, (str, int)) and not isinstance(provider_id, bool) else None
    upc = row.get('UPC', row.get('upc'))
    upc = upc if isinstance(upc, str) and re.fullmatch(r'\d{8}|\d{12,14}', upc) else None
    store_id = store.get('Store Location ID')
    identity = json.dumps(['foodspark', retailer, store_id, provider_id or upc, name, size, _pack(row, size)])
    capture = row.get('Captured At', row.get('captured_at', captured))
    expiry = row.get('Valid Until', row.get('valid_until', expires))
    # Request time and naive timestamps are not source capture evidence.
    capture = capture if _timestamp(capture) is not None else None
    expiry = expiry if _timestamp(expiry) is not None else None
    images = row.get('product_images', [])
    image = next((safe for v in images if (safe := _image_url(v))), None) if isinstance(images, list) else None
    item = {'id': hashlib.sha256(identity.encode()).hexdigest()[:24],
            'identity_kind': 'observation', 'provider_product_id': provider_id, 'upc': upc,
            'name': name, 'brand': row.get('Brand'), 'size': size, 'pack_count': _pack(row, size),
            'price': price, 'currency': currency, 'price_label': row.get('Price'), 'price_kind': kind,
            'unit_price': _unit_price(row.get('Price Per Unit')), 'unit_price_label': row.get('Price Per Unit'),
            'price_basis': basis, 'price_basis_verified': basis != 'unspecified',
            'price_unit': 'item' if basis == 'per_item' else basis[4:] if basis.startswith('per_') else None,
            'query': query, 'retailer': retailer, 'provider_location_id': str(store_id) if store_id is not None else None,
            'store': {'name': store.get('Store Name') or retailer, 'location_id': str(store_id) if store_id is not None else None,
                      'delivery_zip': zip_code, 'branch_verified': False},
            'provider': 'instacart', 'source': 'foodspark', 'data_provider': 'xbyte_foodspark',
            'platform': 'instacart', 'price_source': 'instacart_marketplace',
            'observed_zip': zip_code, 'image_url': image, 'url': url,
            'details_status': 'available' if url else 'product_link_unavailable',
            'handoff': {'kind': 'merchant_link' if url else 'unavailable', 'url': url, 'cart_transfer_supported': False},
            'availability': row.get('Availability'), 'availability_verified': False,
            'availability_captured_at': None, 'captured_at': capture, 'source_expires_at': expiry,
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'data_kind': 'snapshot',
            'refresh_schedule_verified': False}
    return refresh_evidence(item)


def _verify_store(store, location, retailer):
    address = location['address']
    if (not isinstance(store, dict) or str(store.get('Delivery Zip Code')) != location['postal_code']
            or store.get('Store Slug') != retailer
            or str(store.get('Delivery Address', '')).strip().casefold() != address['line1'].casefold()):
        raise ConnectorError('location_unsupported', 'X-Byte did not acknowledge the requested retailer and street address.')


def search_live(zip_code, queries, retailer, allow, browser_search):
    if not selected(zip_code, retailer):
        return browser_search(zip_code, queries, retailer, allow)
    location = current_location(zip_code)
    items = {}
    for query in queries:
        payload = transport.request_json('/search', {**_params(location, retailer),
            'endpoint_type': 'dl295_search_by_addr_brand_slug_and_keyword', 'search_term': query})
        result = payload.get('response')
        if not isinstance(result, dict) or not isinstance(result.get('results'), list):
            raise ConnectorError('invalid_response', 'X-Byte grocery result does not match the documented contract.')
        if not result['results']:
            continue
        if not any(isinstance(row, dict) and isinstance(row.get('Product Name'), str) and row['Product Name'].strip() for row in result['results']):
            raise ConnectorError('invalid_response', 'X-Byte grocery rows did not contain recognizable product names.')
        store = result.get('store_details')
        _verify_store(store, location, retailer)
        for row in result['results']:
            item = normalize(row, store, query, retailer, zip_code, result.get('captured_at'), result.get('valid_until'))
            if item:
                items[(query, item['id'])] = item
    if not items:
        raise ConnectorError('no_match', 'X-Byte returned no products that match the requested item. Recommendations were excluded.')
    return {'items': list(items.values()),
            'location': {'requested_zip': zip_code, 'observed_zip': zip_code, 'verified': True,
                         'context_id': request_context_id(location), 'address_selected': True,
                         'verification_method': 'foodspark_delivery_address', 'verification_scope': 'zip_only',
                         'delivery_serviceability_verified': False, 'store_branch_verified': False},
            'retailers': [{'id': retailer, 'name': store.get('Store Name') or retailer, 'products_returned': len(items)}],
            'warnings': ['Instacart marketplace snapshot; not a direct retailer price. Source capture time, stock freshness and refresh guarantees may be unknown.',
                         'Merchant links do not transfer a cart. Missing product URLs cannot be reconstructed.'],
            'coverage_complete': False}


def details(item, location):
    """Accept only the item/context resolved from a server-held search."""
    if item.get('data_provider') != 'xbyte_foodspark':
        return {'status': 'merchant_link_only', 'item': item, 'handoff': {
            'kind': 'merchant_link' if item.get('url') else 'unavailable',
            'url': item.get('url'), 'cart_transfer_supported': False},
            'cart_updated': False, 'provider_cart_updated': False}
    url = product_url(item.get('url'), item.get('retailer'))
    if not url:
        raise ConnectorError('product_link_unavailable', 'X-Byte search did not supply a usable product URL. Product details cannot be fetched.')
    if not location.get('address'):
        raise ConnectorError('location_required', 'The original street-address search context is required.')
    payload = transport.request_json('/product_details', {**_params(location, item['retailer']),
        'endpoint_type': 'dl295_pdp_by_addr_and_product_url', 'product_url': url})
    result = payload.get('response', {})
    rows = result.get('results') if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise ConnectorError('invalid_response', 'X-Byte details did not match the documented contract.')
    for row in rows:
        value = row.get('Result') if isinstance(row, dict) else None
        store = row.get('Store Details', {}) if isinstance(row, dict) else {}
        if not isinstance(value, dict) or product_url(value.get('Product URL'), item['retailer']) != url:
            continue
        if str(store.get('Zipcode')) != location['postal_code']:
            raise ConnectorError('location_unsupported', 'X-Byte details did not confirm the original ZIP.')
        normalized = normalize(value, {'Store Name': store.get('ShopName'), 'Store Location ID': store.get('Store Location ID')},
                               item['query'], item['retailer'], location['postal_code'], result.get('captured_at'), result.get('valid_until'))
        if normalized:
            normalized['store']['address'] = store.get('Street')
            normalized['location_verified'] = False
            normalized['selectable'] = False
            normalized['local_offer_eligible'] = False
            normalized['discovery_only'] = True
            return {'status': 'ok', 'item': normalized, 'search_item_id': item['id'],
                    'quote_updated': False, 'cart_updated': False, 'provider_cart_updated': False,
                    'handoff': normalized['handoff'],
                    'message': 'Details observation only. Existing basket prices are unchanged; search again to replace a quote.'}
    raise ConnectorError('no_match', 'X-Byte did not return details for the selected product URL.')

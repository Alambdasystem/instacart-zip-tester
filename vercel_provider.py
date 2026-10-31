"""Bounded read-only adapter for mealmeai.vercel.app, separate from Satsuma.
Catalog mappings are provisional until verified with this account's live key.
"""
import hashlib
import math
import os
from urllib.parse import urlencode
from satsuma_provider import ProviderError, get_json, safe_image

BASE_URL = 'https://mealmeai.vercel.app'
PROVIDERS = {'vercel_kroger': 'kroger', 'vercel_square': 'square'}
PATHS = ('/api/stores/search', '/api/catalog/search')

def request(path, params):
    if path not in PATHS or params.get('providerId') not in PROVIDERS.values():
        raise ProviderError('unsupported_operation', 'Only Kroger/Square discovery and catalog reads are enabled.')
    key = os.getenv('VERCEL_MEALME_API_KEY', '')
    if not key:
        raise ProviderError('not_configured', 'Vercel proxy key is not configured; this is separate from Satsuma.')
    try:
        return get_json(BASE_URL + path + '?' + urlencode(params), {'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    except ProviderError as e:
        if e.http_status == 402:
            raise ProviderError('billing_required', 'Proxy billing requires attention; no automatic payment was attempted.', 402) from None
        raise

def rows(data, field):
    if not isinstance(data, dict) or data.get('error') or not isinstance(data.get(field), list):
        raise ProviderError('invalid_response', 'Proxy response did not match the expected ' + field + ' contract.')
    return data[field]

def normalize(data, provider, store, query, zip_code):
    items = []
    for p in rows(data, 'items'):
        if not isinstance(p, dict) or not p.get('providerItemId') or not (p.get('title') or p.get('name')):
            continue
        if p.get('providerId', PROVIDERS[provider]) != PROVIDERS[provider]:
            raise ProviderError('identity_mismatch', 'Proxy item provider did not match the requested source.')
        if p.get('providerLocationId', store['id']) != store['id']:
            raise ProviderError('identity_mismatch', 'Proxy item location did not match the selected store.')
        money = p.get('price') if isinstance(p.get('price'), dict) else {}
        raw_price = money.get('amount', p.get('priceCents', p.get('price')))
        if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)) or not math.isfinite(raw_price) or raw_price < 0:
            raw_price = None
        identity = '|'.join((provider, store['id'], str(p['providerItemId'])))
        items.append({'id': provider + ':' + hashlib.sha256(identity.encode()).hexdigest()[:32],
            'provider': provider, 'provider_product_id': str(p['providerItemId']),
            'provider_location_id': store['id'], 'retailer': store['name'],
            'name': str(p.get('title') or p['name']), 'query': query, 'size': None,
            'price': None, 'provider_price': raw_price, 'currency': money.get('currency', p.get('currency')),
            'price_unit_verified': False, 'selectable': False,
            'image_url': safe_image(p.get('imageUrl') or p.get('image_url')), 'url': None,
            'availability': 'unknown', 'location_verified': False, 'requested_zip': zip_code})
    if data['items'] and not items:
        raise ProviderError('invalid_response', 'Proxy returned items but no recognized product identities; mapping needs review.')
    return items

def search(zip_code, queries, provider, request):
    upstream = PROVIDERS[provider]
    data = request('/api/stores/search', {'providerId': upstream,
        'merchantType': 'grocery' if upstream == 'kroger' else 'restaurant',
        'zipCode': zip_code, 'radiusInMiles': 25 if upstream == 'kroger' else 5, 'limit': 2})
    stores = {}
    for s in rows(data, 'stores'):
        if isinstance(s, dict) and s.get('providerLocationId') and s.get('name'):
            if s.get('providerId', upstream) != upstream:
                raise ProviderError('identity_mismatch', 'Proxy store provider does not match request.')
            ident = str(s['providerLocationId'])
            stores[ident] = {'id': ident, 'name': str(s['name']), 'provider': provider}
    # Bound fanout even if upstream ignores limit. Do not turn empty/failed discovery into a global search.
    selected = list(stores.values())[:2]
    items = []
    error = None
    for store in selected:
        for q in queries:
            try:
                data = request('/api/catalog/search', {'providerId': upstream, 'locationId': store['id'], 'query': q, 'limit': 12})
                items.extend(normalize(data, provider, store, q, zip_code))
            except ProviderError as e:
                error = {'code':e.code,'message':e.message,'http_status':e.http_status}
                break
        if error:break
    return {'provider': provider, 'items': items, 'retailers': selected, 'error': error,
        'location': {'requested_zip': zip_code, 'observed_zip': None, 'verified': False, 'scope': 'nearby_store_discovery'},
        'warnings': ['Bounded sample: at most two nearby stores per query; not exhaustive ZIP coverage or delivery verification.',
                     'Proxy catalog mapping and price units await live account verification; comparison only, no checkout.']}

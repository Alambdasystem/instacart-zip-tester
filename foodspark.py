"""FoodSpark demo adapter for address-scoped retailer searches."""
import hashlib
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import deadline
from connector import ConnectorError
from location_context import current_location
from provider_session import request_context_id


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def search_live(zip_code, queries, retailer, allow, browser_search):
    key = os.getenv('FOODSPARK_API_KEY')
    location = current_location(zip_code)
    address = location.get('address')
    if not key or not retailer or not address:
        return browser_search(zip_code, queries, retailer, allow)

    items = {}
    for query in queries:
        params = {
            'country': 'usa', 'platform': 'web', 'domain': 'instacart',
            'endpoint_type': 'dl295_search_by_addr_brand_slug_and_keyword',
            'brand_slug': retailer, 'search_term': query, 'zipcode': zip_code,
            'address': ', '.join(filter(None, (
                address['line1'], address.get('line2'), address['city'],
                address['region'] + ' ' + zip_code,
            ))),
            'api_key': key,
        }
        request = Request('https://foodsparkfastapiframework.xbyte.io/search?' + urlencode(params))
        try:
            with build_opener(NoRedirect()).open(request, timeout=deadline.bounded_seconds(30)) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('Oversized response')
            payload = json.loads(raw)
            result = payload['response']
            store = result['store_details']
            rows = result['results']
            if not isinstance(rows, list) or str(payload['request_log']['status']) != '200':
                raise ValueError('Unexpected provider result')
        except HTTPError as error:
            code = 'provider_quota' if error.code in (402, 429) else 'connector_error'
            raise ConnectorError(code, f'FoodSpark returned HTTP {error.code}.') from None
        except TimeoutError:
            raise ConnectorError('provider_timeout', 'FoodSpark search timed out.') from None
        except (OSError, ValueError, KeyError, TypeError):
            raise ConnectorError('connector_error', 'FoodSpark did not return a valid search result.') from None

        if (str(store.get('Delivery Zip Code')) != zip_code
                or store.get('Store Slug') != retailer
                or str(store.get('Delivery Address', '')).strip().casefold() != address['line1'].casefold()):
            raise ConnectorError('location_unsupported', 'FoodSpark did not confirm the requested retailer and address.')

        for row in rows:
            if not isinstance(row, dict):
                continue
            name, size = row.get('Product Name'), row.get('Unit Size')
            price = re.fullmatch(r'\$([0-9]+(?:\.[0-9]{2})?)', str(row.get('Price')))
            if not isinstance(name, str) or not name.strip() or not price:
                continue
            identity = json.dumps([retailer, store.get('Store Location ID'), name, size])
            item_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
            items[(query, item_id)] = {
                'id': item_id, 'name': name, 'size': size, 'price': float(price[1]),
                'currency': 'USD', 'query': query, 'retailer': retailer,
                'source': 'foodspark', 'observed_zip': zip_code, 'image_url': None,
                'url': None, 'price_basis': 'unspecified', 'price_basis_verified': False,
                'availability': row.get('Availability'),
            }

    return {
        'items': list(items.values()),
        'location': {
            'requested_zip': zip_code, 'observed_zip': zip_code, 'verified': True,
            'context_id': request_context_id(location), 'address_selected': True,
            'verification_method': 'foodspark_delivery_address', 'verification_scope': 'zip_only',
            'delivery_serviceability_verified': False, 'store_branch_verified': False,
        },
        'retailers': [{'id': retailer, 'name': retailer, 'products_returned': len(items)}],
        'warnings': [], 'coverage_complete': False,
    }

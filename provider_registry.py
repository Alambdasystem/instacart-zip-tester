"""Active providers and explicit retirement of unsupported upstreams."""
import os

RETIRED_PROVIDERS = frozenset({
    'mealme', 'satsuma', 'mealme_satsuma', 'vercel_kroger',
    'vercel_square', 'vercel_proxy',
})
COMPARISON_PROVIDERS = ('instacart',)
CATALOG_PROVIDERS = ('instacart',)
CAPABILITIES = {
    'instacart': {'kind': 'grocery_discovery', 'location_inputs': ['zip', 'address'],
                 'address_acknowledgment_required': True, 'delivery_verified': False,
                 'cart': 'northfork_handoff'},
    'xbyte_foodspark': {'kind': 'grocery_discovery', 'location_inputs': ['address'],
                       'retailer_required': True, 'address_acknowledgment_required': True,
                       'zip_store_discovery': False, 'delivery_verified': False,
                       'cart': 'northfork_handoff'},
    'northfork': {'kind': 'checkout_handoff', 'retailer_scope': 'all_enabled_northfork_retailers',
                  'routes': ['/api/v1/checkout/stores', '/api/v1/checkout/resolve', '/api/v1/checkout/handoff'],
                  'order_placement': False},
}


def source_provider(item):
    """Keep the existing catalog namespace while disclosing the actual fetcher."""
    if item.get('source_provider') in ('xbyte_foodspark', 'instacart_browser'):
        return item['source_provider']
    return 'xbyte_foodspark' if item.get('source') == 'foodspark' else 'instacart_browser'


def routing_status():
    return {
        'primary_provider': 'xbyte_foodspark', 'selection': 'conditional',
        'configured': bool(os.getenv('FOODSPARK_API_KEY', '').strip()),
        'requires': ['retailer', 'location.address'],
        'otherwise': 'instacart_browser', 'on_primary_error': 'error_no_fallback',
        'comparison': 'instacart_browser', 'background_ingestion': 'instacart_browser',
        'checkout_provider': 'northfork',
        'checkout_routes': ['/api/v1/checkout/stores', '/api/v1/checkout/resolve', '/api/v1/checkout/handoff'],
        'provider_test_route': '/api/v1/provider-tests/search',
    }

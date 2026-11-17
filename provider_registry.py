"""Declared adapter capabilities, separate from actual provider availability."""
CAPABILITIES={
 'instacart':{'kind':'grocery_discovery','location_inputs':['zip','address'],'address_acknowledgment_required':True,'delivery_verified':False,'cart':'local_app_only'},
 'xbyte_foodspark':{'kind':'grocery_and_restaurant_demo','selection':'existing_instacart_search_when_enabled_with_address_and_retailer','grocery_platform':'instacart','restaurant_platforms':['yelp'],'pending_platforms':['doordash','ubereats'],'location_inputs':['address'],'restaurant_location_inputs':['zip'],'grocery_store_discovery_verified':False,'capture_schedule_verified':False,'product_details_requires_returned_url':True,'cross_platform_matching':False,'cart':'local_app_only','cart_transfer':False,'default_enabled':False},
 'mealme_satsuma':{'kind':'food_catalog','location_inputs':['zip','coordinates'],'address_supported':False,'price_units_verified':False,'cart':'unsupported'},
 'vercel_kroger':{'kind':'grocery','location_inputs':['zip'],'documented_routes_verified':True,'live_contract_verified':False,'price_units_verified':False,'cart':'unsupported'},
 'vercel_square':{'kind':'restaurant','location_inputs':['zip'],'documented_routes_verified':True,'live_contract_verified':False,'price_units_verified':False,'cart':'unsupported'}
}

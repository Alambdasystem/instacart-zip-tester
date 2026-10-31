"""Declared adapter capabilities, separate from actual provider availability."""
CAPABILITIES={
 'instacart':{'kind':'grocery_discovery','location_inputs':['zip','address'],'address_acknowledgment_required':True,'delivery_verified':False,'cart':'local_app_only'},
 'mealme_satsuma':{'kind':'food_catalog','location_inputs':['zip','coordinates'],'address_supported':False,'price_units_verified':False,'cart':'unsupported'},
 'vercel_kroger':{'kind':'grocery','location_inputs':['zip'],'documented_routes_verified':True,'live_contract_verified':False,'price_units_verified':False,'cart':'unsupported'},
 'vercel_square':{'kind':'restaurant','location_inputs':['zip'],'documented_routes_verified':True,'live_contract_verified':False,'price_units_verified':False,'cart':'unsupported'}
}

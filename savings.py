"""Merchandise savings from two server-held, fresh, equivalent offers."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re,time
from urllib.parse import urlsplit

class SavingsError(ValueError):
    def __init__(self,code,message):self.code=code;super().__init__(message)

def is_xbyte_offer(item):
    return item.get('data_provider') == 'xbyte_foodspark' or item.get('source') == 'foodspark'


def validate_offer(item):
    """Return authoritative Decimal money or a typed ineligibility error.

    Legacy browser items may omit selectability and charge-unit metadata. X-Byte
    items must carry the stricter evidence produced by its adapter. Freshness
    here means the adapter's provider-capture assessment, never retrieval age.
    """
    if is_xbyte_offer(item):
        freshness = item.get('source_freshness')
        if freshness != 'fresh':
            code = 'source_stale' if freshness in ('stale', 'expired') else 'source_freshness_unknown'
            raise SavingsError(code, 'A verified current provider capture is required for this offer.')
        if item.get('price_basis_verified') is not True:
            raise SavingsError('price_unit_unverified', 'The provider must verify the charge unit before selection.')
        if item.get('price_basis') != 'per_item':
            raise SavingsError('weighted_quantity_required', 'This basket requires a verified per-item price.')
        if item.get('price_unit') != 'item':
            raise SavingsError('price_unit_unverified', 'A verified item charge unit is required.')
        qualifiers = {'starting_at', 'starting at', 'estimated', 'estimate', 'from', 'minimum'}
        if (any(str(item.get(field, '')).strip().casefold() in qualifiers
                for field in ('price_kind', 'price_type', 'price_qualifier', 'price_status'))
                or item.get('is_estimated') is True or item.get('starting_at') is True
                or re.search(r'\b(starting at|starts at|from|estimated)\b', str(item.get('price_label') or ''), re.I)
                or item.get('promotion_text')):
            raise SavingsError('conditional_price', 'Estimated, starting-at or conditional prices cannot establish this total.')
    if item.get('selectable') is False:
        raise SavingsError('offer_unselectable', 'This observation is not eligible for selection or basket totals.')
    if item.get('currency') != 'USD':
        raise SavingsError('price_unit_mismatch', 'This basket requires a verified USD price.')
    raw = item.get('price')
    if raw is None or raw == '':
        raise SavingsError('price_unavailable', 'The provider did not establish a price for this offer.')
    if isinstance(raw, bool):
        raise SavingsError('invalid_price', 'The offer price must be a finite nonnegative amount in cents.')
    try:
        amount = Decimal(str(raw))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        raise SavingsError('invalid_price', 'The offer price must be a finite nonnegative amount in cents.') from None
    return amount


def _text(value):
    return ' '.join(str(value or '').casefold().split())


def _size(value):
    """Normalize explicit display units, without guessing weight/volume conversion."""
    value = _text(value)
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|fluid ounces?|ounces?|oz|pounds?|lbs?|lb|grams?|g|kilograms?|kg|millilit(?:er|re)s?|ml|lit(?:er|re)s?|l|gallons?|gal|quarts?|qt|counts?|ct)', value)
    if not match:
        # Preserve compound pack/size expressions exactly after whitespace/case
        # normalization; do not infer a pack count from them.
        return value
    amount, unit = match.groups()
    aliases = {'ounces': 'oz', 'ounce': 'oz', 'pound': 'lb', 'pounds': 'lb', 'lbs': 'lb',
               'gram': 'g', 'grams': 'g', 'kilogram': 'kg', 'kilograms': 'kg',
               'milliliter': 'ml', 'milliliters': 'ml', 'millilitre': 'ml', 'millilitres': 'ml',
               'liter': 'l', 'liters': 'l', 'litre': 'l', 'litres': 'l',
               'gallon': 'gal', 'gallons': 'gal', 'quart': 'qt', 'quarts': 'qt',
               'count': 'ct', 'counts': 'ct'}
    if re.fullmatch(r'fl\.?\s*oz|fluid ounces?', unit):
        unit = 'fl oz'
    return str(Decimal(amount).normalize()) + ' ' + aliases.get(unit, unit)


def _upc(item):
    value = item.get('upc')
    # UPC/GTIN must retain their leading zeros. Numeric values cannot establish
    # that identity; ingestion should preserve the vendor's string value.
    return value if isinstance(value, str) and re.fullmatch(r'\d{8}|\d{12,14}', value) else None


def _product_id(item):
    value = item.get('provider_product_id')
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value == item.get('id') or value.casefold().startswith(('observation:', 'synthetic:', 'local:')):
        return None
    return value


def _xbyte_equivalence(a, b):
    if not (is_xbyte_offer(a) and is_xbyte_offer(b)):
        raise SavingsError('provider_unsupported', 'A cross-provider product mapping has not been verified.')
    if (not a.get('platform') or a.get('platform') != b.get('platform')
            or a.get('price_source') not in ('instacart_marketplace', 'direct_retailer')
            or a.get('price_source') != b.get('price_source')):
        raise SavingsError('price_source_mismatch', 'Keep marketplace/platform prices separate from direct retailer prices.')
    a_upc, b_upc = _upc(a), _upc(b)
    a_id, b_id = _product_id(a), _product_id(b)
    conflicting_upc = a_upc and b_upc and a_upc != b_upc
    shared_identity = (a_upc and a_upc == b_upc) or (a_id and a_id == b_id)
    if conflicting_upc or not shared_identity:
        raise SavingsError('product_mismatch', 'A shared vendor product ID or UPC is required; observation IDs are insufficient.')
    if (not _text(a.get('brand')) or _text(a.get('brand')) != _text(b.get('brand'))
            or not _size(a.get('size')) or _size(a.get('size')) != _size(b.get('size'))
            or type(a.get('pack_count')) is not int or type(b.get('pack_count')) is not int
            or a['pack_count'] < 1 or a['pack_count'] != b['pack_count']):
        raise SavingsError('product_mismatch', 'Brand, package size and explicit pack count must match.')


def calculate(store,body,now=None):
    now=time.time() if now is None else now
    if set(body)-{'offers','quantity'}:raise ValueError('Unknown savings fields.')
    refs=body.get('offers');qty=body.get('quantity',1)
    if not isinstance(refs,list) or len(refs)!=2 or type(qty) is not int or not 1<=qty<=99:raise ValueError('Supply two offer references and quantity 1–99.')
    offers=[];contexts=[];expiries=[]
    for ref in refs:
        if not isinstance(ref,dict) or set(ref)!={'search_id','item_id'} or any(not isinstance(v,str) for v in ref.values()):raise ValueError('Invalid offer reference.')
        result=store.get(ref['search_id'])
        if not result or now>=result.get('expires',result['created']+300):raise SavingsError('price_expired','Refresh both offers before comparing.')
        payload=result['payload'];loc=payload.get('location',{})
        if not loc.get('verified') or not loc.get('context_id') or loc.get('observed_zip')!=loc.get('requested_zip'):raise SavingsError('location_unverified','Both offers must acknowledge the same requested location.')
        item=next((i for i in payload['items'] if i['id']==ref['item_id']),None)
        if not item:raise ValueError('Unknown offer.')
        if not item.get('price_basis_verified') or item.get('price_basis')=='unspecified':raise SavingsError('price_unit_unverified','The provider must expose the charge unit for both offers.')
        if item.get('promotion_text'):raise SavingsError('conditional_price','Conditional/promotional prices require terms validation before savings.')
        if item.get('provider','instacart')!='instacart' and not is_xbyte_offer(item):raise SavingsError('provider_unsupported','This comparison currently supports Instacart and verified X-Byte grocery observations only.')
        validate_offer(item)
        offers.append(item);contexts.append(loc);expiries.append(result.get('expires',result['created']+300))
    a,b=offers
    if (contexts[0]['context_id'],contexts[0]['requested_zip'])!=(contexts[1]['context_id'],contexts[1]['requested_zip']):raise SavingsError('mixed_location_context','Keep both offers in the same location context.')
    if not a.get('retailer') or not b.get('retailer') or a['retailer']==b['retailer']:raise SavingsError('different_merchants_required','Choose two distinct merchants.')
    def identity(item):
        u=urlsplit(item.get('url',''))
        return re.match(r'^/products/(\d+)(?:-|$)',u.path).group(1) if u.scheme=='https' and u.hostname=='www.instacart.com' and re.match(r'^/products/(\d+)(?:-|$)',u.path) else None
    # Never use a lexical search hit as proof of product equivalence.
    if is_xbyte_offer(a) or is_xbyte_offer(b):
        _xbyte_equivalence(a, b)
    elif not identity(a) or identity(a)!=identity(b) or not a.get('size') or (_text(a['size']),_text(a['name']))!=(_text(b.get('size')),_text(b.get('name'))):raise SavingsError('product_mismatch','Require the same provider product identity, name and package size.')
    if a.get('currency')!='USD' or b.get('currency')!='USD' or a.get('price_basis')!=b.get('price_basis') or a.get('price_unit')!=b.get('price_unit'):raise SavingsError('price_unit_mismatch','Currency and charge units must match.')
    if a['price_basis']!='per_item':raise SavingsError('weighted_quantity_required','Weighted offers require a verified weight; integer item quantities are insufficient.')
    low,high=sorted(offers,key=lambda i:Decimal(str(i['price'])))
    money=lambda n:float(n.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    original=Decimal(str(high['price']))*qty;replacement=Decimal(str(low['price']))*qty
    return {'status':'verified_observation_comparison','compared_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(now)),'offers':[{**i,'search_id':refs[n]['search_id']} for n,i in enumerate(offers)],
            'quantity':qty,'currency':'USD','original_subtotal':money(original),'replacement_subtotal':money(replacement),
            'savings':money(original-replacement),'savings_percent':money((original-replacement)*100/original) if original else 0,
            'lower_price_merchant':low['retailer'],'higher_price_merchant':high['retailer'],
            'expires_in':max(0,int(min(expiries)-now)),'location':contexts[0],
            'scope':'Observed merchandise prices only; excludes taxes, fees, delivery and checkout verification.'}

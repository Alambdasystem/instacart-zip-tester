"""Merchandise savings from two server-held, fresh, equivalent offers."""
from decimal import Decimal, ROUND_HALF_UP
import re,time
from urllib.parse import urlsplit

class SavingsError(ValueError):
    def __init__(self,code,message):self.code=code;super().__init__(message)

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
        if item.get('provider','instacart')!='instacart':raise SavingsError('provider_unsupported','This comparison currently supports Instacart observations only.')
        amount=Decimal(str(item.get('price')))
        if not amount.is_finite() or amount<0 or amount!=amount.quantize(Decimal('.01')):raise ValueError('Invalid money amount.')
        offers.append(item);contexts.append(loc);expiries.append(result.get('expires',result['created']+300))
    a,b=offers
    if (contexts[0]['context_id'],contexts[0]['requested_zip'])!=(contexts[1]['context_id'],contexts[1]['requested_zip']):raise SavingsError('mixed_location_context','Keep both offers in the same location context.')
    if not a.get('retailer') or not b.get('retailer') or a['retailer']==b['retailer']:raise SavingsError('different_merchants_required','Choose two distinct merchants.')
    def identity(item):
        u=urlsplit(item.get('url',''))
        return re.match(r'^/products/(\d+)(?:-|$)',u.path).group(1) if u.scheme=='https' and u.hostname=='www.instacart.com' and re.match(r'^/products/(\d+)(?:-|$)',u.path) else None
    def text(value):return ' '.join(str(value or '').casefold().split())
    # Never use a lexical search hit as proof of product equivalence.
    if not identity(a) or identity(a)!=identity(b) or not a.get('size') or (text(a['size']),text(a['name']))!=(text(b.get('size')),text(b.get('name'))):raise SavingsError('product_mismatch','Require the same provider product identity, name and package size.')
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

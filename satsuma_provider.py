"""Read-only account-enabled MealMe/Satsuma adapter. Never follows credential redirects."""
import hashlib, json, math, os, time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit

BASE_URL = 'https://api.satsuma.ai'
class ProviderError(Exception):
    def __init__(self, code, message, http_status=None):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

def get_json(url, headers=None):
    try:
        with build_opener(NoRedirect).open(Request(url, headers=headers or {}), timeout=15) as r:
            if 'application/json' not in r.headers.get('Content-Type', ''):
                raise ProviderError('invalid_response', 'Provider did not return JSON.', r.status)
            body=r.read(2_000_001)
            if len(body)>2_000_000: raise ProviderError('invalid_response','Provider response exceeded size limit.')
            return json.loads(body)
    except HTTPError as e:
        code = 'authentication_failed' if e.code in (401,403) else 'rate_limited' if e.code==429 else 'service_unavailable' if e.code>=500 else 'http_error'
        raise ProviderError(code, 'Provider returned HTTP '+str(e.code)+'.', e.code) from None
    except (URLError, TimeoutError): raise ProviderError('connection_failed','Provider connection failed or timed out.') from None
    except (ValueError, UnicodeError): raise ProviderError('invalid_response','Provider returned invalid JSON.') from None

ZIP_CACHE={}
def zip_coordinates(zip_code):
    # Public ZIP centroid lookup, not an address or delivery-serviceability assertion.
    if zip_code not in ZIP_CACHE:
        data=get_json('https://api.zippopotam.us/us/'+zip_code)
        try:
            places=data['places']; latitude=float(places[0]['latitude']); longitude=float(places[0]['longitude'])
            if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90<=latitude<=90 or not -180<=longitude<=180:raise ValueError()
            if len(ZIP_CACHE)>=100:ZIP_CACHE.pop(next(iter(ZIP_CACHE)))
            ZIP_CACHE[zip_code]=(latitude,longitude)
        except (KeyError,IndexError,TypeError,ValueError):raise ProviderError('zip_lookup_failed','ZIP centroid is unavailable.') from None
    return ZIP_CACHE[zip_code]

def safe_image(value):
    if not isinstance(value,str):return None
    try:
        u=urlsplit(value)
        if u.scheme=='https' and u.hostname and not u.username and not u.password and not u.query and not u.fragment:return value
    except ValueError:pass
    return None

def normalize(data,query,zip_code):
    if not isinstance(data,dict) or not isinstance(data.get('data'),list):
        raise ProviderError('invalid_response','Expected a provider data array.')
    if any(isinstance(m,dict) and str(m.get('level','')).lower()=='error' for m in data.get('messages',[])):
        raise ProviderError('provider_error','Provider reported an error; results were not cached.')
    items=[]
    for p in data['data']:
        if not isinstance(p,dict) or not p.get('id') or not p.get('name'):continue
        location=p.get('location') if isinstance(p.get('location'),dict) else {}
        merchant=str(p.get('merchant_id') or location.get('merchant_id') or '')
        location_id=str(location.get('id') or '')
        ident='|'.join((merchant,location_id,str(p['id'])))
        raw_price=p.get('price')
        if isinstance(raw_price,bool) or not isinstance(raw_price,(int,float)) or not math.isfinite(raw_price) or raw_price<0:raw_price=None
        items.append({'id':'satsuma:'+hashlib.sha256(ident.encode()).hexdigest()[:32],
            'provider':'mealme_satsuma','provider_product_id':str(p['id']), 'provider_location_id':location_id or None,
            'name':str(p['name']), 'retailer':merchant,'size':None,'query':query,
            'price':None,'provider_price':raw_price,'currency':p.get('currency'),
            'price_unit_verified':False,'selectable':False,
            'image_url':safe_image(p.get('image_url')),'url':None,
            'availability':p.get('status','unknown'),'upc':p.get('upc'),
            'location_verified':False,'requested_zip':zip_code})
    return items

def search(zip_code,queries,request):
    lat,lon=zip_coordinates(zip_code)
    items=[]
    for query in queries:
        data=request('/product',{'latitude':lat,'longitude':lon,'query':query,'size':25,'page':1})
        items.extend(normalize(data,query,zip_code))
    return {'provider':'mealme_satsuma','items':items,
        'location':{'requested_zip':zip_code,'observed_zip':None,'verified':False,'latitude':lat,'longitude':lon,'scope':'zip_centroid','coordinate_source':'zippopotam.us'},
        'retrieved_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        'warnings':['MealMe account uses Satsuma. ZIP centroid search does not prove delivery serviceability.',
                    'Raw provider prices retained; price units require live contract verification before cart use.']}

def request(path,params):
    if path not in ('/product','/location','/merchant'):raise ProviderError('unsupported_operation','Only read endpoints are enabled.')
    key=os.getenv('SATSUMA_API_KEY','')
    if not key:raise ProviderError('not_configured','MealMe/Satsuma key is not configured.')
    return get_json(BASE_URL+path+('?' + urlencode(params) if params else ''),{'Authorization':key,'Accept':'application/json'})

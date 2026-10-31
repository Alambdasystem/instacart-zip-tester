"""Validated request-scoped location; opaque cache identity, no address logging."""
import contextvars, hashlib, hmac, json, math, os, secrets
from pathlib import Path

_CURRENT=contextvars.ContextVar('location_context',default=None)
_SECRET=None

def normalize_location(data):
    raw=data.get('location')
    if raw is None:raw={'postal_code':data.get('zip',''),'country_code':'US'}
    if not isinstance(raw,dict) or set(raw)-{'postal_code','country_code','address','coordinates'}:raise ValueError('Invalid location fields.')
    z=raw.get('postal_code',data.get('zip',''))
    import re
    if not isinstance(z,str) or not re.fullmatch(r'\d{5}',z):raise ValueError('location.postal_code must contain five digits.')
    if data.get('zip',z)!=z:raise ValueError('zip conflicts with location.postal_code.')
    if raw.get('country_code','US')!='US':raise ValueError('Only US locations are currently supported.')
    out={'postal_code':z,'country_code':'US'}
    address=raw.get('address');coords=raw.get('coordinates')
    if address and coords:raise ValueError('Choose address or coordinates, not both.')
    if address is not None:
        if not isinstance(address,dict) or set(address)-{'line1','line2','city','region'}:raise ValueError('Invalid address fields.')
        if any(not isinstance(address.get(k),str) or not address[k].strip() or len(address[k])>200 for k in ['line1','city','region']):raise ValueError('Address requires line1, city and region.')
        if 'line2' in address and (not isinstance(address['line2'],str) or len(address['line2'])>100):raise ValueError('Invalid address line2.')
        out['address']={k:' '.join(v.split()) for k,v in address.items()}
    if coords is not None:
        if not isinstance(coords,dict) or set(coords)!={'latitude','longitude'}:raise ValueError('Coordinates require latitude and longitude.')
        for k,limit in [('latitude',90),('longitude',180)]:
            v=coords[k]
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not -limit<=v<=limit:raise ValueError('Invalid '+k)
        out['coordinates']=dict(coords)
    return out

def set_location(location):return _CURRENT.set(location)
def current_location(z=None):
    value=_CURRENT.get()
    return value if value and (z is None or value['postal_code']==z) else {'postal_code':z,'country_code':'US'}
def context_id(location=None):
    global _SECRET
    loc=location or current_location()
    if not loc.get('address') and not loc.get('coordinates'):return 'zip:'+str(loc['postal_code'])
    if _SECRET is None:
        path=Path(os.getenv('CATALOG_PATH','/tmp/alambda-grocery-catalog.sqlite')).parent/'location-context.key'
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:f.write(secrets.token_bytes(32))
        except FileExistsError:pass
        _SECRET=path.read_bytes()
    normalized=json.dumps(loc,sort_keys=True,separators=(',',':')).casefold().encode()
    return 'ctx:'+hmac.new(_SECRET,normalized,hashlib.sha256).hexdigest()

def has_precise_location(z=None):
    loc=current_location(z)
    return bool(loc.get('address') or loc.get('coordinates'))

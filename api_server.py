import hashlib, json, os, re, threading, time, uuid
from urllib.parse import urlsplit, parse_qs
from ingestion import Catalog, Ingestor
from search_cache import SearchCache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from connector import ConnectorError, search_browser

ROOT = Path(__file__).parent
LOCK = threading.Lock()
RESULTS = {}
RATE = {}
CACHE = SearchCache(ttl=300)
INVALIDATIONS = {}
CATALOG = Catalog(os.getenv('CATALOG_PATH','/tmp/alambda-grocery-catalog.sqlite'))
INGESTOR = None

def validate(data):
    if not isinstance(data, dict): raise ValueError('Expected a JSON object.')
    z = data.get('zip', '')
    if not isinstance(z, str) or not re.fullmatch(r'\d{5}', z): raise ValueError('zip must contain five digits.')
    qs = data.get('queries', [data['query']] if 'query' in data else [])
    if not isinstance(qs, list) or not 1 <= len(qs) <= 3 or any(not isinstance(q, str) or not q.strip() or len(q) > 120 for q in qs): raise ValueError('Send query or queries with 1–3 nonempty strings, up to 120 characters each.')
    retailer = data.get('retailer', '')
    if retailer not in ('', 'aldi'): raise ValueError('retailer must be empty or aldi for this prototype.')
    flag = data.get('allow_unverified_location', False)
    if type(flag) is not bool: raise ValueError('allow_unverified_location must be boolean.')
    return z, list(dict.fromkeys(q.strip() for q in qs)), retailer, flag

SPEC = {'openapi':'3.1.0','info':{'title':'Alambda Grocery Middleware','version':'0.1.0','description':'Experimental public-browser search connector. No checkout or cart writes. Location verification is explicit.'},'paths':{'/api/v1/search':{'post':{'summary':'Search food products','requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','required':['zip'],'properties':{'zip':{'type':'string','pattern':'^[0-9]{5}$'},'query':{'type':'string'},'queries':{'type':'array','minItems':1,'maxItems':3,'items':{'type':'string'}},'retailer':{'enum':['','aldi']},'allow_unverified_location':{'type':'boolean','default':False}}}}}},'responses':{'200':{'description':'Normalized live browser results; inspect location.verified'},'422':{'description':'Invalid input or delivery location required'},'429':{'description':'Busy or rate limited'},'502':{'description':'Provider blocked, login required, timeout, or unreadable results'}}}},'/api/v1/select':{'post':{'summary':'Select returned product IDs from a recent search','responses':{'200':{'description':'Validated local selection; no cart operation'},'404':{'description':'Search expired'}}}},'/health':{'get':{'responses':{'200':{'description':'Service ready'}}}}}}

SPEC['paths']['/api/v1/cache/invalidate']={'post':{'summary':'Expire store or restaurant cache and associated cart price snapshots','requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','required':['retailer'],'properties':{'retailer':{'enum':['aldi','*']},'zip':{'type':'string','pattern':'^[0-9]{5}$'}}}}}},'responses':{'200':{'description':'Cache invalidated'},'429':{'description':'Invalidation cooldown'}}}}

SPEC['paths']['/api/v1/cart']={'post':{'summary':'Validate our app cart against recent searches','requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','required':['lines'],'properties':{'lines':{'type':'array','maxItems':60,'items':{'type':'object','required':['search_id','item_id','quantity'],'properties':{'search_id':{'type':'string'},'item_id':{'type':'string'},'quantity':{'type':'integer','minimum':1,'maximum':99}}}}}}}}},'responses':{'200':{'description':'Validated local cart; provider cart unchanged'},'409':{'description':'Search prices expired; refresh required'},'422':{'description':'Invalid item or quantity'}}}}

for path,summary in [('/api/v1/catalog','Search the stored ingested catalog'),('/api/v1/catalog/export','Export ingested catalog'),('/api/v1/ingestion/status','Read ingestion queue and resource counters')]:
    SPEC['paths'][path]={'get':{'summary':summary,'responses':{'200':{'description':'Catalog or queue snapshot'}}}}

def merge_parts(parts, hit_queries, fetched_queries):
    payloads=[p for p,m in parts];metas=[m for p,m in parts]
    products={item['id']:item for payload in payloads for item in payload['items']}
    observed={p['location'].get('observed_zip') for p in payloads}
    location=dict(payloads[0]['location'])
    location['verified']=all(p['location']['verified'] for p in payloads)
    location['observed_zip']=observed.pop() if len(observed)==1 else None
    meta={'hit':not fetched_queries,'partial_hit':bool(hit_queries and fetched_queries),'hit_queries':hit_queries,'fetched_queries':fetched_queries,'age_seconds':max(m['age_seconds'] for m in metas),'expires_in':min(m['expires_in'] for m in metas),'ttl_seconds':300}
    return {'items':list(products.values()),'location':location,'warnings':list(dict.fromkeys(w for p in payloads for w in p.get('warnings',[]))),'retrieved_at':min(p.get('retrieved_at','') for p in payloads)},meta

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,code,data):
        raw=json.dumps(data).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        route=urlsplit(self.path)
        if route.path=='/api/v1/ingestion/status':
            return self.send(200,INGESTOR.status() if INGESTOR else {'enabled':False,'catalog':CATALOG.counts()})
        if route.path in ('/api/v1/catalog','/api/v1/catalog/export'):
            params=parse_qs(route.query);q=[x[:120] for x in params.get('queries',params.get('query',['']))[:3]];z=params.get('zip',[None])[0];retailer=params.get('retailer',[None])[0];allow=params.get('allow_unverified_location',['false'])[0]=='true'
            discovery=INGESTOR.observe(z,q,retailer or '') if INGESTOR and route.path=='/api/v1/catalog' and z else None
            items=CATALOG.search(q,z,retailer,limit=5000 if route.path.endswith('/export') else 100)
            for i in items:i['selectable']=i['price_fresh'] and (i.get('location_verified',False) or allow)
            fresh=[i for i in items if i['selectable']]
            location={'requested_zip':z,'observed_zip':None,'verified':bool(fresh) and all(i.get('location_verified') for i in fresh)}
            sid=str(uuid.uuid4())
            payload={'search_id':sid,'status':'catalog','discovery':discovery,'items':items,'location':location,'catalog':CATALOG.counts(),'cache':{'hit':True,'age_seconds':max([i['age_seconds'] for i in items],default=0),'expires_in':min([i['price_expires_in'] for i in fresh],default=0)},'warnings':['Stored catalog; inspect price_fresh. Requested ZIP is not a verified store location.']}
            if fresh:
                RESULTS[sid]={'created':time.time(),'expires':time.time()+min(i['price_expires_in'] for i in fresh),'payload':{**payload,'items':fresh}}
            return self.send(200,payload)
        if self.path=='/health': return self.send(200,{'status':'ok','version':'0.1.0'})
        if self.path=='/openapi.json': return self.send(200,SPEC)
        if self.path not in ('/','/docs'): return self.send(404,{'error':'not_found'})
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write((ROOT/'middleware.html').read_bytes())
    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=10000: raise ValueError('Request body must be 1–10000 bytes.')
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict): raise ValueError('Expected a JSON object.')
            now=time.time()
            while len(RESULTS)>=200: RESULTS.pop(next(iter(RESULTS)))
            for key in list(RESULTS):
                if now>=RESULTS[key].get('expires',RESULTS[key]['created']+300): RESULTS.pop(key,None)
            if self.path=='/api/v1/cache/invalidate':
                retailer=data.get('retailer');z=data.get('zip')
                if retailer not in ('aldi','*') or (z is not None and (not isinstance(z,str) or not re.fullmatch(r'\d{5}',z))): raise ValueError('Supply retailer aldi or *, and optional five-digit zip.')
                group=(retailer,z)
                if now-INVALIDATIONS.get(group,0)<60:return self.send(429,{'error':'rate_limited','message':'Store invalidation is limited to once per minute.'})
                INVALIDATIONS[group]=now
                removed=CACHE.invalidate(retailer,z)
                CATALOG.invalidate(retailer,z)
                expired=0
                for sid,result in list(RESULTS.items()):
                    payload=result['payload']
                    if (z is None or payload['location']['requested_zip']==z) and (retailer=='*' or any(p.get('retailer')==retailer for p in payload['items'])):
                        RESULTS.pop(sid,None);expired+=1
                return self.send(200,{'invalidated_searches':removed,'expired_price_sets':expired,'retailer':retailer,'zip':z,'message':'Next matching search will fetch fresh provider results.'})
            if self.path=='/api/v1/cart':
                lines=data.get('lines',[])
                if not isinstance(lines,list) or len(lines)>60: raise ValueError('Cart supports up to 60 lines.')
                selected=[]; seen=set()
                for line in lines:
                    if not isinstance(line,dict): raise ValueError('Invalid cart line.')
                    sid=line.get('search_id'); item_id=line.get('item_id'); qty=line.get('quantity')
                    if not isinstance(sid,str) or not isinstance(item_id,str) or type(qty) is not int or not 1<=qty<=99: raise ValueError('Invalid product or quantity.')
                    key=(sid,item_id)
                    if key in seen: raise ValueError('Duplicate cart line.')
                    seen.add(key)
                    result=RESULTS.get(sid)
                    if not result: return self.send(409,{'error':'price_expired','message':'One or more product searches expired. Refresh the affected search before validating the cart.'})
                    item=next((i for i in result['payload']['items'] if i['id']==item_id),None)
                    if not item: raise ValueError('Unknown item.')
                    selected.append({**item,'search_id':sid,'quantity':qty,'line_total':round(item['price']*qty,2),'location_verified':result['payload']['location']['verified']})
                return self.send(200,{'items':selected,'item_count':sum(i['quantity'] for i in selected),'subtotal':round(sum(i['line_total'] for i in selected),2),'currency':'USD','cart_updated':False,'provider_cart_updated':False,'location_verified':all(i['location_verified'] for i in selected) if selected else False,'message':'Local app cart validated. Taxes, fees, and provider cart are not included.'})
            if self.path=='/api/v1/select':
                result=RESULTS.get(data.get('search_id',''))
                if not result: return self.send(404,{'error':'search_expired','message':'Search again; results expire after five minutes.'})
                ids=data.get('item_ids',[])
                if not isinstance(ids,list) or not ids or len(ids)>60 or any(not isinstance(i,str) for i in ids): raise ValueError('item_ids must be a nonempty array of IDs.')
                products={p['id']:p for p in result['payload']['items']}
                if any(i not in products for i in ids): raise ValueError('Unknown product ID for this search.')
                selected=[products[i] for i in dict.fromkeys(ids)]
                return self.send(200,{'selection_id':str(uuid.uuid4()),'items':selected,'subtotal':round(sum(p['price'] for p in selected),2),'currency':'USD','cart_updated':False,'location':result['payload']['location'],'message':'Selection only. Provider cart is unchanged.'})
            if self.path!='/api/v1/search': return self.send(404,{'error':'not_found'})
            z,qs,store,flag=validate(data)
            if INGESTOR:INGESTOR.observe(z,qs,store)
            if os.getenv('CONNECTOR_ENABLED','false').lower()!='true': return self.send(503,{'error':'connector_disabled','message':'Browser connector is not enabled on this instance.','items':[]})
            if len(RATE)>1000: RATE.clear()
            client=hashlib.sha256(self.client_address[0].encode()).hexdigest()
            RATE[client]=[t for t in RATE.get(client,[]) if now-t<60]
            if len(RATE[client])>=3: return self.send(429,{'error':'rate_limited','message':'Limit: three searches per minute.'})
            parts=[];missing=[];hit_queries=[]
            for q in qs:
                entry=CACHE.get(CACHE.key(z,[q],store,flag))
                if entry: parts.append(entry);hit_queries.append(q)
                else: missing.append(q)
            if not missing:
                payload,meta=merge_parts(parts,hit_queries,[])
                sid=str(uuid.uuid4());payload.update({'search_id':sid,'cache':meta,'expires_in':meta['expires_in'],'cart_updated':False,'status':'ok'})
                RESULTS[sid]={'created':now,'expires':now+meta['expires_in'],'payload':payload}
                return self.send(200,payload)
            if not LOCK.acquire(blocking=False): return self.send(429,{'error':'busy','message':'One browser search is running. Retry shortly.'})
            RATE[client].append(now)
            try:
                fresh=search_browser(z,missing,store,flag)
                parts=[entry for q in hit_queries if (entry:=CACHE.get(CACHE.key(z,[q],store,flag))) is not None]
                fresh['retrieved_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
                for q in missing:
                    normalized=' '.join(q.casefold().split())
                    subset={**fresh,'items':[i for i in fresh['items'] if ' '.join(i.get('query','').casefold().split())==normalized]}
                    if subset['items']:
                        meta=CACHE.put(CACHE.key(z,[q],store,flag),subset)
                        CATALOG.ingest(z,q,subset,meta['expires_in'])
                        if INGESTOR:INGESTOR.enqueue(q,due=time.time()+240,zip_code=z,retailer=store)
                        parts.append((subset,meta))
                if not parts: raise ConnectorError('no_results','No priced products were returned.')
                payload,meta=merge_parts(parts,hit_queries,missing)
                search_id=str(uuid.uuid4());payload.update({'search_id':search_id,'status':'ok','expires_in':meta['expires_in'],'cache':meta,'cart_updated':False})
                RESULTS[search_id]={'created':now,'expires':time.time()+meta['expires_in'],'payload':payload}
                return self.send(200,payload)
            except ConnectorError as e:
                return self.send(422 if e.status=='location_required' else 502,{'status':e.status,'message':e.message,'items':[],'location':{'requested_zip':z,'verified':False}})
            except Exception:
                return self.send(502,{'status':'connector_error','message':'The browser connector failed. No product results were substituted.','items':[]})
            finally: LOCK.release()
        except (ValueError,TypeError): return self.send(422,{'error':'invalid_request','message':'Use a five-digit zip and 1–3 food queries. See /openapi.json.'})

if __name__=='__main__':
    if os.getenv('INGEST_ENABLED','false').lower()=='true':
        INGESTOR=Ingestor(CATALOG,CACHE,LOCK,search_browser,zip_code=os.getenv('INGEST_ZIP','63129'),retailer=os.getenv('INGEST_RETAILER',''))
        INGESTOR.start()
    ThreadingHTTPServer(('0.0.0.0',int(os.getenv('PORT','3000'))),Handler).serve_forever()

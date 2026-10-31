from operator_access import ensure_credential
import hmac
import deadline
from direct_http import search_http, compare_results
from telemetry import REQUEST_ID, emit
from result_store import ResultStore
from location_context import normalize_location, set_location, context_id, current_location
from work_coordinator import PriorityLock
from decimal import Decimal
import hashlib, json, os, re, threading, time, uuid
from urllib.parse import urlsplit, parse_qs
from ingestion import Catalog, Ingestor
from provider_comparison import Comparison
from search_cache import SearchCache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from connector import ConnectorError, search_browser

ROOT = Path(__file__).parent
LOCK = PriorityLock()
RESULTS = ResultStore()
RATE = {}
CACHE = SearchCache(ttl=300)
INVALIDATIONS = {}
CATALOG = Catalog(os.getenv('CATALOG_PATH',str(Path(os.getenv('RENDER_DISK_MOUNT_PATH','/tmp'))/'alambda-grocery-catalog.sqlite')))
CACHE.attach(CATALOG,'instacart')
OPERATOR_KEY=ensure_credential(CATALOG.path)
INGESTOR = None
COMPARISON = Comparison(CATALOG,CACHE,LOCK,lambda *a: search_browser(*a))

def validate(data):
    if not isinstance(data, dict): raise ValueError('Expected a JSON object.')
    if set(data)-{'zip','location','queries','query','retailer','allow_unverified_location','mode','providers'}:raise ValueError('Unknown search fields.')
    loc=normalize_location(data);set_location(loc)
    z = loc['postal_code']
    if data.get('mode','auto') not in ('auto','cache_only','refresh','catalog','live'):raise ValueError('Invalid search mode.')
    if not isinstance(z, str) or not re.fullmatch(r'\d{5}', z): raise ValueError('zip must contain five digits.')
    qs = data.get('queries', [data['query']] if 'query' in data else [])
    if not isinstance(qs, list) or not 1 <= len(qs) <= 3 or any(not isinstance(q, str) or not q.strip() or len(q) > 120 for q in qs): raise ValueError('Send query or queries with 1–3 nonempty strings, up to 120 characters each.')
    retailer = data.get('retailer', '')
    if not isinstance(retailer,str) or (retailer and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',retailer)): raise ValueError('retailer must be empty or a valid retailer slug.')
    flag = data.get('allow_unverified_location', False)
    if type(flag) is not bool: raise ValueError('allow_unverified_location must be boolean.')
    return z, list(dict.fromkeys(q.strip() for q in qs)), retailer, flag

SPEC = json.loads((ROOT/'openapi.json').read_text())

def merge_parts(parts, hit_queries, fetched_queries):
    payloads=[p for p,m in parts];metas=[m for p,m in parts]
    products={item['id']:item for payload in payloads for item in payload['items']}
    observed={p['location'].get('observed_zip') for p in payloads}
    location=dict(payloads[0]['location'])
    location['verified']=all(p['location']['verified'] for p in payloads)
    location['observed_zip']=observed.pop() if len(observed)==1 else None
    retailer_rows={r['id']:dict(r) for p in payloads for r in p.get('retailers',[])}
    for r in retailer_rows.values():r['products_returned']=sum(i.get('retailer')==r['id'] for i in products.values())
    meta={'hit':not fetched_queries,'partial_hit':bool(hit_queries and fetched_queries),'hit_queries':hit_queries,'fetched_queries':fetched_queries,'age_seconds':max(m['age_seconds'] for m in metas),'expires_in':min(m['expires_in'] for m in metas),'ttl_seconds':300,'sources':sorted({m.get('source','memory_cache' if m.get('hit') else 'live') for m in metas})}
    return {'items':list(products.values()),'location':location,'warnings':list(dict.fromkeys(w for p in payloads for w in p.get('warnings',[]))),'retrieved_at':min(p.get('retrieved_at','') for p in payloads),'retailers':list(retailer_rows.values()),'diagnostics':[d for p in payloads for d in p.get('diagnostics',[])]},meta

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def handle_one_request(self):
        token=REQUEST_ID.set(uuid.uuid4().hex)
        budget=deadline.begin(120)
        self.request_started=time.monotonic()
        try:return super().handle_one_request()
        finally:
            deadline.reset(budget);REQUEST_ID.reset(token)
    def send(self,code,data):
        emit('api_response',method=getattr(self,'command',''),path=urlsplit(self.path).path,http_status=code,status=data.get('status',data.get('error','ok')),duration_ms=round((time.monotonic()-getattr(self,'request_started',time.monotonic()))*1000))
        raw=json.dumps(data).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Request-ID',REQUEST_ID.get() or uuid.uuid4().hex); self.end_headers();
        try:self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):emit('client_disconnected',status='response_abandoned')
    def do_GET(self):
        route=urlsplit(self.path)
        if route.path=='/api/v1/providers/status':return self.send(200,COMPARISON.status())
        if route.path=='/api/v1/ingestion/status':
            params=parse_qs(route.query)
            try:offset=max(0,int(params.get('offset',['0'])[0]));limit=min(100,max(1,int(params.get('limit',['50'])[0])))
            except ValueError:return self.send(422,{'error':'invalid_request','message':'Invalid pagination'})
            return self.send(200,INGESTOR.status(offset,limit) if INGESTOR else {'enabled':False,'catalog':CATALOG.counts()})
        if route.path in ('/api/v1/catalog','/api/v1/catalog/export'):
            params=parse_qs(route.query);q=[x[:120] for x in params.get('queries',params.get('query',['']))[:3]];z=params.get('zip',[None])[0];retailer=params.get('retailer',[None])[0];allow=params.get('allow_unverified_location',['false'])[0]=='true'
            discovery=None
            if z is not None and not re.fullmatch(r'\d{5}',z):return self.send(422,{'error':'invalid_request','message':'Invalid ZIP'})
            set_location({'postal_code':z,'country_code':'US'})
            provider=params.get('provider',['instacart'])[0]
            if provider not in ('instacart','mealme_satsuma','vercel_kroger','vercel_square','all'):return self.send(422,{'error':'invalid_provider'})
            items=CATALOG.search(q,z,retailer,provider=None if provider=='all' else provider,limit=5000 if route.path.endswith('/export') else 100)
            for i in items:i['selectable']=i.get('provider','instacart')=='instacart' and isinstance(i.get('price'),(int,float)) and i['price_fresh'] and i.get('context_compatible',False) and (i.get('location_verified',False) or allow)
            fresh=[i for i in items if i['selectable']]
            observed={i.get('observed_zip') for i in items}
            location={'requested_zip':z,'observed_zip':next(iter(observed)) if len(observed)==1 else None,'verified':bool(fresh) and all(i.get('location_verified') for i in fresh),'verification_scope':'zip_only','delivery_serviceability_verified':False,'store_branch_verified':False}
            sid=str(uuid.uuid4())
            payload={'search_id':sid,'status':'catalog','discovery':discovery,'items':items,'location':location,'catalog':CATALOG.counts(),'cache':{'hit':bool(fresh),'source':'catalog_discovery','age_seconds':max([i['age_seconds'] for i in items],default=0),'expires_in':min([i['price_expires_in'] for i in fresh],default=0)},'warnings':['Stored catalog; inspect price_fresh. Requested ZIP is not a verified store location.']}
            if fresh:
                RESULTS[sid]={'created':time.time(),'expires':time.time()+min(i['price_expires_in'] for i in fresh),'payload':{**payload,'items':fresh}}
            return self.send(200,payload)
        if self.path=='/ready':
            try:
                with CATALOG.db() as db:db.execute('SELECT 1').fetchone()
                return self.send(200,{'status':'ready','storage_available':Path(CATALOG.path).exists(),'persistent_mount_configured':bool(os.getenv('RENDER_DISK_MOUNT_PATH'))})
            except Exception:return self.send(503,{'status':'not_ready','message':'Catalog storage unavailable'})
        if self.path=='/health': return self.send(200,{'status':'ok','version':'0.5.1'})
        if self.path=='/openapi.json': return self.send(200,SPEC)
        if route.path in ('/docs','/swagger','/android-contract.md','/instasearch-contract.md'):
            filename='android-contract.md' if route.path in ('/android-contract.md','/instasearch-contract.md') else 'swagger.html'
            self.send_response(200); self.send_header('Content-Type','text/markdown; charset=utf-8' if filename.endswith('.md') else 'text/html; charset=utf-8'); self.end_headers(); self.wfile.write((ROOT/filename).read_bytes()); return
        if self.path not in ('/',): return self.send(404,{'error':'not_found'})
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write((ROOT/'middleware.html').read_bytes())
    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=10000: raise ValueError('Request body must be 1–10000 bytes.')
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict): raise ValueError('Expected a JSON object.')
            if self.path=='/api/v1/ingestion/recover':
                supplied=self.headers.get('Authorization','')
                if not hmac.compare_digest(supplied,'Bearer '+OPERATOR_KEY):return self.send(403,{'error':'operator_required','message':'Authenticated operator access required.'})
                if data!={'acknowledge':True}:raise ValueError('Explicit acknowledgment required.')
                if not INGESTOR:return self.send(409,{'error':'worker_disabled'})
                try:return self.send(200,INGESTOR.recover())
                except ConnectorError as e:return self.send(409,{'status':e.status,'message':e.message})
            if self.path=='/api/v1/diagnostics/http-compare':
                browser_mode=data.pop('browser_mode',data.get('mode','auto'))
                if browser_mode not in ('auto','refresh','cache_only'):raise ValueError('Invalid browser_mode')
                z,qs,store,flag=validate(data)
                if len(qs)!=1:return self.send(422,{'error':'invalid_request','message':'Comparison accepts exactly one query.'})
                client=self.client_address[0];now=time.time()
                RATE[client]=[t for t in RATE.get(client,[]) if now-t<60]
                if len(RATE[client])>=3:return self.send(429,{'error':'rate_limited','message':'Three diagnostic/live searches per minute.'})
                RATE[client].append(now)
                direct=search_http(z,qs[0],store)
                # Stop on explicit screening; do not use browser fallback to evade it.
                if direct['status']=='provider_blocked':
                    return self.send(200,{'status':'provider_blocked','direct_http':direct,'browser':None,'normal_search_changed':False})
                browser=COMPARISON.one('instacart',z,qs,store,flag,browser_mode)
                return self.send(200,{'status':'compared','browser_mode':browser_mode,'direct_local_supported':False,'summary':compare_results(direct,browser),'direct_http':direct,'browser':browser,'normal_search_changed':False})

            now=time.time()
            RESULTS.prune()
            if self.path=='/api/v1/compare':
                z,qs,store,flag=validate(data)
                mode=data.get('mode','catalog');providers=data.get('providers',['instacart','mealme_satsuma'])
                if mode not in ('catalog','live','auto','cache_only','refresh') or not isinstance(providers,list) or not providers or len(providers)>4 or any(p not in ('instacart','mealme_satsuma','vercel_kroger','vercel_square') for p in providers):raise ValueError('Invalid comparison options.')
                if mode not in ('catalog','cache_only'):
                    client='compare:'+hashlib.sha256(self.client_address[0].encode()).hexdigest()
                    if len(RATE)>1000:RATE.clear()
                    RATE[client]=[t for t in RATE.get(client,[]) if now-t<60]
                    if len(RATE[client])>=3:return self.send(429,{'error':'rate_limited','message':'Three live comparisons per minute.'})
                    RATE[client].append(now)
                if INGESTOR and mode not in ('catalog','cache_only') and 'instacart' in providers and not current_location(z).get('address') and not current_location(z).get('coordinates'):INGESTOR.observe(z,qs,store)
                return self.send(200,COMPARISON.compare(z,qs,store,flag,mode,list(dict.fromkeys(providers))))
            if self.path=='/api/v1/cache/invalidate':
                retailer=data.get('retailer');z=data.get('zip')
                if not isinstance(retailer,str) or not (retailer=='*' or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',retailer)) or (z is not None and (not isinstance(z,str) or not re.fullmatch(r'\d{5}',z))): raise ValueError('Supply a retailer slug or *, and optional five-digit zip.')
                group=(retailer,z)
                if now-INVALIDATIONS.get(group,0)<60:return self.send(429,{'error':'rate_limited','message':'Store invalidation is limited to once per minute.'})
                INVALIDATIONS[group]=now
                removed=CACHE.invalidate(retailer,z)+COMPARISON.scache.invalidate(retailer,z)+sum(c.invalidate(retailer,z) for c in COMPARISON.vcaches.values())
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
                    if not result or time.time()>=result.get('expires',result['created']+300): return self.send(409,{'error':'price_expired','message':'One or more product searches expired. Refresh the affected search before validating the cart.'})
                    item=next((i for i in result['payload']['items'] if i['id']==item_id),None)
                    if not item: raise ValueError('Unknown item.')
                    selected.append({**item,'search_id':sid,'quantity':qty,'line_total':float((Decimal(str(item['price']))*qty).quantize(Decimal('0.01'))),'location_verified':result['payload']['location']['verified']})
                contexts={RESULTS.get(i['search_id'])['payload'].get('location',{}).get('context_id') or 'zip:'+str(RESULTS.get(i['search_id'])['payload'].get('location',{}).get('requested_zip')) for i in selected}
                if len(contexts)>1:return self.send(409,{'error':'mixed_location_context','message':'Keep cart offers in one location context. Refresh the affected searches.'})
                if INGESTOR:
                    for item in selected:
                        location=RESULTS.get(item['search_id'])['payload'].get('location',{})
                        if location.get('requested_zip') and item.get('query'):INGESTOR.observe(location['requested_zip'],[item['query']],item.get('retailer',''))
                return self.send(200,{'items':selected,'item_count':sum(i['quantity'] for i in selected),'subtotal':float(sum((Decimal(str(i['line_total'])) for i in selected),Decimal('0')).quantize(Decimal('0.01'))),'currency':'USD','cart_updated':False,'provider_cart_updated':False,'location_verified':all(i['location_verified'] for i in selected) if selected else False,'message':'Local app cart validated. Taxes, fees, and provider cart are not included.'})
            if self.path=='/api/v1/select':
                result=RESULTS.get(data.get('search_id',''))
                if not result or time.time()>=result.get('expires',result['created']+300): return self.send(404,{'error':'search_expired','message':'Search again; results expire after five minutes.'})
                ids=data.get('item_ids',[])
                if not isinstance(ids,list) or not ids or len(ids)>60 or any(not isinstance(i,str) for i in ids): raise ValueError('item_ids must be a nonempty array of IDs.')
                products={p['id']:p for p in result['payload']['items']}
                if any(i not in products for i in ids): raise ValueError('Unknown product ID for this search.')
                selected=[products[i] for i in dict.fromkeys(ids)]
                return self.send(200,{'selection_id':str(uuid.uuid4()),'items':selected,'subtotal':float(sum((Decimal(str(p['price'])) for p in selected),Decimal('0')).quantize(Decimal('0.01'))),'currency':'USD','cart_updated':False,'location':result['payload']['location'],'message':'Selection only. Provider cart is unchanged.'})
            if self.path!='/api/v1/search': return self.send(404,{'error':'not_found'})
            if 'providers' in data:return self.send(422,{'error':'invalid_request','message':'Use /api/v1/compare to select providers.'})
            z,qs,store,flag=validate(data)
            if INGESTOR and data.get('mode')!='cache_only' and not current_location(z).get('address') and not current_location(z).get('coordinates'):INGESTOR.observe(z,qs,store)
            if os.getenv('CONNECTOR_ENABLED','false').lower()!='true': return self.send(503,{'error':'connector_disabled','message':'Browser connector is not enabled on this instance.','items':[]})
            if len(RATE)>1000: RATE.clear()
            client=hashlib.sha256(self.client_address[0].encode()).hexdigest()
            RATE[client]=[t for t in RATE.get(client,[]) if now-t<60]
            mode=data.get('mode','auto')
            parts=[];missing=[];hit_queries=[]
            for q in qs:
                entry=None if mode=='refresh' else CACHE.get(CACHE.key(z,[q],store,flag))
                if entry: parts.append(entry);hit_queries.append(q)
                else: missing.append(q)
            if not missing:
                payload,meta=merge_parts(parts,hit_queries,[])
                sid=str(uuid.uuid4());payload.update({'search_id':sid,'cache':meta,'expires_in':meta['expires_in'],'cart_updated':False,'status':'ok'})
                RESULTS[sid]={'created':now,'expires':now+meta['expires_in'],'payload':payload}
                return self.send(200,payload)
            if missing and mode=='cache_only':
                if parts:
                    payload,meta=merge_parts(parts,hit_queries,[]);meta.update({'hit':False,'partial_hit':True})
                    sid=str(uuid.uuid4());payload.update({'search_id':sid,'status':'partial','missing_queries':missing,'cache':meta})
                    RESULTS[sid]={'created':time.time(),'expires':time.time()+meta['expires_in'],'payload':payload}
                    return self.send(200,payload)
                return self.send(200,{'status':'cache_miss','items':[],'missing_queries':missing,'location':{'requested_zip':z,'verified':False},'cache':{'hit':False,'sources':[]}})
            if len(RATE[client])>=3:return self.send(429,{'error':'rate_limited','message':'Limit: three live searches per minute.'})
            queued_at=time.monotonic()
            if not LOCK.acquire(timeout=deadline.bounded_seconds(15)): return self.send(429,{'error':'busy','message':'One browser search is running. Retry shortly.'})
            emit('browser_queue',provider='instacart',duration_ms=round((time.monotonic()-queued_at)*1000))
            RATE[client].append(now)
            try:
                # A waiting caller can reuse work completed by the previous owner.
                if mode!='refresh':
                    for q in list(missing):
                        entry=CACHE.get(CACHE.key(z,[q],store,flag))
                        if entry:parts.append(entry);hit_queries.append(q);missing.remove(q)
                fresh=search_browser(z,missing,store,flag) if missing else {'items':[]}

                parts=[entry for q in hit_queries if (entry:=CACHE.get(CACHE.key(z,[q],store,flag))) is not None]
                fresh['retrieved_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
                for q in missing:
                    normalized=' '.join(q.casefold().split())
                    subset={**fresh,'items':[i for i in fresh['items'] if ' '.join(i.get('query','').casefold().split())==normalized]}
                    if subset['items']:
                        meta=CACHE.put(CACHE.key(z,[q],store,flag),subset)
                        CATALOG.ingest(z,q,subset,meta['expires_in'])
                        # Foreground demand is registered once above; refresh does not increment it.
                        parts.append((subset,meta))
                if not parts: raise ConnectorError('no_results','No priced products were returned.')
                payload,meta=merge_parts(parts,hit_queries,missing)
                covered={ ' '.join(i.get('query','').casefold().split()) for i in payload['items']}
                unresolved=[q for q in qs if ' '.join(q.casefold().split()) not in covered]
                search_id=str(uuid.uuid4());payload.update({'missing_queries':unresolved,'search_id':search_id,'status':'partial' if unresolved else 'ok','expires_in':meta['expires_in'],'cache':meta,'cart_updated':False})
                RESULTS[search_id]={'created':now,'expires':time.time()+meta['expires_in'],'payload':payload}
                return self.send(200,payload)
            except ConnectorError as e:
                emit('search_failed',provider='instacart',status=e.status)
                return self.send(422 if e.status in ('location_required','location_unsupported') else 502,{'status':e.status,'message':e.message,'items':[],'location':{'requested_zip':z,'verified':False}})
            except Exception as e:
                emit('search_failed',provider='instacart',error_type=type(e).__name__)
                return self.send(502,{'status':'connector_error','message':'The browser connector failed. No product results were substituted.','items':[]})
            finally: LOCK.release()
        except (ValueError,TypeError): return self.send(422,{'error':'invalid_request','message':'Use a five-digit zip and 1–3 food queries. See /openapi.json.'})

if __name__=='__main__':
    if os.getenv('INGEST_ENABLED','false').lower()=='true':
        INGESTOR=Ingestor(CATALOG,CACHE,LOCK,search_browser,zip_code=os.getenv('INGEST_ZIP','63129'),retailer=os.getenv('INGEST_RETAILER',''))
        INGESTOR.start()
    server=ThreadingHTTPServer(('0.0.0.0',int(os.getenv('PORT','3000'))),Handler)
    import signal
    def stop_service(*args):
        if INGESTOR:INGESTOR.stop.set()
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop_service)
    try:server.serve_forever()
    finally:server.server_close()

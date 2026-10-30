"""Provider-isolated comparison, cache, bounded read budget, and sanitized activity."""
import json, os, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from connector import ConnectorError
from search_cache import SearchCache
import satsuma_provider as satsuma

class Comparison:
    def __init__(self,catalog,instacart_cache,browser_lock,fetch_instacart):
        self.catalog,self.icache,self.browser_lock,self.fetch_instacart=catalog,instacart_cache,browser_lock,fetch_instacart
        self.scache=SearchCache(ttl=300);self.slock=threading.Lock()
        with catalog.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS provider_activity (id TEXT PRIMARY KEY, at REAL, provider TEXT, zip TEXT, outcome TEXT, http_status INTEGER, item_count INTEGER, cache_hit INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS provider_calls (id INTEGER PRIMARY KEY, at REAL, path TEXT, status INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS provider_state (provider TEXT PRIMARY KEY, retry_after REAL)')
    def activity(self,provider,z,outcome,items,status=None,cache_hit=False):
        with self.catalog.db() as db:
            db.execute('INSERT INTO provider_activity VALUES (?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,time.time(),provider,z,outcome,status,len(items),int(cache_hit)))
            db.execute('DELETE FROM provider_activity WHERE id NOT IN (SELECT id FROM provider_activity ORDER BY at DESC LIMIT 1000)')
    def request(self,path,params):
        with self.catalog.db() as db:
            now=time.time();month=time.strftime('%Y-%m',time.gmtime())
            db.execute('BEGIN IMMEDIATE')
            retry=db.execute("SELECT retry_after FROM provider_state WHERE provider='mealme_satsuma'").fetchone()
            if retry and retry[0]>now:raise satsuma.ProviderError('cooldown','MealMe retry delayed for five minutes after provider failure.')
            if not os.getenv('SATSUMA_API_KEY'):raise satsuma.ProviderError('not_configured','MealMe/Satsuma key is not configured.')
            # Persistent in SQLite, but this trial instance has ephemeral storage; reset on redeploy.
            used=db.execute("SELECT COUNT(*) FROM provider_calls WHERE strftime('%Y-%m',at,'unixepoch')=?",(month,)).fetchone()[0]
            budget=max(0,min(100,int(os.getenv('SATSUMA_READ_BUDGET','10'))))
            if used>=budget:raise satsuma.ProviderError('budget_exhausted','Local read-test budget exhausted. No upstream request sent.')
            call_id=db.execute('INSERT INTO provider_calls(at,path) VALUES (?,?)',(now,path)).lastrowid
        try:
            data=satsuma.request(path,params)
            with self.catalog.db() as db:db.execute('UPDATE provider_calls SET status=200 WHERE id=?',(call_id,))
            return data
        except satsuma.ProviderError as e:
            with self.catalog.db() as db:
                db.execute('UPDATE provider_calls SET status=? WHERE id=?',(e.http_status,call_id))
                db.execute('INSERT OR REPLACE INTO provider_state VALUES (?,?)',('mealme_satsuma',time.time()+300))
            raise
    def status(self):
        with self.catalog.db() as db:
            rows=db.execute('SELECT at,provider,zip,outcome,http_status,item_count,cache_hit FROM provider_activity ORDER BY at DESC LIMIT 50').fetchall()
            used=db.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0]
        return {'satsuma_configured':bool(os.getenv('SATSUMA_API_KEY')),'satsuma_base_url':satsuma.BASE_URL,
                'satsuma_background_ingestion':False,'read_attempts_in_database':used,
                'satsuma_read_budget':int(os.getenv('SATSUMA_READ_BUDGET','10')),
                'storage':'SQLite on current instance; ephemeral unless CATALOG_PATH is on a persistent disk',
                'activity':[dict(zip(('at','provider','zip','outcome','http_status','item_count','cache_hit'),r)) for r in rows]}
    def one(self,provider,z,queries,store,allow,mode):
        start=time.monotonic();items=[];warnings=[];cache_hits=[];location={'requested_zip':z,'verified':False}
        error=None;status='ok';upstream=None
        try:
            if mode=='catalog':
                items=self.catalog.search(queries,z,store or None,provider=provider)
                status='cached' if items else 'not_cached'
                warnings=['Stored snapshots only; no upstream request. Compare freshness and location before use.']
                cache_hits=[True]
            else:
                if provider=='mealme_satsuma' and store:
                    raise satsuma.ProviderError('unsupported_retailer','MealMe comparison uses all account-enabled merchants; ALDI filter is unsupported.')
                cache=self.icache if provider=='instacart' else self.scache
                lock=self.browser_lock if provider=='instacart' else self.slock
                for q in queries:
                    key=cache.key(z,[q],store,allow)
                    entry=cache.get(key)
                    if entry:
                        payload,meta=entry;cache_hits.append(True)
                    else:
                        if not lock.acquire(blocking=False):raise satsuma.ProviderError('busy','Provider already has a request in progress. Retry shortly.')
                        try:
                            entry=cache.get(key)
                            if entry:payload,meta=entry;cache_hits.append(True)
                            else:
                                if provider=='instacart':
                                    if os.getenv('CONNECTOR_ENABLED','false').lower()!='true':raise satsuma.ProviderError('not_configured','Instacart browser connector is disabled.')
                                    payload=self.fetch_instacart(z,[q],store,allow)
                                    payload['provider']='instacart'
                                    payload['items']=[{**p,'provider':'instacart'} for p in payload['items']]
                                else:payload=satsuma.search(z,[q],self.request)
                                payload.setdefault('retrieved_at',time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
                                meta=cache.put(key,payload);cache_hits.append(False)
                                self.catalog.ingest(z,q,payload,meta['expires_in'])
                        finally:lock.release()
                    location=payload['location'];warnings.extend(payload.get('warnings',[]))
                    items.extend({**i,'provider':provider,'age_seconds':meta['age_seconds'],'price_expires_in':meta['expires_in'],
                                  'price_fresh':meta['expires_in']>0,'retrieved_at':payload.get('retrieved_at'),
                                  'location_verified':payload['location']['verified']} for i in payload['items'])
                status='ok' if items else 'empty'
        except (satsuma.ProviderError,ConnectorError) as e:
            status='partial' if items else 'error';upstream=getattr(e,'http_status',None)
            error={'code':getattr(e,'code',getattr(e,'status','provider_error')),'message':e.message,'http_status':upstream}
        except Exception:
            status='partial' if items else 'error';error={'code':'provider_error','message':'Provider lookup failed; no sample results substituted.','http_status':None}
        items=list({i['id']:i for i in items}.values())
        # Comparison is observational; existing Instacart cart flow remains separate.
        for item in items:item['selectable']=False
        self.activity(provider,z,status,items,upstream, bool(cache_hits) and all(cache_hits))
        return {'provider':provider,'status':status,'error':error,'items':items,'location':location,
                'warnings':list(dict.fromkeys(warnings)),'cache_hit':bool(cache_hits) and all(cache_hits),
                'duration_ms':round((time.monotonic()-start)*1000),
                'metrics':{'products':len(items),'with_prices':sum(i.get('price') is not None or i.get('provider_price') is not None for i in items),
                           'with_images':sum(bool(i.get('image_url')) for i in items),'fresh':sum(i.get('price_fresh',False) for i in items),
                           'retailers':sorted({i.get('retailer','') for i in items if i.get('retailer')})}}
    def compare(self,z,queries,store,allow,mode,providers):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.one,p,z,queries,store,allow,mode) for p in providers]
            results=[f.result() for f in futures]
        return {'comparison_id':uuid.uuid4().hex,'zip':z,'queries':queries,'mode':mode,
                'compared_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'providers':results,
                'note':'Independent provider results, not matched equivalent products or a unified checkout.'}

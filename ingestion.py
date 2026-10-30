"""Bounded warm-catalog loop for a single free test instance."""
import hashlib, json, os, sqlite3, threading, time, resource, shutil
from pathlib import Path
from connector import ConnectorError

SEEDS = ['milk','eggs','bread','chicken','bananas','rice','cheese','coffee']

class Catalog:
    def __init__(self,path):
        self.path=str(path)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS products (key TEXT PRIMARY KEY, zip TEXT, retailer TEXT, terms TEXT, seen REAL, expires REAL, payload TEXT)')
    def db(self): return sqlite3.connect(self.path,timeout=5)
    def ingest(self,zip_code,query,payload,ttl,now=None):
        now=time.time() if now is None else now
        with self.db() as db:
            for item in payload['items']:
                identity=(item.get('url','').split('?')[0] or item['name']+'|'+str(item.get('size')))+'|'+item.get('retailer','')+'|'+zip_code
                key=hashlib.sha256(identity.encode()).hexdigest()
                old=db.execute('SELECT terms FROM products WHERE key=?',(key,)).fetchone()
                terms=set(json.loads(old[0])) if old else set()
                terms.add(query.casefold())
                data={**item,'requested_zip':zip_code,'location_verified':payload['location']['verified'],'catalog_key':key}
                db.execute('INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?)',(key,zip_code,item.get('retailer',''),json.dumps(sorted(terms)),now,now+ttl,json.dumps(data)))
            db.execute('DELETE FROM products WHERE key IN (SELECT key FROM products ORDER BY seen DESC LIMIT -1 OFFSET 5000)')
    def search(self,query='',zip_code=None,retailer=None,limit=100,now=None):
        now=time.time() if now is None else now
        where=[];args=[]
        if zip_code:where.append('zip=?');args.append(zip_code)
        if retailer:where.append('retailer=?');args.append(retailer)
        sql='SELECT payload,seen,expires,terms FROM products'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY seen DESC LIMIT 5000'
        output=[]
        with self.db() as db:
            for raw,seen,expires,terms in db.execute(sql,args):
                p=json.loads(raw)
                queries=query if isinstance(query,list) else [query]
                if not any(q.casefold() in (p['name']+' '+terms).casefold() for q in queries):continue
                p.update({'age_seconds':max(0,int(now-seen)),'price_fresh':now<expires,'price_expires_in':max(0,int(expires-now)),'last_seen':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(seen))})
                output.append(p)
                if len(output)>=limit:break
        return output
    def invalidate(self,retailer,zip_code=None):
        where=[];args=[]
        if retailer!='*':where.append('retailer=?');args.append(retailer)
        if zip_code:where.append('zip=?');args.append(zip_code)
        with self.db() as db:
            cursor=db.execute('UPDATE products SET expires=0'+(' WHERE '+' AND '.join(where) if where else ''),args)
            return cursor.rowcount
    def counts(self,now=None):
        now=time.time() if now is None else now
        with self.db() as db:
            total,fresh=db.execute('SELECT COUNT(*),COALESCE(SUM(expires>?),0) FROM products',(now,)).fetchone()
        return {'products':total,'fresh_prices':fresh,'stale_prices':total-fresh,'max_products':5000}

class Ingestor:
    def __init__(self,catalog,cache,browser_lock,fetch,queries=None,zip_code='63129',retailer='',clock=time.time):
        self.catalog,self.cache,self.browser_lock,self.fetch=catalog,cache,browser_lock,fetch
        self.zip,self.retailer,self.clock=zip_code,retailer,clock
        self.jobs=[{'query':q,'next_due':0,'successes':0,'failures':0,'last_status':'queued','last_success':None} for q in (queries or SEEDS)[:20]]
        self.mutex=threading.RLock();self.stop=threading.Event();self.pause_reason=None;self.active=None;self.attempts=[];self.last_error=None
    def enqueue(self,query,due=None):
        query=' '.join(query.casefold().split())[:120]
        if not query:return False
        with self.mutex:
            if any(j['query'].casefold()==query for j in self.jobs):return True
            if len(self.jobs)>=20:return False
            self.jobs.append({'query':query,'next_due':self.clock() if due is None else due,'successes':0,'failures':0,'last_status':'queued','last_success':None})
            return True
    def status(self):
        with self.mutex:
            now=self.clock()
            return {'enabled':True,'running':not self.stop.is_set() and self.pause_reason is None,'paused_reason':self.pause_reason,'active_query':self.active,'zip':self.zip,'retailer':self.retailer or 'provider default','location_verified':False,'seed_type':'curated starter terms plus requested queries; not measured retailer popularity','queue':[dict(j,refresh_in_seconds=max(0,int(j['next_due']-now))) for j in self.jobs],'last_error':self.last_error,'attempts_last_hour':len([t for t in self.attempts if now-t<3600]),'hourly_budget':120,'storage':'local SQLite; ephemeral on this free instance','resources':{'python_peak_rss_mb':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1),'finished_children_peak_rss_mb':round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024,1),'catalog_bytes':Path(self.catalog.path).stat().st_size,'free_disk_mb':round(shutil.disk_usage(Path(self.catalog.path).parent).free/1048576)},'catalog':self.catalog.counts()}
    def tick(self):
        now=self.clock()
        with self.mutex:
            if self.pause_reason or self.stop.is_set():return False
            self.attempts=[t for t in self.attempts if now-t<3600]
            if len(self.attempts)>=120:return False
            due=[j for j in self.jobs if j['next_due']<=now]
            if not due:return False
            job=min(due,key=lambda j:(j['next_due'],j['successes']))
        if not self.browser_lock.acquire(blocking=False):return False
        try:
            with self.mutex:self.active=job['query'];self.attempts.append(now);job['last_status']='fetching'
            payload=self.fetch(self.zip,[job['query']],self.retailer,True)
            stamp=self.clock();payload['retrieved_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(stamp))
            meta=self.cache.put(self.cache.key(self.zip,[job['query']],self.retailer,True),payload)
            self.catalog.ingest(self.zip,job['query'],payload,meta['expires_in'],now=stamp)
            with self.mutex:
                job.update({'next_due':stamp+240,'successes':job['successes']+1,'failures':0,'last_status':'ready','last_success':stamp});self.last_error=None
            return True
        except ConnectorError as e:
            with self.mutex:
                job['failures']+=1;job['last_status']=e.status
                job['next_due']=self.clock()+min(1800,60*(2**min(job['failures']-1,5)))
                self.last_error={'query':job['query'],'status':e.status,'message':e.message}
                if e.status in ('provider_blocked','login_required'):self.pause_reason=e.status
            return False
        except Exception:
            with self.mutex:
                job['failures']+=1;job['last_status']='worker_error';job['next_due']=self.clock()+300
                self.last_error={'query':job['query'],'status':'worker_error','message':'Catalog ingestion failed; retry delayed.'}
            return False
        finally:
            with self.mutex:self.active=None
            self.browser_lock.release()
    def run(self):
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(15)
    def start(self):
        thread=threading.Thread(target=self.run,name='catalog-ingestor',daemon=True);thread.start();return thread

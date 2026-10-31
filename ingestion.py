import deadline
from telemetry import emit
from location_context import set_location, current_location, context_id
"""Bounded warm-catalog loop for a single free test instance."""
import hashlib, json, os, sqlite3, threading, time, resource, shutil, re
from pathlib import Path
from connector import ConnectorError, matches_query

SEARCH_GROUPS = {'produce': ['bananas', 'apples', 'oranges', 'grapes', 'strawberries', 'blueberries', 'raspberries', 'blackberries', 'pineapple', 'watermelon', 'cantaloupe', 'honeydew', 'peaches', 'pears', 'plums', 'mango', 'kiwi', 'lemons', 'limes', 'avocado', 'tomatoes', 'potatoes', 'sweet potatoes', 'onions', 'garlic', 'carrots', 'celery', 'broccoli', 'cauliflower', 'spinach', 'kale', 'romaine lettuce', 'salad mix', 'cabbage', 'cucumbers', 'bell peppers', 'jalapenos', 'mushrooms', 'zucchini', 'asparagus', 'green beans', 'corn', 'peas', 'cilantro', 'parsley', 'basil', 'ginger'], 'protein': ['chicken', 'chicken breast', 'chicken thighs', 'chicken wings', 'rotisserie chicken', 'ground beef', 'steak', 'beef roast', 'pork chops', 'pork tenderloin', 'bacon', 'sausage', 'ham', 'turkey', 'ground turkey', 'deli turkey', 'salmon', 'shrimp', 'tilapia', 'cod', 'tuna', 'crab', 'tofu', 'tempeh', 'plant based meat', 'hot dogs', 'meatballs'], 'dairy_breakfast': ['milk', 'eggs', 'cheese', 'yogurt', 'whole milk', 'skim milk', 'almond milk', 'oat milk', 'soy milk', 'lactose free milk', 'butter', 'cream cheese', 'sour cream', 'heavy cream', 'half and half', 'cheddar cheese', 'mozzarella', 'parmesan', 'cottage cheese', 'greek yogurt', 'egg whites', 'oatmeal', 'cereal', 'granola', 'pancake mix', 'waffles', 'maple syrup', 'breakfast sandwiches'], 'bakery': ['bread', 'white bread', 'wheat bread', 'sourdough', 'bagels', 'english muffins', 'hamburger buns', 'hot dog buns', 'tortillas', 'pita bread', 'croissants', 'muffins', 'donuts', 'dinner rolls', 'biscuits', 'cakes', 'cookies', 'brownies'], 'pantry': ['rice', 'pasta', 'coffee', 'white rice', 'brown rice', 'jasmine rice', 'quinoa', 'spaghetti', 'macaroni', 'ramen', 'flour', 'sugar', 'brown sugar', 'salt', 'black pepper', 'olive oil', 'vegetable oil', 'vinegar', 'peanut butter', 'jelly', 'honey', 'canned beans', 'black beans', 'chickpeas', 'lentils', 'canned tomatoes', 'tomato sauce', 'pasta sauce', 'chicken broth', 'vegetable broth', 'canned soup', 'canned tuna', 'mac and cheese', 'instant potatoes', 'breadcrumbs', 'taco seasoning', 'curry powder', 'cinnamon', 'vanilla extract', 'baking powder', 'baking soda'], 'frozen_prepared': ['frozen pizza', 'frozen vegetables', 'frozen fruit', 'frozen chicken nuggets', 'frozen fries', 'ice cream', 'frozen meals', 'dumplings', 'potstickers', 'frozen burritos', 'fish sticks', 'frozen meatballs', 'tater tots', 'hash browns', 'lasagna', 'deli sandwiches', 'prepared salads', 'sushi', 'hummus', 'guacamole', 'salsa', 'potato salad', 'coleslaw'], 'snacks_drinks': ['chips', 'tortilla chips', 'pretzels', 'popcorn', 'crackers', 'nuts', 'almonds', 'cashews', 'trail mix', 'protein bars', 'granola bars', 'fruit snacks', 'chocolate', 'candy', 'beef jerky', 'dried fruit', 'applesauce', 'bottled water', 'sparkling water', 'soda', 'cola', 'lemonade', 'orange juice', 'apple juice', 'sports drinks', 'energy drinks', 'tea', 'iced tea', 'ground coffee', 'coffee beans', 'coffee pods', 'coffee creamer', 'hot chocolate', 'protein shakes'], 'condiments_diet': ['ketchup', 'mustard', 'mayonnaise', 'ranch dressing', 'barbecue sauce', 'hot sauce', 'soy sauce', 'salad dressing', 'pickles', 'olives', 'relish', 'gluten free bread', 'gluten free pasta', 'vegan cheese', 'vegan yogurt', 'low sodium soup', 'sugar free syrup', 'baby formula', 'baby food'], 'restaurant_chains': ["mcdonald's", 'burger king', "wendy's", 'taco bell', 'chick-fil-a', 'popeyes', 'kfc', "raising cane's", "zaxby's", "church's texas chicken", 'sonic', 'jack in the box', "hardee's", "carl's jr.", 'five guys', 'shake shack', 'in-n-out burger', 'whataburger', "culver's", "freddy's", "steak 'n shake", 'white castle', 'checkers', "rally's", "arby's", 'subway', "jimmy john's", "jersey mike's", 'firehouse subs', 'potbelly', 'quiznos', 'panera bread', 'chipotle', 'qdoba', "moe's southwest grill", 'del taco', 'el pollo loco', 'panda express', 'pei wei', 'noodles & company', "domino's", 'pizza hut', 'papa johns', 'little caesars', "papa murphy's", "marco's pizza", 'mod pizza', 'blaze pizza', "imo's pizza", 'st. louis bread co.', "lion's choice", 'sugarfire smoke house', 'salt + smoke', 'starbucks', "dunkin'", "scooter's coffee", 'dutch bros', 'tim hortons', 'krispy kreme', 'dairy queen', 'baskin-robbins', 'cold stone creamery', 'crumbl', 'nothing bundt cakes', 'smoothie king', 'tropical smoothie cafe', 'jamba', 'wingstop', 'buffalo wild wings', "applebee's", "chili's", 'olive garden', 'texas roadhouse', 'longhorn steakhouse', 'outback steakhouse', 'red lobster', 'red robin', 'ihop', "denny's", 'waffle house', 'cracker barrel', 'bob evans', 'the cheesecake factory', "p.f. chang's", 'first watch', 'golden corral', 'sweetgreen', 'cava', "portillo's", "mcalister's deli", "jason's deli", 'penn station', "captain d's", "long john silver's", 'boston market', 'einstein bros. bagels', "auntie anne's", 'cinnabon'], 'restaurant_foods': ['big mac', 'quarter pounder', 'mcnuggets', 'egg mcmuffin', 'whopper', 'baconator', 'crunchwrap supreme', 'chalupa', 'doritos locos tacos', 'chicken sandwich', 'spicy chicken sandwich', 'chicken tenders', 'fried chicken', 'cheeseburger', 'double cheeseburger', 'french fries', 'onion rings', 'milkshake', 'pizza', 'pepperoni pizza', 'cheese pizza', 'buffalo wings', 'boneless wings', 'tacos', 'burrito', 'burrito bowl', 'quesadilla', 'nachos', 'fajitas', 'enchiladas', 'teriyaki chicken', 'orange chicken', 'fried rice', 'lo mein', 'pad thai', 'pho', 'ramen bowl', 'poke bowl', 'sushi rolls', 'gyros', 'falafel', 'shawarma', 'mediterranean bowl', 'caesar salad', 'cobb salad', 'club sandwich', 'italian sub', 'philly cheesesteak', 'barbecue ribs', 'pulled pork', 'brisket', 'biscuits and gravy', 'pancakes', 'breakfast burrito', 'omelet', 'iced latte', 'cold brew', 'smoothie', 'frozen custard']}

RETAILER_DISCOVERY_TARGETS = {
    'st_louis_priority': ['Schnucks', 'Dierbergs Markets', "Straub's", 'ALDI', 'Walmart', 'Target', 'Costco', "Sam's Club", 'Whole Foods Market', "Trader Joe's", 'Fresh Thyme Market', 'Save A Lot', 'Ruler Foods'],
    'missouri_expansion': ['Hy-Vee', 'Price Chopper', 'Price Cutter', 'Harps Food Stores', 'Country Mart', "Woods Supermarket", "Moser's Foods", 'Gerbes', 'Food 4 Less', 'Harter House', 'Town & Country Supermarket'],
    'specialty_discovery': ['international grocery', 'Asian grocery', 'Indian grocery', 'Mexican grocery', 'African grocery', 'halal market', 'kosher market', 'butcher shop', 'seafood market', 'farmers market', 'organic grocery', 'food cooperative', 'discount grocery', 'warehouse club', 'convenience store']
}
# Candidate discovery names only: never a retailer allowlist or proof of ZIP coverage.

GROCERY_SEEDS = list(dict.fromkeys(term for group, terms in SEARCH_GROUPS.items() if not group.startswith("restaurant_") for term in terms))
RESTAURANT_DISCOVERY_TERMS = list(dict.fromkeys(SEARCH_GROUPS["restaurant_chains"] + SEARCH_GROUPS["restaurant_foods"]))
# Restaurant terms are discovery probes, never proof of a chain menu or availability.
SEEDS = list(dict.fromkeys(["milk", "eggs", "bread"] + GROCERY_SEEDS + RESTAURANT_DISCOVERY_TERMS))
MAX_JOBS = 5000
MAX_QUERIES_PER_ZIP = 1000


class Catalog:
    def __init__(self,path):
        self.path=str(path)
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        backup=Path(self.path+'.pre-v04.sqlite')
        if Path(self.path).exists() and not backup.exists():
            with sqlite3.connect(self.path) as source,sqlite3.connect(str(backup)) as target:source.backup(target)
        backup05=Path(self.path+'.pre-v05.sqlite')
        if Path(self.path).exists() and not backup05.exists():
            with sqlite3.connect(self.path) as source,sqlite3.connect(str(backup05)) as target:source.backup(target)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS products (key TEXT PRIMARY KEY, zip TEXT, retailer TEXT, terms TEXT, seen REAL, expires REAL, payload TEXT)')
            columns={r[1] for r in db.execute('PRAGMA table_info(products)')}
            if 'provider' not in columns:db.execute("ALTER TABLE products ADD COLUMN provider TEXT NOT NULL DEFAULT 'instacart'")
            db.execute('CREATE INDEX IF NOT EXISTS products_provider_zip ON products(provider,zip)')
            if 'context_id' not in columns:db.execute('ALTER TABLE products ADD COLUMN context_id TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS products_context ON products(provider,context_id,zip)')
            db.execute('CREATE TABLE IF NOT EXISTS product_descriptions (id TEXT PRIMARY KEY, provider TEXT, payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied REAL)')
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (5,?)',(time.time(),))
            db.execute('CREATE TABLE IF NOT EXISTS worker_state (id TEXT PRIMARY KEY, payload TEXT)')
    def db(self): return sqlite3.connect(self.path,timeout=5)
    def ingest(self,zip_code,query,payload,ttl,now=None):
        now=time.time() if now is None else now
        with self.db() as db:
            for item in payload['items']:
                provider=item.get('provider',payload.get('provider','instacart'))
                identity=((item.get('url') or '').split('?')[0] or item['name']+'|'+str(item.get('size')))+'|'+item.get('retailer','')+'|'+zip_code
                if provider!='instacart':identity=provider+'|'+str(item.get('provider_location_id'))+'|'+item['id']+'|'+zip_code
                location=payload.get('location',{})
                ctx=location.get('context_id')
                fulfillment=payload.get('fulfillment','unknown')
                key=hashlib.sha256((identity+'|'+str(ctx or 'legacy-unresolved')+'|'+fulfillment).encode()).hexdigest()
                product_key=hashlib.sha256((provider+'|'+(item.get('url') or item.get('id','')).split('?')[0]).encode()).hexdigest()
                description={k:item.get(k) for k in ('id','name','size','image_url','url')}
                db.execute('INSERT OR REPLACE INTO product_descriptions VALUES (?,?,?)',(product_key,provider,json.dumps(description)))
                old=db.execute('SELECT terms FROM products WHERE key=?',(key,)).fetchone()
                terms=set(json.loads(old[0])) if old else set()
                terms.add(query.casefold())
                location=payload.get('location',{})
                verified=bool(location.get('verified')) and location.get('observed_zip')==zip_code
                data={**item,'observed_zip':location.get('observed_zip'),'provider_location_id':item.get('provider_location_id',location.get('provider_location_id')),'branch_verified':bool(location.get('branch_verified',location.get('store_branch_verified',False))),'delivery_verified':bool(location.get('delivery_verified',location.get('delivery_serviceability_verified',False))),'provider':provider,'context_id':ctx,'fulfillment':fulfillment,'product_key':product_key,'requested_zip':zip_code,'location_verified':verified,'catalog_key':key}
                db.execute('INSERT OR REPLACE INTO products (key,zip,retailer,terms,seen,expires,payload,provider,context_id) VALUES (?,?,?,?,?,?,?,?,?)',(key,zip_code,item.get('retailer',''),json.dumps(sorted(terms)),now,now+ttl,json.dumps(data),provider,ctx))
            db.execute('DELETE FROM product_descriptions WHERE rowid IN (SELECT rowid FROM product_descriptions ORDER BY rowid DESC LIMIT -1 OFFSET 10000)')
            db.execute('DELETE FROM products WHERE key IN (SELECT key FROM products ORDER BY seen DESC LIMIT -1 OFFSET 5000)')
    def search(self,query='',zip_code=None,retailer=None,limit=100,now=None,provider=None,context=None):
        now=time.time() if now is None else now
        where=[];args=[]
        if context:where.append('context_id=?');args.append(context)
        if provider:where.append('provider=?');args.append(provider)
        if zip_code:where.append('zip=?');args.append(zip_code)
        if retailer:where.append('retailer=?');args.append(retailer)
        sql='SELECT payload,seen,expires,terms,provider FROM products'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY seen DESC LIMIT 5000'
        output=[]
        with self.db() as db:
            for raw,seen,expires,terms,provider_name in db.execute(sql,args):
                p=json.loads(raw);p['provider']=provider_name
                p['context_compatible']=bool(p.get('context_id') and p['context_id']==context_id(current_location(zip_code or p.get('requested_zip'))))
                queries=query if isinstance(query,list) else [query]
                import re
                if query and not any(matches_query(p['name'],q) for q in queries):continue
                if query and re.search(r'\b(conditioner|shampoo|body wash|soap|lotion|lip balm|detergent|moisturizer)\b',p['name'],re.I):continue
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
            rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM products')]
        stores={(p.get('provider'),p.get('provider_location_id')) for p in rows if p.get('branch_verified') and p.get('provider_location_id')}
        return {'products':total,'fresh_prices':fresh,'stale_prices':total-fresh,'max_products':5000,
                'distinct_products':len({(p.get('provider'),p.get('retailer'),(p.get('url') or p.get('id','')).split('?')[0]) for p in rows}),
                'with_images':sum(bool(p.get('image_url') or p.get('image')) for p in rows),
                'verified_store_locations':len(stores),
                'verified_zip_codes':sorted({p['requested_zip'] for p in rows if p.get('location_verified')}),
                'observed_zip_codes':sorted({p['observed_zip'] for p in rows if p.get('observed_zip')}),
                'unverified_location_products':sum(not p.get('location_verified') for p in rows)}

    def save_state(self,key,payload):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO worker_state VALUES (?,?)',(key,json.dumps(payload)))
    def load_state(self,key):
        with self.db() as db:row=db.execute('SELECT payload FROM worker_state WHERE id=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def backup(self,destination):
        with self.db() as source,sqlite3.connect(str(destination)) as target:source.backup(target)

class Ingestor:
    def __init__(self,catalog,cache,browser_lock,fetch,queries=None,zip_code='63129',retailer='',clock=time.time):
        self.catalog,self.cache,self.browser_lock,self.fetch=catalog,cache,browser_lock,fetch
        self.zip,self.retailer,self.clock=zip_code,retailer,clock
        self.jobs=[{'query':q,'zip':zip_code,'retailer':retailer,'requests':0,'next_due':0,'successes':0,'failures':0,'last_status':'queued','last_success':None} for q in (SEEDS if queries is None else queries)[:MAX_QUERIES_PER_ZIP]]
        self.state_key='ingestor:'+zip_code+':'+retailer
        saved=self.catalog.load_state(self.state_key)
        if saved:
            self.jobs=saved.get('jobs',self.jobs)
            self.attempts=saved.get('attempts',[])
            for job in self.jobs:
                if job.get('last_status')=='fetching':job.update({'last_status':'retry_after_restart','next_due':self.clock()+60})
        else:self.attempts=[]
        self._batch=False
        self.scheduler_turn=saved.get('scheduler_turn',0) if saved else 0
        self.mutex=threading.RLock();self.stop=threading.Event();self.pause_reason=None;self.active=None;self.last_error=None
        self.circuit=(saved or {}).get('circuit');self.heartbeat=None;self.durations=(saved or {}).get('durations',[])
        if saved:
            self.pause_reason=saved.get('pause_reason')
            self.last_error=saved.get('last_error')
            if self.pause_reason and not self.last_error:
                self.last_error={'status':self.pause_reason,'message':'A previous job reported a login or screening condition. Original page evidence may be unavailable. Use authenticated operator recovery after reviewing provider session/access.'}
    def persist(self):
        if self._batch:return
        self.catalog.save_state(self.state_key,{'jobs':self.jobs,'attempts':self.attempts,'pause_reason':self.pause_reason,'last_error':self.last_error,'scheduler_turn':self.scheduler_turn,'circuit':self.circuit,'durations':self.durations[-50:]})
    def recover(self):
        with self.mutex:
            if self.pause_reason=='provider_blocked':raise ConnectorError('provider_blocked','Screening pause requires authorized provider-session recovery; anonymous retry is disabled.')
            if not self.pause_reason:return {'status':'already_running','message':'No provider pause needs recovery.'}
            scope=dict(self.circuit or {})
            if not all(k in scope for k in ('zip','query','retailer')):
                raise ConnectorError('recovery_context_required','The paused job context was not retained. Authorized provider-session setup is required; an unrelated public probe cannot clear this pause.')
        if not self.browser_lock.acquire(blocking=False):raise ConnectorError('busy','Browser is occupied; retry operator recovery later.')
        budget=deadline.begin(90)
        try:
            set_location({'postal_code':scope['zip'],'country_code':'US'})
            probe=self.fetch(scope['zip'],[scope['query']],scope['retailer'],True)
            if not probe.get('items'):raise ConnectorError('no_results','Recovery probe returned no readable products.')
            with self.mutex:
                self.pause_reason=None;self.circuit=None;self.last_error=None;self.persist()
            emit('ingestion_recovered',provider='instacart',status='probe_succeeded')
            return {'status':'recovered','probe_products':len(probe['items']),'location_verified':probe.get('location',{}).get('verified',False),'message':'Paused-job public-read probe succeeded. Background discovery resumed; no authenticated provider session was imported.'}
        finally:self.browser_lock.release();deadline.reset(budget)
    def tier(self,job):
        age=self.clock()-job.get('last_requested',0)
        if job.get('last_requested') and age<900:return 'hot'
        if job.get('requests',0)>1:return 'warm'
        return 'discovery'
    def choose_job(self,due):
        # Weighted cycle reserves discovery capacity even under ongoing demand.
        cycle=('hot','hot','warm','hot','discovery')
        desired=cycle[self.scheduler_turn % len(cycle)]
        self.scheduler_turn+=1
        pool=[j for j in due if self.tier(j)==desired] or due
        return min(pool,key=lambda j:(j.get('last_attempt',0),-j['requests'],j['next_due']))
    def enqueue(self,query,due=None,zip_code=None,retailer=None):
        query=' '.join(query.casefold().split())[:120]
        z=zip_code or self.zip;store=self.retailer if retailer is None else retailer
        if not query or not re.fullmatch(r'\d{5}',z) or (not isinstance(store,str) or (store and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',store))):return False
        with self.mutex:
            for job in self.jobs:
                if (job['zip'],job['retailer'],job['query'].casefold())==(z,store,query):
                    job['requests']+=1;job['last_requested']=self.clock();job['next_due']=min(job['next_due'],self.clock()) if due is None else due;self.persist();return True
            zips={j['zip'] for j in self.jobs}
            if z not in zips and len(zips)>=5:return False
            if len(self.jobs)>=MAX_JOBS:return False
            if sum(1 for j in self.jobs if (j['zip'],j['retailer'])==(z,store))>=MAX_QUERIES_PER_ZIP:return False
            self.jobs.append({'query':query,'zip':z,'retailer':store,'requests':1,'last_requested':self.clock(),'next_due':self.clock() if due is None else due,'successes':0,'failures':0,'last_status':'queued','last_success':None})
            self.persist()
            return True
    def observe(self,zip_code,queries,retailer=''):
        with self.mutex:
            self._batch=True
            new_zip=not any(j['zip']==zip_code for j in self.jobs)
            accepted=[q for q in queries if self.enqueue(q,zip_code=zip_code,retailer=retailer)]
            if new_zip and accepted:
                for term in SEEDS:
                    if not any(j['zip']==zip_code and j['retailer']==retailer and j['query']==term for j in self.jobs):
                        added=self.enqueue(term,zip_code=zip_code,retailer=retailer)
                        if added and self.jobs[-1]['query']==term and self.jobs[-1]['zip']==zip_code:
                            self.jobs[-1]['requests']=0
                            self.jobs[-1]['last_requested']=0
            self._batch=False
            self.persist()
            return {'accepted_queries':accepted,'rejected_queries':[q for q in queries if q not in accepted],'zip':zip_code,'retailer':retailer,'limits':{'zip_codes':5,'jobs':MAX_JOBS,'queries_per_zip_retailer':MAX_QUERIES_PER_ZIP}}
    def status(self,offset=0,limit=None):
        with self.mutex:
            now=self.clock()
            return {'enabled':True,'running':not self.stop.is_set() and self.pause_reason is None,'paused_reason':self.pause_reason,'active_query':self.active,'zip':self.zip,'retailer':self.retailer or 'provider default','location_verified':False,'seed_type':'expanded curated foods and restaurant discovery probes; not measured popularity', 'search_groups':SEARCH_GROUPS, 'retailer_discovery_targets':RETAILER_DISCOVERY_TARGETS, 'restaurant_discovery_note':'Search matches do not verify restaurant menus, ordering support, or local availability','footprint':{'zip_codes':sorted({j['zip'] for j in self.jobs}),'zip_count':len({j['zip'] for j in self.jobs}),'unique_queries':len({j['query'] for j in self.jobs}),'jobs':len(self.jobs),'max_zip_codes':5,'max_jobs':MAX_JOBS,'max_queries_per_zip_retailer':MAX_QUERIES_PER_ZIP},'scheduler':{'tiers':{tier:sum(self.tier(j)==tier for j in self.jobs) for tier in ['hot','warm','discovery']},'refresh_targets_seconds':{'hot':240,'warm':1800,'discovery':21600},'discovery_reserved_share':0.2},'queue_pagination':{'offset':offset,'limit':limit,'total':len(self.jobs),'next_offset':offset+limit if limit is not None and offset+limit<len(self.jobs) else None},'queue':[dict(j,tier=self.tier(j),refresh_in_seconds=max(0,int(j['next_due']-now))) for j in self.jobs[offset:None if limit is None else offset+limit]],'last_error':self.last_error,'circuit':self.circuit or ({'reason':self.pause_reason,'evidence':'legacy_unavailable'} if self.pause_reason else None),'heartbeat':self.heartbeat,'throughput':{'sample_jobs':len(self.durations),'mean_job_seconds':round(sum(self.durations)/len(self.durations),2) if self.durations else None,'estimated_cycle_seconds':round(len(self.jobs)*(sum(self.durations)/len(self.durations)+15)) if self.durations else None,'estimate_scope':'single browser, includes post-job delay, excludes foreground contention and failures'},'attempts_last_hour':len([t for t in self.attempts if now-t<3600]),'hourly_budget':120,'storage':{'engine':'sqlite','path':self.catalog.path,'persistent_mount_configured':bool(os.getenv('RENDER_DISK_MOUNT_PATH'))},'resources':{'python_peak_rss_mb':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1),'finished_children_peak_rss_mb':round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024,1),'catalog_bytes':Path(self.catalog.path).stat().st_size,'free_disk_mb':round(shutil.disk_usage(Path(self.catalog.path).parent).free/1048576)},'catalog':self.catalog.counts()}
    def tick(self):
        now=self.clock();self.heartbeat=now
        with self.mutex:
            if self.pause_reason or self.stop.is_set():return False
            self.attempts=[t for t in self.attempts if now-t<3600]
            if len(self.attempts)>=120:return False
            due=[j for j in self.jobs if j['next_due']<=now]
            if not due:return False
            job=self.choose_job(due)
        if not self.browser_lock.acquire(blocking=False):return False
        budget=deadline.begin(90)
        started=time.monotonic()
        try:
            with self.mutex:self.active=job['zip']+' / '+job['query'];self.attempts.append(now);job['last_status']='fetching';job['last_attempt']=now;self.persist()
            set_location({'postal_code':job['zip'],'country_code':'US'})
            payload=self.fetch(job['zip'],[job['query']],job['retailer'],True)
            stamp=self.clock();payload['retrieved_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(stamp))
            meta=self.cache.put(self.cache.key(job['zip'],[job['query']],job['retailer'],True),payload)
            self.catalog.ingest(job['zip'],job['query'],payload,meta['expires_in'],now=stamp)
            with self.mutex:
                interval={'hot':240,'warm':1800,'discovery':21600}[self.tier(job)]
                job.update({'next_due':stamp+interval,'successes':job['successes']+1,'failures':0,'last_status':'ready','last_success':stamp});self.last_error=None;self.durations.append(time.monotonic()-started);self.durations=self.durations[-50:]
            return True
        except ConnectorError as e:
            with self.mutex:
                job['failures']+=1;job['last_status']=e.status
                job['next_due']=self.clock()+min(1800,60*(2**min(job['failures']-1,5)))
                self.last_error={'query':job['query'],'status':e.status,'message':e.message}
                emit('ingestion_failed',provider='instacart',status=e.status)
                if e.status in ('provider_blocked','login_required'):
                    self.pause_reason=e.status
                    self.circuit={'reason':e.status,'paused_at':self.clock(),'scope':'instacart_public_browser','evidence':'visible_dialog_or_screening','stage':getattr(e,'stage',None),'zip':job['zip'],'query':job['query'],'retailer':job['retailer']}
                    emit('ingestion_paused',provider='instacart',pause_reason=e.status)
            return False
        except Exception:
            with self.mutex:
                job['failures']+=1;job['last_status']='worker_error';job['next_due']=self.clock()+300
                self.last_error={'query':job['query'],'status':'worker_error','message':'Catalog ingestion failed; retry delayed.'}
            return False
        finally:
            with self.mutex:self.active=None;self.persist()
            self.browser_lock.release()
            deadline.reset(budget)
    def run(self):
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(15)
    def start(self):
        thread=threading.Thread(target=self.run,name='catalog-ingestor',daemon=True);thread.start();return thread

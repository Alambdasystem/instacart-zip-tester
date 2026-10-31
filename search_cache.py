"""Thread-safe, bounded, non-sliding search cache with store-wide expiry."""
from copy import deepcopy
import threading, time, json
from location_context import current_location, context_id

class SearchCache:
    def __init__(self, ttl=300, max_entries=100, clock=time.time):
        self.ttl, self.max_entries, self.clock = ttl, max_entries, clock
        self.entries, self.groups = {}, {}
        self.lock = threading.RLock()
        self.catalog=None;self.namespace="instacart"
    def attach(self,catalog,namespace):
        self.catalog,self.namespace=catalog,namespace
        with catalog.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS search_snapshots (namespace TEXT, key TEXT, expires REAL, payload TEXT, PRIMARY KEY(namespace,key))")
            db.execute("CREATE TABLE IF NOT EXISTS cache_epochs (namespace TEXT, group_key TEXT, expires REAL, PRIMARY KEY(namespace,group_key))")
        return self
    def _persist(self,key,entry):
        if self.catalog:
            with self.catalog.db() as db:
                db.execute("INSERT OR REPLACE INTO search_snapshots VALUES (?,?,?,?)",(self.namespace,json.dumps(key),entry["expires"],json.dumps({**entry,"groups":list(entry["groups"])})))
                db.execute("DELETE FROM search_snapshots WHERE expires<=?",(self.clock(),))
                db.execute('DELETE FROM search_snapshots WHERE namespace=? AND key NOT IN (SELECT key FROM search_snapshots WHERE namespace=? ORDER BY expires DESC LIMIT ?)',(self.namespace,self.namespace,self.max_entries))
    def key(self, zip_code, queries, retailer, allow_unverified):
        return (zip_code, tuple(sorted(set(' '.join(q.casefold().split()) for q in queries))), retailer or '*', allow_unverified, 'v05.0:'+context_id(current_location(zip_code)))
    def _prune(self):
        now=self.clock()
        expired_groups={g for g,expires in self.groups.items() if expires<=now}
        for key,entry in list(self.entries.items()):
            if entry['expires']<=now or entry['groups'] & expired_groups:
                self.entries.pop(key,None)
        for g in expired_groups:self.groups.pop(g,None)
    def get(self,key):
        with self.lock:
            self._prune();entry=self.entries.get(key)
            source='memory_cache'
            if not entry and self.catalog:
                with self.catalog.db() as db:row=db.execute('SELECT payload FROM search_snapshots WHERE namespace=? AND key=? AND expires>?',(self.namespace,json.dumps(key),self.clock())).fetchone()
                if row:
                    entry=json.loads(row[0]);entry['groups']={tuple(g) for g in entry['groups']};source='persistent_cache'
                    size=len(json.dumps(entry['payload']).encode())
                    if size<=2_000_000:
                        while self.entries and (len(self.entries)>=self.max_entries or sum(v.get('bytes',0) for v in self.entries.values())+size>8_000_000):self.entries.pop(next(iter(self.entries)))
                        self.entries[key]={**entry,'bytes':size}
                        for group in entry['groups']:self.groups[group]=entry['expires']
            if not entry:return None
            return deepcopy(entry['payload']), {'hit':True,'source':source,'age_seconds':int(self.clock()-entry['created']),'expires_in':max(0,int(entry['expires']-self.clock())),'ttl_seconds':self.ttl}
    def put(self,key,payload):
        with self.lock:
            self._prune();now=self.clock()
            groups={(key[0],p.get('retailer') or key[2],key[4]) for p in payload['items']} or {(key[0],key[2],key[4])}
            if self.catalog:
                with self.catalog.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('DELETE FROM cache_epochs WHERE expires<=?',(now,))
                    for group in groups:
                        encoded=json.dumps(group)
                        row=db.execute('SELECT expires FROM cache_epochs WHERE namespace=? AND group_key=?',(self.namespace,encoded)).fetchone()
                        expiry=row[0] if row else now+self.ttl
                        db.execute('INSERT OR IGNORE INTO cache_epochs VALUES (?,?,?)',(self.namespace,encoded,expiry))
                        self.groups[group]=expiry
            for group in groups:self.groups.setdefault(group,now+self.ttl)
            expires=min(now+self.ttl,*(self.groups[g] for g in groups))
            if key not in self.entries and len(self.entries)>=self.max_entries:self.entries.pop(next(iter(self.entries)))
            encoded_size=len(json.dumps(payload).encode())
            if encoded_size>2_000_000:raise ValueError('Search snapshot exceeds cache size limit.')
            while self.entries and sum(v.get('bytes',0) for v in self.entries.values())+encoded_size>8_000_000:self.entries.pop(next(iter(self.entries)))
            self.entries[key]={'bytes':encoded_size,'payload':deepcopy(payload),'created':now,'expires':expires,'groups':groups}
            self._persist(key,self.entries[key])
            return {'hit':False,'source':'live','age_seconds':0,'expires_in':max(0,int(expires-now)),'ttl_seconds':self.ttl}
    def invalidate(self,retailer,zip_code=None):
        with self.lock:
            self._prune();removed=0
            for key,entry in list(self.entries.items()):
                if any((store==retailer or retailer=='*') and (zip_code is None or z==zip_code) for z,store,*_ in entry['groups']):
                    self.entries.pop(key,None);removed+=1
            if self.catalog:
                with self.catalog.db() as db:
                    for raw,encoded in db.execute('SELECT key,payload FROM search_snapshots WHERE namespace=?',(self.namespace,)).fetchall():
                        value=json.loads(encoded)
                        if any((retailer=='*' or store==retailer) and (zip_code is None or z==zip_code) for z,store,*_ in value['groups']):
                            db.execute('DELETE FROM search_snapshots WHERE namespace=? AND key=?',(self.namespace,raw))
                    for raw, in db.execute('SELECT group_key FROM cache_epochs WHERE namespace=?',(self.namespace,)).fetchall():
                        z,store,*_=json.loads(raw)
                        if (retailer=='*' or store==retailer) and (zip_code is None or z==zip_code):db.execute('DELETE FROM cache_epochs WHERE namespace=? AND group_key=?',(self.namespace,raw))
            for group in list(self.groups):
                if (group[1]==retailer or retailer=='*') and (zip_code is None or group[0]==zip_code):self.groups.pop(group,None)
            return removed

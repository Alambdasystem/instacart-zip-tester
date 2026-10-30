"""Thread-safe, bounded, non-sliding search cache with store-wide expiry."""
from copy import deepcopy
import threading, time

class SearchCache:
    def __init__(self, ttl=300, max_entries=100, clock=time.time):
        self.ttl, self.max_entries, self.clock = ttl, max_entries, clock
        self.entries, self.groups = {}, {}
        self.lock = threading.RLock()
    def key(self, zip_code, queries, retailer, allow_unverified):
        return (zip_code, tuple(sorted(set(' '.join(q.casefold().split()) for q in queries))), retailer or '*', allow_unverified)
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
            if not entry:return None
            return deepcopy(entry['payload']), {'hit':True,'age_seconds':int(self.clock()-entry['created']),'expires_in':max(0,int(entry['expires']-self.clock())),'ttl_seconds':self.ttl}
    def put(self,key,payload):
        with self.lock:
            self._prune();now=self.clock()
            groups={(key[0],key[2])}|{(key[0],p.get('retailer') or '*') for p in payload['items']}
            for group in groups:self.groups.setdefault(group,now+self.ttl)
            expires=min(now+self.ttl,*(self.groups[g] for g in groups))
            if len(self.entries)>=self.max_entries:self.entries.pop(next(iter(self.entries)))
            self.entries[key]={'payload':deepcopy(payload),'created':now,'expires':expires,'groups':groups}
            return {'hit':False,'age_seconds':0,'expires_in':max(0,int(expires-now)),'ttl_seconds':self.ttl}
    def invalidate(self,retailer,zip_code=None):
        with self.lock:
            self._prune();removed=0
            for key,entry in list(self.entries.items()):
                if any((store==retailer or retailer=='*') and (zip_code is None or z==zip_code) for z,store in entry['groups']):
                    self.entries.pop(key,None);removed+=1
            for group in list(self.groups):
                if (group[1]==retailer or retailer=='*') and (zip_code is None or group[0]==zip_code):self.groups.pop(group,None)
            return removed

"""Bounded RAM-only demand tracking. Private addresses never enter worker_state.

The existing ingestion thread owns all provider work and hourly budgets. This
planner only supplies a small admitted set; it never creates worker threads.
"""
from copy import deepcopy
import hashlib,math,threading,time,json,os
from pathlib import Path
from location_context import current_location

def policy_enabled(root):
    override=os.getenv('INSTACART_FRESHNESS_V07')
    if override is not None:return override.lower()=='true'
    path=Path(root)/'freshness-config.json'
    if not path.exists():return False
    enabled=json.loads(path.read_text()).get('enabled',False)
    if type(enabled) is not bool:raise ValueError('Freshness enabled must be boolean.')
    return enabled

class RefreshPlanner:
    def __init__(self,cache,clock=time.time,maximum=64,demand_ttl=900):
        self.cache,self.clock=cache,clock
        self.maximum,self.demand_ttl=maximum,demand_ttl
        self.lock=threading.RLock();self.entries={};self.turn=0
    def _prune(self):
        now=self.clock()
        for key,j in list(self.entries.items()):
            if now-j['last_requested']>=self.demand_ttl:self.entries.pop(key,None)
    def observe(self,z,q,store,allow,meta):
        if not meta.get('expires_in'):return
        with self.lock:
            self._prune();now=self.clock();key=self.cache.key(z,[q],store,allow)
            j=self.entries.get(key)
            if j is None:
                if len(self.entries)>=self.maximum:
                    victim=min(self.entries,key=lambda k:(self.entries[k].get('_basket_until',0)>now,self.entries[k]['last_requested']))
                    self.entries.pop(victim)
                j={'zip':z,'query':q,'retailer':store,'requests':0,'successes':0,'failures':0,'last_status':'observed','last_success':None,'_location':deepcopy(current_location(z)),'_allow':allow,'_refresh_key':key}
                self.entries[key]=j
            j['requests']+=1;j['last_requested']=now;j['_blocked']=False
            self.completed(j,meta)
    def completed(self,j,meta):
        with self.lock:
            now=self.clock();expiry=now+meta['expires_in']
            # Allow two bounded 90s reads plus the 15s tick delay and margin.
            # Deterministic jitter staggers keys without extending quote expiry.
            jitter=int(hashlib.sha256(repr(j['_refresh_key']).encode()).hexdigest()[:4],16)%16
            j['_expires']=expiry;j['next_due']=max(now,expiry-210-jitter)
    def promote(self,location,items):
        with self.lock:
            self._prune();now=self.clock()
            for j in self.entries.values():
                if j['zip']!=location.get('requested_zip'):continue
                # Provider context fingerprints are opaque; no address is exposed.
                from provider_session import request_context_id
                if location.get('context_id')!=request_context_id(j['_location']):continue
                if any(' '.join(i.get('query','').casefold().split())==' '.join(j['query'].casefold().split()) for i in items):
                    j['_basket_until']=now+self.demand_ttl;j['last_requested']=now
    @staticmethod
    def capacity(mean):
        # Reserve 40% for foreground/discovery; never admit a huge five-minute set.
        return max(1,min(8,math.floor((75+max(1,mean or 60))*.6/(max(1,mean or 60)+15))))
    def admitted(self,mean=60):
        now=self.clock();self._prune()
        return sorted((j for j in self.entries.values() if not j.get('_blocked')),key=lambda j:(j.get('_basket_until',0)>now,j['requests'],j['last_requested']),reverse=True)[:self.capacity(mean)]
    def pick(self,mean=60,blocked=None):
        with self.lock:
            now=self.clock();candidates=[j for j in self.admitted(mean) if j['next_due']<=now and not (blocked and blocked(j))]
            if not candidates:return None
            return min(candidates,key=lambda j:(j.get('_expires',0),j['next_due']))
    def failed(self,j,status):
        with self.lock:
            j['next_due']=self.clock()+min(1800,60*2**min(j.get('failures',1)-1,5))
            if status in ('login_required','provider_blocked','provider_context_changed'):j['_blocked']=True
    def status(self,mean=60):
        with self.lock:
            admitted=self.admitted(mean);now=self.clock()
            return {'enabled':True,'demand_entries':len(self.entries),'admitted':len(admitted),'capacity':self.capacity(mean),'waiting':max(0,len(self.entries)-len(admitted)),'due':sum(j['next_due']<=now for j in admitted),'demand_ttl_seconds':self.demand_ttl,'refresh_lead_seconds':[210,225],'hard_quote_ttl_seconds':300,'private_context_storage':'memory_only','scope':'Capacity target, not guaranteed freshness or delivery coverage.'}

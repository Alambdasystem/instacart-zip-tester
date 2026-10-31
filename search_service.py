"""Shared exact-snapshot resolution for foreground, comparison and ingestion."""
import time
import deadline
from connector import ConnectorError
from telemetry import emit
from provider_session import request_context_id
from location_context import current_location

def merge_parts(parts,hit_queries,fetched_queries):
    payloads=[p for p,m in parts];metas=[m for p,m in parts]
    products={i['id']:i for p in payloads for i in p['items']}
    observed={p['location'].get('observed_zip') for p in payloads}
    location=dict(payloads[0]['location']);location['verified']=all(p['location'].get('verified',False) for p in payloads)
    location['observed_zip']=next(iter(observed)) if len(observed)==1 else None
    retailers={r['id']:dict(r) for p in payloads for r in p.get('retailers',[])}
    for r in retailers.values():r['products_returned']=sum(i.get('retailer')==r['id'] for i in products.values())
    meta={'hit':not fetched_queries,'partial_hit':bool(hit_queries and fetched_queries),'hit_queries':hit_queries,'fetched_queries':fetched_queries,'age_seconds':max(m['age_seconds'] for m in metas),'expires_in':min(m['expires_in'] for m in metas),'ttl_seconds':300,'sources':sorted({m.get('source','memory_cache' if m.get('hit') else 'live') for m in metas})}
    return {'items':list(products.values()),'location':location,'warnings':list(dict.fromkeys(w for p in payloads for w in p.get('warnings',[]))),'retrieved_at':min(p.get('retrieved_at','') for p in payloads),'retailers':list(retailers.values()),'diagnostics':[d for p in payloads for d in p.get('diagnostics',[])], 'coverage_complete':False,'partial_errors':[e for p in payloads for e in p.get('partial_errors',[])]},meta

class SearchService:
    def __init__(self,catalog,cache,lock,fetch,provider='instacart'):
        self.catalog,self.cache,self.lock,self.fetch,self.provider=catalog,cache,lock,fetch,provider
    def resolve(self,z,queries,store,allow,mode='auto',before_live=None,locked=False):
        entries={};hits=[];missing=[];fetched=[];failure=None
        def compatible(payload):
            ctx=payload.get('location',{}).get('context_id')
            return self.provider!='instacart' or not ctx or ctx==request_context_id(current_location(z))
        def finish():
            parts=[];covered=set()
            for q in queries:
                if q not in entries:continue
                entry=entries[q] if entries[q][0].get('error') else self.cache.get(self.cache.key(z,[q],store,allow))
                if not entry or not compatible(entry[0]):continue
                payload,meta=entry
                meta={**meta,'source':entries[q][1].get('source',meta.get('source','live'))}
                if failure:payload={**payload,'partial_errors':[failure],'warnings':payload.get('warnings',[])+[failure['message']]}
                parts.append((payload,meta))
                if payload['items']:covered.add(q)
            return parts,[q for q in hits if q in covered],[q for q in fetched if q in covered],[q for q in queries if q not in covered]
        for q in queries:
            deadline.check();entry=None if mode=='refresh' else self.cache.get(self.cache.key(z,[q],store,allow))
            if entry and compatible(entry[0]):entries[q]=entry;hits.append(q)
            else:missing.append(q)
        if not missing or mode=='cache_only':return finish()
        if before_live:before_live()
        acquired=False
        if not locked:
            started=time.monotonic()
            if not self.lock.acquire(timeout=deadline.bounded_seconds(15)):raise ConnectorError('busy','Provider is occupied. Retry shortly.')
            acquired=True;emit('browser_queue',provider=self.provider,duration_ms=round((time.monotonic()-started)*1000))
        try:
            if mode!='refresh':
                for q in list(missing):
                    entry=self.cache.get(self.cache.key(z,[q],store,allow))
                    if entry and compatible(entry[0]):entries[q]=entry;hits.append(q);missing.remove(q)
            if missing:
                try:payload=self.fetch(z,list(missing),store,allow)
                except (ConnectorError,deadline.DeadlineExceeded) as error:
                    if not entries:raise
                    failure={'code':getattr(error,'status','provider_timeout'),'message':getattr(error,'message',str(error))}
                    return finish()
                payload.setdefault('retrieved_at',time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
                if not compatible(payload):raise ConnectorError('provider_context_changed','Provider session/context changed during search. Retry with the current context.')
                for q in missing:
                    normalized=' '.join(q.casefold().split())
                    subset={**payload,'items':[i for i in payload['items'] if len(missing)==1 or ' '.join(i.get('query','').casefold().split())==normalized]}
                    if not subset['items'] and not payload.get('error'):continue
                    meta={'hit':False,'source':'live','age_seconds':0,'expires_in':0,'ttl_seconds':300} if payload.get('error') else self.cache.put(self.cache.key(z,[q],store,allow),subset)
                    if not payload.get('error'):self.catalog.ingest(z,q,subset,meta['expires_in'])
                    entries[q]=(subset,meta);fetched.append(q)
            return finish()
        finally:
            if acquired:self.lock.release()

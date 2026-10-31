"""Bounded, capability-protected asynchronous search jobs; no persistent tokens."""
import contextvars,hashlib,hmac,secrets,threading,time,uuid
import cancellation,deadline
from connector import ConnectorError
class JobError(Exception):
    def __init__(self,code,message,http=403):self.code,self.message,self.http=code,message,http
class SearchJobs:
    def __init__(self,ttl=600,maximum=32,active_limit=2):
        self.ttl,self.maximum,self.active_limit=ttl,maximum,active_limit;self.jobs={};self.lock=threading.RLock()
    def prune(self):
        for key,j in list(self.jobs.items()):
            if j['expires']<=time.time():j['cancel'].set();del self.jobs[key]
    def submit(self,work):
        with self.lock:
            self.prune()
            if len(self.jobs)>=self.maximum or sum(j['state'] in ('queued','running') for j in self.jobs.values())>=self.active_limit:raise JobError('busy','Search job capacity is occupied. Retry shortly.',429)
            token=secrets.token_urlsafe(32);key=uuid.uuid4().hex
            j={'job_id':key,'state':'queued','created':time.time(),'started':None,'finished':None,'expires':time.time()+self.ttl,'digest':hashlib.sha256(token.encode()).digest(),'cancel':threading.Event(),'result':None,'error':None}
            self.jobs[key]=j;ctx=contextvars.copy_context()
            threading.Thread(target=ctx.run,args=(self.run,j,work),daemon=True,name='search-job').start()
            return {'job_id':key,'job_token':token,'status':'queued','expires_in':self.ttl}
    def run(self,j,work):
        budget=deadline.begin(120);cancel=cancellation.bind(j['cancel'])
        try:
            with self.lock:j['state']='running';j['started']=time.time()
            cancellation.check();result=work();cancellation.check()
            with self.lock:j['state']='succeeded';j['result']=result
        except (ConnectorError,cancellation.CancelledError,deadline.DeadlineExceeded) as e:
            code=getattr(e,'status','cancelled' if isinstance(e,cancellation.CancelledError) else 'provider_timeout')
            with self.lock:j['state']='cancelled' if code=='cancelled' else 'failed';j['error']={'code':code,'message':getattr(e,'message',str(e))}
        except Exception:
            with self.lock:j['state']='failed';j['error']={'code':'search_failed','message':'Search failed; no substitute inventory returned.'}
        finally:
            with self.lock:j['finished']=time.time()
            cancellation.reset(cancel);deadline.reset(budget)
    def access(self,key,token):
        self.prune();j=self.jobs.get(key)
        if not j or not hmac.compare_digest(j['digest'],hashlib.sha256(token.encode()).digest()):raise JobError('job_access_denied','Valid job token required.')
        return j
    def get(self,key,token):
        with self.lock:
            j=self.access(key,token)
            return {'job_id':key,'status':j['state'],'created_at':j['created'],'started_at':j['started'],'finished_at':j['finished'],'expires_in':max(0,int(j['expires']-time.time())),'cancel_requested':j['cancel'].is_set(),'result':j['result'],'error':j['error']}
    def cancel(self,key,token):
        with self.lock:
            j=self.access(key,token)
            if j['state'] in ('queued','running'):j['cancel'].set()
            return self.get(key,token)
    def stop(self):
        with self.lock:
            for j in self.jobs.values():j['cancel'].set()

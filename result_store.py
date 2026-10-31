"""Thread-safe bounded, expiring server-authoritative quote store."""
import threading,time
class ResultStore:
    def __init__(self,limit=200,clock=time.time):self.data={};self.lock=threading.RLock();self.limit=limit;self.clock=clock
    def prune(self):
        with self.lock:
            for k,v in list(self.data.items()):
                if self.clock()>=v.get('expires',v['created']+300):self.data.pop(k,None)
            while len(self.data)>self.limit:self.data.pop(next(iter(self.data)))
    def __setitem__(self,k,v):
        with self.lock:self.data[k]=v;self.prune()
    def __getitem__(self,k):
        with self.lock:self.prune();return self.data[k]
    def get(self,k,default=None):
        with self.lock:self.prune();return self.data.get(k,default)
    def clear(self):
        with self.lock:self.data.clear()
    def pop(self,k,default=None):
        with self.lock:return self.data.pop(k,default)
    def items(self):
        with self.lock:self.prune();return list(self.data.items())
    def __iter__(self):
        with self.lock:return iter(list(self.data))
    def __len__(self):
        with self.lock:return len(self.data)
    def __contains__(self,k):return self.get(k) is not None

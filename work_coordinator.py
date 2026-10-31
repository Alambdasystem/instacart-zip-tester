"""Single browser slot: queued foreground requests precede background work."""
import threading,time
import cancellation
class PriorityLock:
    def __init__(self):self.condition=threading.Condition();self.held=False;self.waiters=0
    def acquire(self,blocking=True,timeout=-1):
        with self.condition:
            if not blocking:
                if self.held or self.waiters:return False
                self.held=True;return True
            self.waiters+=1
            try:
                deadline=time.monotonic()+(90 if timeout<0 else timeout)
                while self.held:
                    cancellation.check()
                    remaining=deadline-time.monotonic()
                    if remaining<=0:return False
                    self.condition.wait(min(.1,remaining))
                self.held=True;return True
            finally:self.waiters-=1
    def release(self):
        with self.condition:self.held=False;self.condition.notify_all()
    def locked(self):
        with self.condition:return self.held

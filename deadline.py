"""One monotonic time budget, shared through copied request contexts."""
import contextvars,time
_END=contextvars.ContextVar('search_deadline',default=None)
class DeadlineExceeded(TimeoutError):pass
def begin(seconds=120):return _END.set(time.monotonic()+seconds)
def reset(token):_END.reset(token)
def remaining():
    end=_END.get()
    return float('inf') if end is None else end-time.monotonic()
def check():
    if remaining()<=0:raise DeadlineExceeded('Search deadline exhausted.')
def bounded_ms(maximum):
    check();return max(1,int(min(maximum,remaining()*1000)))
def bounded_seconds(maximum):
    check();return max(.001,min(maximum,remaining()))

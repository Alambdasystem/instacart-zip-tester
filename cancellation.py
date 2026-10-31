"""Explicit cooperative cancellation propagated through request contexts."""
import contextvars
_EVENT=contextvars.ContextVar('search_cancellation',default=None)
class CancelledError(Exception):pass
def bind(event):return _EVENT.set(event)
def reset(token):_EVENT.reset(token)
def check():
    event=_EVENT.get()
    if event is not None and event.is_set():raise CancelledError('Search cancelled.')

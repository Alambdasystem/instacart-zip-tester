"""Optional server-only authorized Instacart state; never supplied by public clients."""
import hashlib,json,os,stat
from pathlib import Path
class SessionConfigError(Exception):pass
def load_state():
    path=os.getenv('INSTACART_STORAGE_STATE_PATH')
    if not path:return None
    try:
        p=Path(path);info=p.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid!=os.geteuid() or info.st_size>1_000_000:raise ValueError()
        state=json.loads(p.read_text())
        if set(state)-{'cookies','origins'} or not isinstance(state.get('cookies',[]),list) or not isinstance(state.get('origins',[]),list):raise ValueError()
        for cookie in state.get('cookies',[]):
            domain=cookie.get('domain','').lstrip('.')
            if domain!='instacart.com' and not domain.endswith('.instacart.com'):raise ValueError()
        from urllib.parse import urlsplit
        for origin in state.get('origins',[]):
            u=urlsplit(origin.get('origin',''));host=u.hostname or ''
            if u.scheme!='https' or not (host=='instacart.com' or host.endswith('.instacart.com')):raise ValueError()
        return state
    except (OSError,ValueError,TypeError):raise SessionConfigError('Authorized provider state must be a private, owner-readable Instacart-only state file.')
def scope():
    state=load_state()
    return hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()[:20] if state is not None else 'anonymous'
def status():
    try:return {'configured':load_state() is not None,'valid_configuration':True,'authentication_verified':False}
    except SessionConfigError:return {'configured':True,'valid_configuration':False,'authentication_verified':False}
def cache_scope():
    try:return scope()
    except SessionConfigError:return 'invalid'
def request_context_id(location):
    from location_context import context_id
    value=cache_scope()
    return context_id(location) if value=='anonymous' else context_id(location)+':ic:'+value

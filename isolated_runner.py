"""Bound browser execution and stop its process tree on cancellation/deadline."""
import json,os,signal,subprocess,sys,time,threading,ctypes
# Adopt and reap cancelled browser descendants instead of leaving zombies.
_SUBREAPER=(sys.platform.startswith('linux') and ctypes.CDLL(None).prctl(36,1,0,0,0)==0)
from pathlib import Path
import cancellation,deadline
from connector import ConnectorError
from location_context import current_location
from telemetry import REQUEST_ID
_ACTIVE=set();_LOCK=threading.Lock()
def stop_tree(proc):
    # Freeze each owned process before discovering its children. Chromium/Node
    # may use different process groups, so group-only termination is insufficient.
    stopped=[]
    def identity(pid):
        try:return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]
        except OSError:return None
    def freeze(pid):
        try:
            birth=identity(pid)
            if birth is None:return
            os.kill(pid,signal.SIGSTOP);stopped.append((pid,birth))
            children=Path('/proc/'+str(pid)+'/task/'+str(pid)+'/children').read_text().split()
            for child in children:freeze(int(child))
        except (OSError,ProcessLookupError):pass
    if proc.poll() is None:
        freeze(proc.pid)
        for pid,birth in reversed(stopped):
            try:
                if identity(pid)==birth:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass
    try:proc.wait(timeout=2)
    except subprocess.TimeoutExpired:proc.kill()
    if _SUBREAPER:
        until=time.monotonic()+.5
        for pid,birth in stopped:
            if pid==proc.pid:continue
            while identity(pid)==birth and time.monotonic()<until:
                try:
                    reaped,_=os.waitpid(pid,os.WNOHANG)
                    if reaped:break
                except ChildProcessError:break
                time.sleep(.005)
def stop_all():
    with _LOCK:processes=list(_ACTIVE)
    for proc in processes:stop_tree(proc)
def execute(command,body,seconds,env=None):
    cancellation.check();deadline.check()
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,start_new_session=True,env=env)
    with _LOCK:_ACTIVE.add(proc)
    end=time.monotonic()+seconds;input_data=json.dumps(body).encode()
    try:
        while True:
            cancellation.check();deadline.check()
            if time.monotonic()>=end:raise deadline.DeadlineExceeded('Browser process deadline exhausted.')
            try:
                raw,_=proc.communicate(input=input_data,timeout=min(.1,max(.001,end-time.monotonic())))
                break
            except subprocess.TimeoutExpired:input_data=None
        cancellation.check()
        if proc.returncode or len(raw)>3_000_000:raise ConnectorError('connector_error','Isolated browser exited without a valid bounded response.')
        return json.loads(raw)
    except cancellation.CancelledError:raise ConnectorError('cancelled','Search cancelled; browser process tree stopped.')
    except deadline.DeadlineExceeded:raise ConnectorError('provider_timeout','Browser process reached the shared time limit.')
    finally:
        stop_tree(proc)
        for stream in (proc.stdin,proc.stdout):
            if stream is not None and not stream.closed:stream.close()
        with _LOCK:_ACTIVE.discard(proc)
def search_browser(z,queries,retailer='',allow=False):
    data=execute([sys.executable,str(Path(__file__).with_name('browser_worker.py'))],{'zip':z,'queries':queries,'retailer':retailer,'allow':allow,'location':current_location(z),'request_id':REQUEST_ID.get(),'seconds':min(110,deadline.remaining())},min(110,deadline.remaining()),env={k:v for k,v in os.environ.items() if k in {'PATH','HOME','LANG','LC_ALL','TZ','TMPDIR','PLAYWRIGHT_BROWSERS_PATH','CATALOG_PATH','INSTACART_STORAGE_STATE_PATH','INSTACART_PAGE_ACTION_LIMIT'}})
    if 'error' in data:raise ConnectorError(data['error']['code'],data['error']['message'])
    return data['result']

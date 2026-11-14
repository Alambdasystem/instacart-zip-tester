"""Internal JSON protocol. Provider logs go to stderr, never into result stdout."""
import contextlib,json,sys,traceback
import deadline
from connector import search_browser,ConnectorError
from location_context import set_location
from telemetry import REQUEST_ID,emit
if __name__=='__main__':
    body=json.load(sys.stdin);set_location(body['location']);REQUEST_ID.set(body.get('request_id'));deadline.begin(max(.1,body['seconds']-3))
    with contextlib.redirect_stdout(sys.stderr):
        try:value={'result':search_browser(body['zip'],body['queries'],body['retailer'],body['allow'])}
        except ConnectorError as e:value={'error':{'code':e.status,'message':e.message}}
        except Exception as error:
            frames=traceback.extract_tb(error.__traceback__)
            connector_frame=next((f for f in reversed(frames) if f.filename.endswith('/connector.py')),None)
            stage=f'connector.py:{connector_frame.lineno}' if connector_frame else 'browser_worker'
            message=str(error).lower()
            reason=next((label for fragment,label in (
                ('executable doesn','missing_executable'),
                ('no space left on device','disk_full'),
                ('resource temporarily unavailable','resource_exhausted'),
                ('permission denied','permission_denied'),
                ('missing dependencies','missing_dependencies'),
            ) if fragment in message),'unknown')
            emit('browser_worker_exception',provider='instacart',stage=stage,
                 error_type=type(error).__name__,status=reason)
            value={'error':{'code':'connector_error','message':'Isolated provider read failed.'}}
    print(json.dumps(value),flush=True)

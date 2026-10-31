"""Internal JSON protocol. Provider logs go to stderr, never into result stdout."""
import contextlib,json,sys
import deadline
from connector import search_browser,ConnectorError
from location_context import set_location
from telemetry import REQUEST_ID
if __name__=='__main__':
    body=json.load(sys.stdin);set_location(body['location']);REQUEST_ID.set(body.get('request_id'));deadline.begin(max(.1,body['seconds']-3))
    with contextlib.redirect_stdout(sys.stderr):
        try:value={'result':search_browser(body['zip'],body['queries'],body['retailer'],body['allow'])}
        except ConnectorError as e:value={'error':{'code':e.status,'message':e.message}}
        except Exception:value={'error':{'code':'connector_error','message':'Isolated provider read failed.'}}
    print(json.dumps(value),flush=True)

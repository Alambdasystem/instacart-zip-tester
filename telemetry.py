"""Allowlisted operational events: no addresses, queries, URLs or credentials."""
import contextvars,json,time,uuid
REQUEST_ID=contextvars.ContextVar('request_id',default=None)
def emit(event,**fields):
    allowed={'stage','duration_ms','status','provider','query_count','item_count','error_type','http_status','method','path','pause_reason'}
    row={'event':event,'at':round(time.time(),3),'request_id':REQUEST_ID.get()}
    row.update({k:v for k,v in fields.items() if k in allowed})
    print(json.dumps(row,separators=(',',':')),flush=True)

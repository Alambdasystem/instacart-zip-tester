"""Read-only service soak. Never issues provider searches or changes budgets."""
import argparse,json,time,urllib.request,urllib.error,statistics
from pathlib import Path

def run(base,seconds,interval,output):
    start=time.monotonic();samples=[]
    with Path(output).open('w') as log:
        while time.monotonic()-start<seconds:
            began=time.monotonic();row={'elapsed_seconds':round(began-start,2),'at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
            for name,path in [('health','/health'),('ready','/ready'),('worker','/api/v1/ingestion/status?limit=1')]:
                t=time.monotonic()
                try:
                    with urllib.request.urlopen(base.rstrip('/')+path,timeout=20) as response:
                        data=json.load(response);row[name]={'http':response.status,'seconds':round(time.monotonic()-t,3)}
                        if name=='health':row['version']=data.get('version')
                        if name=='worker':row.update({k:data.get(k) for k in ['resources','running','work_state','blocked_jobs','eligible_jobs','throughput','catalog']})
                except Exception as e:row[name]={'error':type(e).__name__,'seconds':round(time.monotonic()-t,3)}
            samples.append(row);log.write(json.dumps(row)+'\n');log.flush()
            time.sleep(min(max(0,interval-(time.monotonic()-began)),max(0,seconds-(time.monotonic()-start))))
    result={'duration_seconds':round(time.monotonic()-start,2),'samples':len(samples),'version':samples[-1].get('version') if samples else None,'errors':sum('error' in s[n] or s[n].get('http')!=200 for s in samples for n in ['health','ready','worker']),'max_endpoint_seconds':max((s[n]['seconds'] for s in samples for n in ['health','ready','worker']),default=0),'peak_python_mb':max((s.get('resources',{}).get('python_peak_rss_mb',0) for s in samples),default=0),'note':'Read-only HTTP monitoring under the existing background workload. This is not load saturation or verified local-inventory coverage.'}
    Path(output+'.summary.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--seconds',type=int,default=1800);p.add_argument('--interval',type=int,default=30);p.add_argument('--output',required=True);a=p.parse_args()
    run(a.base,max(1,min(7200,a.seconds)),max(10,a.interval),a.output)

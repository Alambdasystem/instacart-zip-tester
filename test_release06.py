import io,json,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import cancellation,deadline
from isolated_runner import execute,_ACTIVE
from connector import ConnectorError,normalize
from ingestion import Catalog,Ingestor
from search_cache import SearchCache
from search_jobs import SearchJobs,JobError
from location_context import set_location
from provider_session import load_state,scope,SessionConfigError
from pagination import expand

class Release06Tests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);set_location({'postal_code':'63129','country_code':'US'})
    def tearDown(self):set_location(None);self.tmp.cleanup()
    def item(self):return normalize([{'name':'Milk','text':'Current price: $2.99\n1 gal','url':'/products/123-milk?retailerSlug=aldi'}],'milk','aldi','63129')[0]
    def test_normalized_offer_is_authoritative_and_legacy_retained(self):
        c=Catalog(self.path/'db');c.ingest('63129','milk',{'items':[self.item()],'location':{'verified':True,'observed_zip':'63129','context_id':'zip:63129'}},300,now=1000)
        with c.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM offers').fetchone()[0],1)
            raw=json.loads(db.execute('SELECT payload FROM products').fetchone()[0]);raw['price']=999
            db.execute('UPDATE products SET payload=?',(json.dumps(raw),))
        self.assertEqual(c.search('milk','63129',now=1100)[0]['price'],2.99)
        c.invalidate('aldi','63129');self.assertFalse(c.search('milk','63129',now=1100)[0]['price_fresh'])
    def test_normalized_jobs_and_backup_restore(self):
        c=Catalog(self.path/'db');w=Ingestor(c,SearchCache(),threading.Lock(),Mock(),queries=['milk','eggs']);w.persist()
        with c.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ingestion_jobs').fetchone()[0],2)
            raw=json.loads(db.execute('SELECT payload FROM worker_state').fetchone()[0]);raw['jobs']=[]
            db.execute('UPDATE worker_state SET payload=?',(json.dumps(raw),))
        self.assertEqual(c.load_state(w.state_key)['jobs'],w.jobs)
        c.backup(self.path/'copy');copy=Catalog(self.path/'copy');self.assertEqual(copy.load_state(w.state_key)['jobs'],w.jobs)
    def test_session_file_is_private_provider_only_and_changes_cache_scope(self):
        path=self.path/'state.json';path.write_text(json.dumps({'cookies':[],'origins':[]}));path.chmod(0o600)
        with patch.dict(os.environ,{'INSTACART_STORAGE_STATE_PATH':str(path)}):
            old=scope();self.assertEqual(load_state()['cookies'],[])
            path.write_text(json.dumps({'cookies':[{'domain':'.instacart.com','name':'test','value':'synthetic'}],'origins':[]}));self.assertNotEqual(scope(),old)
            path.chmod(0o644)
            with self.assertRaises(SessionConfigError):load_state()
            path.chmod(0o600);path.write_text(json.dumps({'cookies':[{'domain':'example.com'}]}))
            with self.assertRaises(SessionConfigError):load_state()
    def test_job_tokens_isolate_poll_and_cancel(self):
        jobs=SearchJobs();hold=threading.Event()
        def work():
            while not hold.is_set():cancellation.check();time.sleep(.01)
            return {'items':[]}
        j=jobs.submit(work)
        with self.assertRaises(JobError):jobs.get(j['job_id'],'wrong-token')
        jobs.cancel(j['job_id'],j['job_token'])
        until=time.time()+2
        while jobs.get(j['job_id'],j['job_token'])['status'] in ('queued','running') and time.time()<until:time.sleep(.01)
        self.assertEqual(jobs.get(j['job_id'],j['job_token'])['status'],'cancelled')
    def test_process_deadline_is_hard_and_releases_registry(self):
        start=time.monotonic()
        with self.assertRaises(ConnectorError) as e:execute([sys.executable,'-c','import time;time.sleep(60)'],{},.15)
        self.assertEqual(e.exception.status,'provider_timeout');self.assertLess(time.monotonic()-start,3);self.assertFalse(_ACTIVE)
    def test_cancellation_reaps_descendant_process(self):
        pidfile=self.path/'pid';event=threading.Event();errors=[]
        code="import json,subprocess,sys,time;from pathlib import Path;x=json.load(sys.stdin);p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path(x['pid']).write_text(str(p.pid));time.sleep(60)"
        def run():
            token=cancellation.bind(event)
            try:execute([sys.executable,'-c',code],{'pid':str(pidfile)},10)
            except ConnectorError as e:errors.append(e.status)
            finally:cancellation.reset(token)
        t=threading.Thread(target=run);t.start();until=time.time()+3
        while not pidfile.exists() and time.time()<until:time.sleep(.01)
        self.assertTrue(pidfile.exists());pid=int(pidfile.read_text());event.set();t.join(3)
        self.assertFalse(t.is_alive());self.assertEqual(errors,['cancelled']);self.assertFalse(Path('/proc/'+str(pid)).exists())
    def test_pagination_obeys_limit_and_preserves_first_page(self):
        button=Mock();button.count.return_value=1;button.first=button;button.is_visible.return_value=True;button.is_enabled.return_value=True
        group=Mock();group.get_by_role.return_value=button
        groups=Mock();groups.count.return_value=2;groups.nth.return_value=group
        nav=Mock();nav.count.return_value=0
        page=Mock();page.locator.return_value=groups;page.get_by_role.return_value=nav
        initial={'cards':[{'url':'/1','name':'Milk'}],'retailers':[],'has_next_page':True}
        later={'cards':[{'url':'/2','name':'Milk two'}],'retailers':[],'has_next_page':True}
        state,meta=expand(page,initial,None,lambda *a,**kw:(later,True),lambda p:'63129','63129',False,'milk',maximum=1)
        self.assertEqual(len(state['cards']),2);self.assertEqual(len(meta['actions']),1);self.assertEqual(meta['stop_reason'],'action_limit');button.click.assert_called_once()
    def test_pagination_context_drift_stops_strict_read(self):
        button=Mock();button.count.return_value=1;button.first=button;button.is_visible.return_value=True;button.is_enabled.return_value=True
        group=Mock();group.get_by_role.return_value=button
        groups=Mock();groups.count.return_value=1;groups.nth.return_value=group
        nav=Mock();nav.count.return_value=0
        page=Mock();page.locator.return_value=groups;page.get_by_role.return_value=nav
        state={'cards':[],'retailers':[]}
        with self.assertRaises(ConnectorError):expand(page,state,None,lambda *a,**kw:(state,True),lambda p:'30043','63129',False,'milk')

class AsyncApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api_server as api
        cls.api=api;cls.server=api.ThreadingHTTPServer(('127.0.0.1',0),api.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start();cls.base='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.api.JOBS.stop();cls.server.shutdown();cls.server.server_close()
    def request(self,path,body=None,token=None):
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        req=Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if token else {})})
        try:r=urlopen(req,timeout=5)
        except HTTPError as e:r=e
        with r:return r.status,json.load(r)
    def setUp(self):self.api.JOBS=SearchJobs();self.api.RATE.clear();self.api.RESULTS.clear()
    def test_async_result_uses_normal_selection_contract(self):
        item=normalize([{'name':'Milk','text':'Current price: $2.99\n1 gal'}],'milk','aldi','63129')[0]
        payload={'items':[item],'location':{'verified':True,'observed_zip':'63129','requested_zip':'63129'}}
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(self.api,'search_browser',return_value=payload):
            code,j=self.request('/api/v1/search/jobs',{'zip':'63129','query':'milk','retailer':'aldi','mode':'refresh'})
            self.assertEqual(code,202);path='/api/v1/search/jobs/'+j['job_id']
            self.assertEqual(self.request(path,token='wrong')[0],403)
            for _ in range(100):
                code,job=self.request(path,token=j['job_token'])
                if job['status'] not in ('queued','running'):break
                time.sleep(.01)
        self.assertEqual(job['status'],'succeeded')
        code,selection=self.request('/api/v1/select',{'search_id':job['result']['search_id'],'item_ids':[item['id']]})
        self.assertEqual(code,200);self.assertEqual(selection['subtotal'],2.99)
    def test_cancel_endpoint_stops_work_and_releases_browser(self):
        began=threading.Event()
        def fetch(*args):
            began.set()
            while True:cancellation.check();time.sleep(.01)
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(self.api,'search_browser',side_effect=fetch):
            code,j=self.request('/api/v1/search/jobs',{'zip':'63129','query':'different milk','mode':'refresh'})
            self.assertEqual(code,202);self.assertTrue(began.wait(2))
            path='/api/v1/search/jobs/'+j['job_id'];self.assertEqual(self.request(path+'/cancel',{},'wrong')[0],403)
            self.assertEqual(self.request(path+'/cancel',{},j['job_token'])[0],200)
            for _ in range(100):
                _,job=self.request(path,token=j['job_token'])
                if job['status']=='cancelled':break
                time.sleep(.01)
        self.assertEqual(job['status'],'cancelled');self.assertFalse(self.api.LOCK.locked())

    def test_cache_only_job_does_not_fetch_and_matches_poll_model(self):
        import jsonschema
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(self.api,'search_browser') as fetch:
            code,j=self.request('/api/v1/search/jobs',{'zip':'63129','query':'uncached release six fixture','mode':'cache_only'})
            self.assertEqual(code,202)
            for _ in range(100):
                _,job=self.request('/api/v1/search/jobs/'+j['job_id'],token=j['job_token'])
                if job['status']=='succeeded':break
                time.sleep(.01)
            fetch.assert_not_called()
        self.assertEqual(job['result']['status'],'cache_miss')
        schema={**self.api.SPEC['components']['schemas']['SearchJobStatus'],'components':self.api.SPEC['components']}
        jsonschema.Draft202012Validator(schema).validate(job)

    def test_async_provider_selector_is_rejected_without_rerouting(self):
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(self.api,'search_browser') as fetch:
            code,value=self.request('/api/v1/search/jobs',{'zip':'63129','query':'milk','providers':['vercel_square']})
        self.assertEqual(code,422);self.assertEqual(value['error'],'invalid_provider');fetch.assert_not_called()

class ResolverFreshnessTests(unittest.TestCase):
    def setUp(self):set_location({'postal_code':'63129','country_code':'US'})
    def tearDown(self):set_location(None)
    def test_cached_offer_expiring_during_live_miss_is_not_reissued(self):
        from search_service import SearchService
        clock=[1000];cache=SearchCache(clock=lambda:clock[0]);catalog=Mock()
        def payload(q):return {'items':[{'id':q,'name':q,'query':q,'price':2}],'location':{'verified':False,'observed_zip':None}}
        cache.put(cache.key('63129',['milk'],'',True),payload('milk'))
        def fetch(*args):clock[0]=1400;return payload('eggs')
        entries,hits,fetched,missing=SearchService(catalog,cache,threading.Lock(),fetch).resolve('63129',['milk','eggs'],'',True)
        self.assertEqual([i['id'] for p,m in entries for i in p['items']],['eggs']);self.assertEqual(missing,['milk']);self.assertEqual(fetched,['eggs'])
    def test_upstream_failure_preserves_still_fresh_cached_part(self):
        from search_service import SearchService,merge_parts
        cache=SearchCache();p={'items':[{'id':'milk','name':'Milk','query':'milk','price':2}],'location':{'verified':False}}
        cache.put(cache.key('63129',['milk'],'',True),p)
        fetch=Mock(side_effect=ConnectorError('provider_timeout','Upstream timed out.'))
        parts,hits,fetched,missing=SearchService(Mock(),cache,threading.Lock(),fetch).resolve('63129',['milk','eggs'],'',True)
        value,meta=merge_parts(parts,hits,fetched)
        self.assertEqual(missing,['eggs']);self.assertEqual(value['partial_errors'][0]['code'],'provider_timeout');self.assertEqual(len(value['items']),1)

    def test_session_rotation_during_fetch_cannot_populate_new_scope(self):
        from search_service import SearchService
        state=['ctx:A'];cache=SearchCache()
        def fetch(*args):
            state[0]='ctx:B'
            return {'items':[{'id':'milk','name':'Milk','query':'milk','price':2}],'location':{'verified':True,'context_id':'ctx:A'}}
        with patch('search_service.request_context_id',side_effect=lambda *a:state[0]):
            with self.assertRaises(ConnectorError) as caught:SearchService(Mock(),cache,threading.Lock(),fetch).resolve('63129',['milk'],'',False)
        self.assertEqual(caught.exception.status,'provider_context_changed');self.assertFalse(cache.entries)

class OfferDetailTests(unittest.TestCase):
    def test_weighted_price_and_promotion_are_labeled(self):
        row=normalize([{'name':'Chicken per lb','text':'Current price: $2.99\n$2.99/lb\nSale today','url':'/products/1-chicken?retailerSlug=aldi'}],'chicken','aldi','63129')[0]
        self.assertEqual(row['price_basis'],'per_lb');self.assertEqual(row['unit_price']['unit'],'lb');self.assertEqual(row['promotion_text'],'Sale today');self.assertEqual(row['variants_status'],'not_exposed')
    def test_actual_branch_ids_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as d:
            c=Catalog(Path(d)/'db');item=normalize([{'name':'Milk','text':'Current price: $2.99','url':'/products/1-milk?retailerSlug=aldi'}],'milk','aldi','63129')[0]
            for branch in ('one','two'):c.ingest('63129','milk',{'items':[{**item,'provider_location_id':branch}],'location':{'context_id':'zip:63129','verified':True,'observed_zip':'63129'}},300)
            self.assertEqual(len(c.search('milk','63129')),2)

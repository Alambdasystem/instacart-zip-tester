import json, os, threading, unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import api_server as api
from connector import normalize, ConnectorError

class MiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=api.ThreadingHTTPServer(('127.0.0.1',0),api.Handler)
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls): cls.server.shutdown();cls.server.server_close()
    def post(self,path,body):
        try:r=urlopen(Request(self.url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}))
        except HTTPError as e:r=e
        return r.status,json.load(r)
    def setUp(self):
        api.RESULTS.clear();api.RATE.clear();api.CACHE.entries.clear();api.CACHE.groups.clear();api.INVALIDATIONS.clear()
        with api.CATALOG.db() as db:db.execute('DELETE FROM search_snapshots')
    def test_http_comparison_does_not_mutate_normal_search(self):
        direct={'status':'ok','items':[],'duration_ms':2,'location':{'verified':False}}
        browser={'status':'ok','items':[],'duration_ms':3,'location':{'verified':True}}
        with patch.object(api,'search_http',return_value=direct),patch.object(api.COMPARISON,'one',return_value=browser) as fetch:
            status,data=self.post('/api/v1/diagnostics/http-compare',{'zip':'63129','query':'milk'})
        self.assertEqual(status,200);self.assertFalse(data['normal_search_changed'])
        fetch.assert_called_once_with('instacart','63129',['milk'],'',False,'auto')
    def test_http_screening_does_not_fallback_to_browser(self):
        with patch.object(api,'search_http',return_value={'status':'provider_blocked','items':[]}),patch.object(api.COMPARISON,'one') as fetch:
            status,data=self.post('/api/v1/diagnostics/http-compare',{'zip':'63129','query':'milk'})
        self.assertEqual(data['status'],'provider_blocked');fetch.assert_not_called()
    def test_operator_recovery_requires_server_credential(self):
        with patch.object(api,'INGESTOR') as worker:
            status,data=self.post('/api/v1/ingestion/recover',{'acknowledge':True})
        self.assertEqual(status,403);worker.recover.assert_not_called()
    def test_explicit_comparison_mode_is_used(self):
        direct={'status':'ok','items':[],'duration_ms':2,'location':{'verified':False}}
        with patch.object(api,'search_http',return_value=direct),patch.object(api.COMPARISON,'one',return_value=direct) as fetch:
            status,data=self.post('/api/v1/diagnostics/http-compare',{'zip':'63129','query':'milk','browser_mode':'refresh'})
        self.assertEqual(status,200);self.assertEqual(fetch.call_args.args[-1],'refresh')
        self.assertEqual(data['summary']['comparability'],'not_comparable')
    def test_response_request_id_matches_log(self):
        from telemetry import REQUEST_ID
        rows=[]
        with patch.object(api,'emit',side_effect=lambda event,**fields:rows.append((REQUEST_ID.get(),fields))):
            with urlopen(self.url+'/ready') as r:
                request_id=r.headers['X-Request-ID'];self.assertEqual(r.status,200)
        self.assertEqual(rows[0][0],request_id)
        self.assertEqual(rows[0][1]['http_status'],200)
    def test_busy_wait_is_bounded_and_does_not_fetch(self):
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(api.LOCK,'acquire',return_value=False) as lock,patch.object(api,'search_browser') as fetch:
            status,data=self.post('/api/v1/search',{'zip':'63129','query':'milk'})
        self.assertEqual(status,429);self.assertEqual(data['error'],'busy')
        self.assertLessEqual(lock.call_args.kwargs['timeout'],15);fetch.assert_not_called()
    def test_price_size_normalization(self):
        rows=normalize([{'name':'Milk','text':'Milk\nCurrent price: $2.78\n1 gal'}],'milk','aldi','63129')
        self.assertEqual((rows[0]['price'],rows[0]['size']), (2.78,'1 gal'))
        self.assertEqual(normalize([{'name':'Milk','text':'No price'}],'milk','aldi',None),[])
    def test_reject_invalid_zip(self):
        self.assertEqual(self.post('/api/v1/search',{'zip':'bad','query':'milk'})[0],422)
    def test_disabled_connector(self):
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'false'}):
            self.assertEqual(self.post('/api/v1/search',{'zip':'63129','query':'milk'})[0],503)
    def test_location_failure_is_not_empty_success(self):
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(api,'search_browser',side_effect=ConnectorError('location_required','Street address required')):
            status,data=self.post('/api/v1/search',{'zip':'63129','query':'milk'})
            self.assertEqual(status,422);self.assertEqual(data['items'],[])
    def test_search_selection_and_tampered_id(self):
        item=normalize([{'name':'Milk','text':'Milk\nCurrent price: $2.78\n1 gal'}],'milk','aldi','63129')[0]
        result={'items':[item],'location':{'requested_zip':'63129','observed_zip':'63129','verified':True},'warnings':[]}
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(api,'search_browser',return_value=result):
            status,data=self.post('/api/v1/search',{'zip':'63129','queries':['milk'],'retailer':'aldi'})
        self.assertEqual(status,200)
        status,selection=self.post('/api/v1/select',{'search_id':data['search_id'],'item_ids':[item['id']]})
        self.assertEqual(status,200);self.assertEqual(selection['subtotal'],2.78);self.assertFalse(selection['cart_updated'])
        self.assertEqual(self.post('/api/v1/select',{'search_id':data['search_id'],'item_ids':['fabricated']})[0],422)
    def test_cart_quantity_and_expired_prices(self):
        item=normalize([{'name':'Milk','text':'Current price: $2.78\n1 gal'}],'milk','aldi','63129')[0]
        api.RESULTS['test-search']={'created':__import__('time').time(),'payload':{'items':[item],'location':{'verified':True}}}
        line={'search_id':'test-search','item_id':item['id'],'quantity':2}
        status,cart=self.post('/api/v1/cart',{'lines':[line]})
        self.assertEqual(status,200);self.assertEqual(cart['subtotal'],5.56);self.assertEqual(cart['item_count'],2)
        self.assertFalse(cart['provider_cart_updated'])
        self.assertEqual(self.post('/api/v1/cart',{'lines':[{**line,'quantity':-1}]})[0],422)
        api.RESULTS.clear()
        self.assertEqual(self.post('/api/v1/cart',{'lines':[line]})[0],409)
    def test_cached_search_avoids_provider_and_store_invalidation(self):
        item=normalize([{'name':'Milk','text':'Current price: $2.78\n1 gal'}],'milk','aldi','63129')[0]
        payload={'items':[item],'location':{'requested_zip':'63129','observed_zip':'63129','verified':True},'warnings':[]}
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(api,'search_browser',return_value=payload) as provider:
            status,first=self.post('/api/v1/search',{'zip':'63129','queries':['milk'],'retailer':'aldi'})
            status,second=self.post('/api/v1/search',{'zip':'63129','queries':[' MILK '],'retailer':'aldi'})
            self.assertEqual(provider.call_count,1);self.assertTrue(second['cache']['hit'])
            self.assertNotEqual(first['search_id'],second['search_id'])
            status,invalidated=self.post('/api/v1/cache/invalidate',{'zip':'63129','retailer':'aldi'})
            self.assertEqual(status,200);self.assertEqual(invalidated['invalidated_searches'],1)
            self.assertNotIn(first['search_id'],api.RESULTS)
            self.post('/api/v1/search',{'zip':'63129','query':'milk','retailer':'aldi'})
            self.assertEqual(provider.call_count,2)
    def test_changed_list_only_fetches_missing_query(self):
        def provider(z,queries,store,flag):
            items=[normalize([{'name':q,'text':'Current price: $1.00\n1 ct'}],q,store,z)[0] for q in queries]
            return {'items':items,'location':{'requested_zip':z,'observed_zip':z,'verified':True},'warnings':[]}
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(api,'search_browser',side_effect=provider) as fetch:
            self.post('/api/v1/search',{'zip':'63129','query':'milk','retailer':'aldi'})
            status,data=self.post('/api/v1/search',{'zip':'63129','queries':['milk','eggs'],'retailer':'aldi'})
            self.assertEqual(status,200);self.assertEqual(fetch.call_args.args[1],['eggs'])
            self.assertTrue(data['cache']['partial_hit']);self.assertEqual(len(data['items']),2)
    def test_unknown_location_fields_and_conflicting_zip_rejected(self):
        self.assertEqual(self.post('/api/v1/search',{'zip':'63129','query':'milk','address':'ignored'})[0],422)
        self.assertEqual(self.post('/api/v1/search',{'zip':'63129','query':'milk','location':{'postal_code':'63110'}})[0],422)
    def test_cache_only_miss_never_calls_live_and_persistent_hit_reuses(self):
        item=normalize([{'name':'Milk','text':'Current price: $2.78'}],'milk','aldi','63129')[0]
        payload={'items':[item],'location':{'requested_zip':'63129','observed_zip':'63129','verified':True},'warnings':[]}
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(api,'search_browser',return_value=payload) as fetch:
            code,d=self.post('/api/v1/search',{'zip':'63129','query':'milk','mode':'cache_only'})
            self.assertEqual(d['status'],'cache_miss');fetch.assert_not_called()
            self.post('/api/v1/search',{'zip':'63129','query':'milk'})
            api.CACHE.entries.clear();api.CACHE.groups.clear()
            code,d=self.post('/api/v1/search',{'zip':'63129','query':'milk','mode':'cache_only'})
            self.assertEqual(code,200);self.assertEqual(fetch.call_count,1);self.assertEqual(d['cache']['sources'],['persistent_cache'])
    def test_address_context_reaches_connector_and_cannot_share_zip_snapshot(self):
        from location_context import current_location
        def provider(z,queries,store,flag):
            self.assertEqual(current_location(z)['address']['line1'],'5430 Telegraph Road')
            raise ConnectorError('location_required','Provider acknowledgment required')
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(api,'search_browser',side_effect=provider):
            code,d=self.post('/api/v1/search',{'location':{'postal_code':'63129','address':{'line1':'5430 Telegraph Road','city':'St. Louis','region':'MO'}},'queries':['milk']})
            self.assertEqual(code,422);self.assertFalse(d['location']['verified'])

    def test_image_url_validation(self):
        card={'name':'Milk','text':'Current price: $2.78\n1 gal','image_url':'https://www.instacart.com/image-server/milk.jpg'}
        self.assertEqual(normalize([card],'milk','aldi',None)[0]['image_url'],card['image_url'])
        card['image_url']='javascript:alert(1)'
        self.assertIsNone(normalize([card],'milk','aldi',None)[0]['image_url'])
    def test_spec_serves(self):
        spec=json.load(urlopen(self.url+'/openapi.json'))
        self.assertIn('/api/v1/search',spec['paths'])

if __name__=='__main__':unittest.main()

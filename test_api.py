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
    def tearDownClass(cls): cls.server.shutdown()
    def post(self,path,body):
        try:r=urlopen(Request(self.url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}))
        except HTTPError as e:r=e
        return r.status,json.load(r)
    def setUp(self): api.RESULTS.clear();api.RATE.clear();api.CACHE.entries.clear();api.CACHE.groups.clear();api.INVALIDATIONS.clear()
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
    def test_image_url_validation(self):
        card={'name':'Milk','text':'Current price: $2.78\n1 gal','image_url':'https://www.instacart.com/image-server/milk.jpg'}
        self.assertEqual(normalize([card],'milk','aldi',None)[0]['image_url'],card['image_url'])
        card['image_url']='javascript:alert(1)'
        self.assertIsNone(normalize([card],'milk','aldi',None)[0]['image_url'])
    def test_spec_serves(self):
        spec=json.load(urlopen(self.url+'/openapi.json'))
        self.assertIn('/api/v1/search',spec['paths'])

if __name__=='__main__':unittest.main()

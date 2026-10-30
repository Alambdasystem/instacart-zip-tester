import json, os, tempfile, threading, unittest
from unittest.mock import patch,Mock
from ingestion import Catalog
from search_cache import SearchCache
from provider_comparison import Comparison
from satsuma_provider import ProviderError,normalize,NoRedirect,request

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.catalog=Catalog(self.temp.name+'/db.sqlite')
        self.ic=Mock(return_value={'items':[{'id':'ic1','name':'Milk','price':3.2,'retailer':'aldi','query':'milk','url':'https://www.instacart.com/products/1'}], 'location':{'requested_zip':'63129','verified':False},'warnings':[]})
        self.c=Comparison(self.catalog,SearchCache(),threading.Lock(),self.ic)
        self.sdata={'data':[{'id':'ic1','name':'Milk','merchant_id':'gopuff','price':325,'currency':'USD','image_url':'https://example.com/milk.jpg','location':{'id':'loc1'}}]}
    def tearDown(self):self.temp.cleanup()
    def test_adapter_keeps_provenance_and_unknown_units(self):
        item=normalize(self.sdata,'milk','63129')[0]
        self.assertTrue(item['id'].startswith('satsuma:'));self.assertEqual(item['provider_price'],325)
        self.assertIsNone(item['price']);self.assertFalse(item['selectable']);self.assertEqual(item['provider_location_id'],'loc1')
    def test_no_secret_redirect_and_no_writes(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None))
        with self.assertRaises(ProviderError):request('/order',{})
    def test_success_cached_and_provider_db_isolated(self):
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch('satsuma_provider.zip_coordinates',return_value=(38.4,-90.3)),patch.object(self.c,'request',return_value=self.sdata) as fetch:
            result=self.c.compare('63129',['milk'],'',True,'live',['instacart','mealme_satsuma'])
            self.assertEqual([p['status'] for p in result['providers']],['ok','ok'])
            self.c.compare('63129',['milk'],'',True,'live',['instacart','mealme_satsuma'])
            self.assertEqual(fetch.call_count,1);self.assertEqual(self.ic.call_count,1)
        self.assertEqual(len(self.catalog.search('milk',provider='instacart')),1)
        self.assertEqual(len(self.catalog.search('milk',provider='mealme_satsuma')),1)
        self.assertEqual(len(self.catalog.search('milk')),2)
    def test_503_partial_comparison_and_no_success_cache(self):
        with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch('satsuma_provider.zip_coordinates',return_value=(38.4,-90.3)),patch.object(self.c,'request',side_effect=ProviderError('service_unavailable','HTTP 503',503)):
            result=self.c.compare('63129',['milk'],'',True,'live',['instacart','mealme_satsuma'])
        self.assertEqual(result['providers'][0]['status'],'ok');self.assertEqual(result['providers'][1]['error']['http_status'],503)
        self.assertEqual(self.catalog.search(provider='mealme_satsuma'),[]);self.assertFalse(self.c.scache.entries)
    def test_cooldown_and_budget(self):
        with patch.dict(os.environ,{'SATSUMA_API_KEY':'unit-test-key','SATSUMA_READ_BUDGET':'1'}),patch('satsuma_provider.request',side_effect=ProviderError('service_unavailable','unavailable',503)) as req:
            with self.assertRaises(ProviderError):self.c.request('/product',{})
            with self.assertRaises(ProviderError) as cm:self.c.request('/product',{})
            self.assertEqual(cm.exception.code,'cooldown');self.assertEqual(req.call_count,1)
            with self.catalog.db() as db:db.execute('DELETE FROM provider_state')
            with self.assertRaises(ProviderError) as cm:self.c.request('/product',{})
            self.assertEqual(cm.exception.code,'budget_exhausted');self.assertEqual(req.call_count,1)
    def test_cached_comparison_never_hits_provider(self):
        result=self.c.compare('63129',['milk'],'',True,'catalog',['instacart','mealme_satsuma'])
        self.ic.assert_not_called();self.assertEqual([p['status'] for p in result['providers']],['not_cached','not_cached'])
    def test_error_payload_and_invalid_prices_not_facts(self):
        with self.assertRaises(ProviderError):normalize({'data':[],'messages':[{'level':'Error'}]},'milk','63129')
        self.sdata['data'][0]['price']=float('nan');self.sdata['data'][0]['image_url']='https://example.com/i?token=secret'
        item=normalize(self.sdata,'milk','63129')[0];self.assertIsNone(item['provider_price']);self.assertIsNone(item['image_url'])
    def test_old_catalog_migrates_without_losing_records(self):
        import sqlite3
        path=self.temp.name+'/old.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE products(key TEXT PRIMARY KEY,zip TEXT,retailer TEXT,terms TEXT,seen REAL,expires REAL,payload TEXT)')
            db.execute('INSERT INTO products VALUES(?,?,?,?,?,?,?)',('old','63129','aldi','["milk"]',0,1,json.dumps({'id':'old','name':'Milk','price':2})))
        old=Catalog(path);row=old.search('milk')[0];self.assertEqual(row['provider'],'instacart');self.assertEqual(row['id'],'old')

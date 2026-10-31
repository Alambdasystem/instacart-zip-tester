import json, tempfile, threading, time, unittest
from unittest.mock import Mock,patch
from pathlib import Path
from location_context import normalize_location,set_location,context_id
from search_cache import SearchCache
from ingestion import Catalog,Ingestor
from work_coordinator import PriorityLock
from connector import normalize

class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.catalog=Catalog(Path(self.temp.name)/'db');self.now=1000
        set_location({'postal_code':'63129','country_code':'US'})
    def tearDown(self):set_location(None);self.temp.cleanup()
    def cache(self):return SearchCache(clock=lambda:self.now).attach(self.catalog,'instacart')
    def test_exact_snapshot_survives_memory_loss_without_extending_expiry(self):
        a=self.cache();key=a.key('63129',['milk'],'',True);p={'items':[{'id':'1','retailer':'aldi'}]}
        a.put(key,p);self.now+=299;b=self.cache();payload,meta=b.get(key)
        self.assertEqual(meta['source'],'persistent_cache');self.assertEqual(meta['expires_in'],1)
        self.now+=1;self.assertIsNone(b.get(key))
    def test_invalidation_cannot_resurrect_durable_snapshot(self):
        a=self.cache();key=a.key('63129',['milk'],'',True);a.put(key,{'items':[{'retailer':'aldi'}]})
        a.invalidate('aldi','63129');self.assertIsNone(self.cache().get(key))
    def test_unrelated_store_does_not_inherit_all_store_expiry(self):
        a=self.cache();a.put(a.key('63129',['milk'],'',True),{'items':[{'retailer':'aldi'}]});self.now+=240
        key=a.key('63129',['eggs'],'',True);self.assertEqual(a.put(key,{'items':[{'retailer':'costco'}]})['expires_in'],300)
    def test_location_validation_rejects_conflicts_and_invalid_gps(self):
        for raw in [{'zip':'63129','location':{'postal_code':'63110'}},{'location':{'postal_code':'63129','coordinates':{'latitude':float('nan'),'longitude':0}}},{'location':{'postal_code':'63129','extra':'ignored'}}]:
            with self.assertRaises(ValueError):normalize_location(raw)
    def test_different_addresses_have_distinct_opaque_keys(self):
        loc={'postal_code':'63129','country_code':'US','address':{'line1':'5430 Telegraph Road','city':'St. Louis','region':'MO'}}
        a=context_id(loc);b=context_id({**loc,'address':{**loc['address'],'line1':'1 Other Road'}})
        self.assertNotEqual(a,b);self.assertNotIn('Telegraph',a)
    def test_recommendations_do_not_satisfy_query(self):
        from connector import matches_query
        self.assertFalse(matches_query('Whole Milk','cucumbers'))
        self.assertTrue(matches_query('Organic Cucumber','cucumbers'))
        self.assertFalse(matches_query('Chicken Thighs','chicken breast'))
        self.assertEqual(normalize([{'name':'Milk Conditioner','text':'Current price: $2.99'}],'milk','aldi',None),[])
    def test_product_id_survives_price_change(self):
        raw={'name':'Milk','text':'Current price: $2.99','url':'/products/123?retailerSlug=aldi'}
        self.assertEqual(normalize([raw],'milk','aldi',None)[0]['id'],normalize([{**raw,'text':'Current price: $3.99'}],'milk','aldi',None)[0]['id'])
    def test_background_yields_next_slot_to_waiting_foreground(self):
        lock=PriorityLock();self.assertTrue(lock.acquire(blocking=False));order=[]
        def foreground():
            self.assertTrue(lock.acquire(timeout=1));order.append('foreground');lock.release()
        t=threading.Thread(target=foreground);t.start()
        deadline=time.monotonic()+1
        while lock.waiters==0 and time.monotonic()<deadline:time.sleep(.001)
        lock.release();self.assertFalse(lock.acquire(blocking=False));t.join();self.assertEqual(order,['foreground'])
    def test_seed_batch_writes_once_and_future_due_is_preserved(self):
        worker=Ingestor(self.catalog,self.cache(),threading.Lock(),Mock(),queries=['milk'],clock=lambda:self.now)
        with patch.object(self.catalog,'save_state',wraps=self.catalog.save_state) as save:
            worker.observe('63110',['eggs']);self.assertEqual(save.call_count,1)
        worker.enqueue('milk',due=2000);self.assertEqual(worker.jobs[0]['next_due'],2000)
    def test_quote_expiry_and_thread_safety(self):
        from result_store import ResultStore
        store=ResultStore(clock=lambda:self.now)
        def write(n):store[str(n)]={'created':1000,'expires':1100,'payload':{}}
        threads=[threading.Thread(target=write,args=(n,)) for n in range(30)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(store),30);self.now=1100;self.assertIsNone(store.get('1'));self.assertEqual(len(store),0)
    def test_pause_survives_restore(self):
        worker=Ingestor(self.catalog,self.cache(),threading.Lock(),Mock(),queries=['milk']);worker.pause_reason='login_required';worker.persist()
        other=Ingestor(self.catalog,self.cache(),threading.Lock(),Mock(),queries=['milk']);self.assertEqual(other.pause_reason,'login_required')
    def test_nonfood_catalog_match_is_excluded(self):
        p={'items':[{'id':'1','name':'Milk Conditioner','price':2,'retailer':'aldi','url':'https://www.instacart.com/products/1'}],'location':{'verified':False}}
        self.catalog.ingest('63129','milk',p,300);self.assertEqual(self.catalog.search('milk'),[])

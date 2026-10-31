import tempfile, threading, unittest
from pathlib import Path
from unittest.mock import Mock
from ingestion import Catalog, Ingestor, SEEDS, MAX_QUERIES_PER_ZIP
from connector import ConnectorError
from search_cache import SearchCache

class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.catalog=Catalog(Path(self.temp.name)/'catalog.sqlite');self.now=1000
        self.payload={'items':[{'id':'milk-price1','name':'Whole Milk','retailer':'aldi','size':'1 gal','price':2.99,'url':'https://www.instacart.com/products/123-milk?retailerSlug=aldi','query':'milk'}],'location':{'requested_zip':'63129','observed_zip':None,'verified':False},'warnings':[]}
        self.fetch=Mock(return_value=self.payload)
        self.cache=SearchCache(clock=lambda:self.now)
        self.lock=threading.Lock()
        self.worker=Ingestor(self.catalog,self.cache,self.lock,self.fetch,queries=['milk','eggs'],clock=lambda:self.now)
    def tearDown(self):self.temp.cleanup()
    def test_stored_catalog_survives_reopen_and_expires_price(self):
        self.catalog.ingest('63129','milk',self.payload,300,now=1000)
        other=Catalog(self.catalog.path)
        self.assertEqual(other.search('milk','63129',now=1100)[0]['price'],2.99)
        self.assertFalse(other.search('milk','63129',now=1301)[0]['price_fresh'])
        self.assertEqual(other.search('milk','63101',now=1100),[])
    def test_deduplicates_product_when_price_changes(self):
        self.catalog.ingest('63129','milk',self.payload,300,now=1000)
        self.payload['items'][0].update({'price':3.49,'id':'milk-price2'})
        self.catalog.ingest('63129','whole milk',self.payload,300,now=1100)
        rows=self.catalog.search('milk',now=1200)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['price'],3.49)
    def test_queue_rotates_and_requeues(self):
        self.assertTrue(self.worker.tick());self.assertEqual(self.fetch.call_args.args[1],['milk'])
        self.assertTrue(self.worker.tick());self.assertEqual(self.fetch.call_args.args[1],['eggs'])
        self.assertFalse(self.worker.tick())
        self.now+=21601;self.assertTrue(self.worker.tick())
        self.assertEqual(self.worker.jobs[0]['successes'],2)
    def test_failure_backoff_and_screening_pause(self):
        self.fetch.side_effect=ConnectorError('provider_timeout','slow')
        self.assertFalse(self.worker.tick());self.assertEqual(self.worker.jobs[0]['next_due'],1060)
        self.fetch.side_effect=ConnectorError('provider_blocked','screened')
        self.worker.tick();self.assertEqual(self.worker.pause_reason,'provider_blocked')
        calls=self.fetch.call_count;self.now+=3600;self.worker.tick();self.assertEqual(self.fetch.call_count,calls)
    def test_shared_browser_lock_skips_background_work(self):
        self.lock.acquire()
        self.assertFalse(self.worker.tick());self.fetch.assert_not_called();self.lock.release()
    def test_requested_queries_join_bounded_queue(self):
        self.assertTrue(self.worker.enqueue(' Yogurt '));self.assertEqual(self.worker.jobs[-1]['query'],'yogurt')
        self.worker.enqueue('yogurt');self.assertEqual(len(self.worker.jobs),3)
        for n in range(MAX_QUERIES_PER_ZIP + 5):self.worker.enqueue('query '+str(n))
        self.assertEqual(len(self.worker.jobs),MAX_QUERIES_PER_ZIP)
    def test_zip_jobs_deduplicate_and_respect_footprint_limit(self):
        self.worker.observe('63101',[' Chicken ','chicken'])
        matches=[j for j in self.worker.jobs if j['zip']=='63101' and j['query']=='chicken']
        self.assertEqual(len(matches),1);self.assertEqual(matches[0]['requests'],2)
        self.assertTrue(any(j['zip']=='63101' and j['query']=='milk' for j in self.worker.jobs))
        for z in ['63102','63103','63104','63105']:self.worker.observe(z,['rice'])
        self.assertEqual(self.worker.status()['footprint']['zip_count'],5)
        self.assertFalse(any(j['zip']=='63105' for j in self.worker.jobs))
    def test_expanded_list_and_new_zip_have_full_food_coverage(self):
        self.assertGreater(len(SEEDS),350)
        self.assertEqual(len(SEEDS),len(set(SEEDS)))
        self.assertIn("mcdonald's",SEEDS)
        self.worker.observe('63017',['spinach'])
        terms={j['query'] for j in self.worker.jobs if j['zip']=='63017'}
        self.assertTrue(set(SEEDS).issubset(terms))

    def test_worker_fetches_new_job_location(self):
        self.worker.observe('63101',['Chicken'])
        self.worker.tick()
        self.assertEqual(self.fetch.call_args.args[0],'63101')
        self.assertEqual(self.fetch.call_args.args[1],['chicken'])
    def test_queue_and_budget_survive_restart(self):
        self.worker.observe('63017',['sushi'])
        self.worker.tick()
        restored=Ingestor(self.catalog,self.cache,self.lock,self.fetch,clock=lambda:self.now)
        self.assertEqual(restored.jobs,self.worker.jobs)
        self.assertEqual(restored.attempts,self.worker.attempts)
    def test_hot_jobs_do_not_starve_discovery(self):
        self.worker.enqueue('sushi')
        self.worker.enqueue('pizza')
        choices=[self.worker.choose_job(self.worker.jobs) for _ in range(5)]
        self.assertTrue(any(self.worker.tier(j)=='discovery' for j in choices))
        self.assertTrue(any(self.worker.tier(j)=='hot' for j in choices))
    def test_location_mismatch_is_not_verified(self):
        self.payload['location']={'verified':True,'observed_zip':'30043'}
        self.catalog.ingest('63129','milk',self.payload,300,now=self.now)
        row=self.catalog.search('milk',now=self.now)[0]
        self.assertFalse(row['location_verified'])
        self.assertEqual(row['observed_zip'],'30043')
        self.assertEqual(self.catalog.counts()['verified_zip_codes'],[])
    def test_native_backup_restores_products_and_queue(self):
        self.worker.enqueue('sushi')
        target=Path(self.temp.name)/'backup.sqlite'
        self.catalog.backup(target)
        backup=Catalog(target)
        self.assertEqual(backup.load_state(self.worker.state_key)['jobs'],self.worker.jobs)

    def test_store_invalidation_expires_catalog_prices(self):
        self.catalog.ingest('63129','milk',self.payload,300,now=1000)
        self.assertEqual(self.catalog.invalidate('aldi','63129'),1)
        self.assertFalse(self.catalog.search('milk',now=1100)[0]['price_fresh'])

if __name__=='__main__':unittest.main()

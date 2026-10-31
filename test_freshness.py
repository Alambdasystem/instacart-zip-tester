import json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from connector import ConnectorError
from ingestion import Catalog,Ingestor
from location_context import set_location
from provider_session import request_context_id
from refresh_planner import RefreshPlanner,policy_enabled
from result_store import ResultStore
from search_cache import SearchCache
from search_service import SearchService
from work_coordinator import PriorityLock

class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.now=[1000.0];self.clock=lambda:self.now[0]
        self.loc={'postal_code':'63129','country_code':'US'};set_location(self.loc)
        self.catalog=Catalog(self.root/'db.sqlite')
        self.cache=SearchCache(clock=self.clock,independent_expiry=True).attach(self.catalog,'instacart')
    def tearDown(self):set_location(None);self.tmp.cleanup()
    def payload(self,q='milk',retailer='aldi'):
        return {'items':[{'id':q,'name':q,'query':q,'retailer':retailer,'price':2.99,'url':'https://www.instacart.com/products/'+q,'image_url':'https://www.instacart.com/image.png'}],'location':{'requested_zip':'63129','observed_zip':'63129','verified':True,'context_id':request_context_id(self.loc)}}
    def put(self,q='milk',retailer='aldi'):
        return self.cache.put(self.cache.key('63129',[q],retailer,True),self.payload(q,retailer))
    def test_independent_expiry_survives_restart_without_renewal(self):
        self.put();self.now[0]=1240;meta=self.put('eggs')
        self.assertEqual(meta['expires_in'],300)
        self.now[0]=1301
        fresh=SearchCache(clock=self.clock,independent_expiry=True).attach(self.catalog,'instacart')
        self.assertIsNone(fresh.get(fresh.key('63129',['milk'],'aldi',True)))
        value,meta=fresh.get(fresh.key('63129',['eggs'],'aldi',True))
        self.assertEqual(meta['expires_in'],239);self.assertEqual(meta['source'],'persistent_cache')
        self.now[0]=1540;self.assertIsNone(fresh.get(fresh.key('63129',['eggs'],'aldi',True)))
    def test_flag_isolated_from_other_providers_and_legacy_keys(self):
        other=SearchCache(clock=self.clock,independent_expiry=True).attach(self.catalog,'mealme_satsuma')
        key=other.key('63129',['milk'],'aldi',True);other.put(key,self.payload());self.now[0]=1240
        self.assertEqual(other.put(other.key('63129',['eggs'],'aldi',True),self.payload('eggs'))['expires_in'],60)
        legacy=SearchCache(clock=self.clock)
        self.assertNotEqual(legacy.key('63129',['milk'],'aldi',True),self.cache.key('63129',['milk'],'aldi',True))
    def test_explicit_store_invalidation_preserves_unrelated_snapshot(self):
        self.put('milk','aldi');self.put('eggs','schnucks')
        self.cache.invalidate('aldi','63129')
        fresh=SearchCache(clock=self.clock,independent_expiry=True).attach(self.catalog,'instacart')
        self.assertIsNone(fresh.get(fresh.key('63129',['milk'],'aldi',True)))
        self.assertIsNotNone(fresh.get(fresh.key('63129',['eggs'],'schnucks',True)))
    def test_invalidation_during_fetch_cannot_repopulate_snapshot_or_catalog(self):
        def fetch(*args):self.cache.invalidate('aldi','63129');return self.payload()
        service=SearchService(self.catalog,self.cache,threading.Lock(),fetch)
        with self.assertRaises(ConnectorError) as e:service.resolve('63129',['milk'],'aldi',True)
        self.assertEqual(e.exception.status,'cache_invalidated')
        self.assertEqual(self.catalog.counts()['products'],0);self.assertFalse(self.cache.entries)
    def test_invalidation_between_snapshot_and_catalog_write_rejects_catalog(self):
        fence=self.cache.fence('63129');self.put();self.cache.invalidate('aldi','63129')
        with self.assertRaises(ConnectorError):self.catalog.ingest('63129','milk',self.payload(),300,cache_fence=('instacart','63129',fence))
        self.assertEqual(self.catalog.counts()['products'],0)
    def test_concurrent_forced_refreshes_share_one_upstream_read_and_budget(self):
        lock=PriorityLock();entered=threading.Event();release=threading.Event();results=[];errors=[];budgets=[]
        def fetch(*args):entered.set();release.wait(3);return self.payload()
        fetch=Mock(side_effect=fetch);service=SearchService(self.catalog,self.cache,lock,fetch)
        def run():
            set_location(self.loc)
            try:results.append(service.resolve('63129',['milk'],'aldi',True,'refresh',lambda:budgets.append(1)))
            except Exception as e:errors.append(e)
        first=threading.Thread(target=run);first.start();self.assertTrue(entered.wait(2))
        second=threading.Thread(target=run);second.start();end=time.time()+2
        while not lock.waiters and time.time()<end:time.sleep(.01)
        self.assertTrue(lock.waiters);release.set();first.join(3);second.join(3)
        self.assertFalse(errors);self.assertEqual(len(results),2);self.assertEqual(fetch.call_count,1);self.assertEqual(len(budgets),1)
    def planner(self):
        p=RefreshPlanner(self.cache,clock=self.clock);self.cache.refresh_planner=p;return p
    def test_refresh_before_expiry_does_not_extend_existing_quote(self):
        planner=self.planner();meta=self.put();planner.observe('63129','milk','aldi',True,meta)
        old=ResultStore(clock=self.clock);old['old']={'created':1000,'expires':1300,'payload':self.payload()}
        def fetch(*args):self.now[0]+=50;return self.payload()
        worker=Ingestor(self.catalog,self.cache,threading.Lock(),fetch,queries=[],clock=self.clock)
        job=planner.admitted()[0];self.now[0]=job['next_due']
        self.assertLess(self.now[0],1300);self.assertTrue(worker.tick())
        self.assertEqual(job['successes'],1);self.assertEqual(len(worker.attempts),1)
        self.now[0]=1300;self.assertIsNone(old.get('old'))
        self.assertIsNotNone(self.cache.get(self.cache.key('63129',['milk'],'aldi',True)))
    def test_private_address_is_memory_only_and_preserved_for_refresh(self):
        self.loc={**self.loc,'address':{'line1':'5430 Telegraph Road','city':'St. Louis','region':'MO'}};set_location(self.loc)
        planner=self.planner();meta=self.put();planner.observe('63129','milk','aldi',True,meta)
        seen=[]
        def fetch(*args):
            from location_context import current_location
            seen.append(current_location());return self.payload()
        worker=Ingestor(self.catalog,self.cache,threading.Lock(),fetch,queries=[],clock=self.clock)
        self.now[0]=planner.admitted()[0]['next_due'];self.assertTrue(worker.tick());worker.persist()
        self.assertEqual(seen[0],self.loc)
        with self.catalog.db() as db:
            persisted=''.join(r[0] for r in db.execute('SELECT payload FROM worker_state'))
            self.assertNotIn('Telegraph',persisted)
            self.assertEqual(db.execute('SELECT count(*) FROM ingestion_jobs').fetchone()[0],0)
        self.assertNotIn('Telegraph',json.dumps(planner.status()))
    def test_cache_only_never_registers_refresh_demand(self):
        planner=self.planner();self.put()
        fetch=Mock();SearchService(self.catalog,self.cache,threading.Lock(),fetch).resolve('63129',['milk'],'aldi',True,'cache_only')
        fetch.assert_not_called();self.assertEqual(planner.status()['demand_entries'],0)
    def test_hot_set_bounded_and_demand_expires_without_reads(self):
        planner=self.planner()
        for n in range(80):
            q='food'+str(n);planner.observe('63129',q,'aldi',True,{'expires_in':300})
        status=planner.status(60);self.assertEqual(status['demand_entries'],64);self.assertEqual(status['admitted'],1)
        self.now[0]+=901;self.assertEqual(planner.status()['demand_entries'],0)
    def test_basket_activity_prioritizes_matching_context(self):
        planner=self.planner();planner.observe('63129','milk','aldi',True,{'expires_in':300});planner.observe('63129','eggs','aldi',True,{'expires_in':300})
        planner.promote(self.payload()['location'],self.payload()['items'])
        self.assertEqual(planner.admitted()[0]['query'],'milk')
    def test_hot_work_obeys_existing_budget_and_pause(self):
        planner=self.planner();planner.observe('63129','milk','aldi',True,{'expires_in':300})
        fetch=Mock(return_value=self.payload());worker=Ingestor(self.catalog,self.cache,threading.Lock(),fetch,queries=[],clock=self.clock)
        self.now[0]+=200;worker.attempts=[self.now[0]]*120;self.assertFalse(worker.tick());fetch.assert_not_called()
        worker.attempts=[];worker.pause_reason='provider_blocked';self.assertFalse(worker.tick());fetch.assert_not_called()
    def test_catalog_retains_expired_descriptions_with_explicit_price_state(self):
        self.catalog.ingest('63129','milk',self.payload(),300,now=1000)
        result=self.catalog.search('milk','63129',now=1301)[0]
        self.assertFalse(result['price_fresh']);self.assertEqual(result['price_status'],'expired');self.assertEqual(result['last_seen_price'],2.99);self.assertTrue(result['image_url'])
    def test_urgent_refresh_precedes_reserved_discovery_turn(self):
        planner=self.planner();meta=self.put();planner.observe('63129','milk','aldi',True,meta)
        fetch=Mock(return_value=self.payload());worker=Ingestor(self.catalog,self.cache,threading.Lock(),fetch,queries=['eggs'],clock=self.clock)
        worker.refresh_turn=4;worker.durations=[54];self.now[0]=1280
        self.assertTrue(worker.tick());self.assertEqual(fetch.call_args.args[1],['milk'])
    def test_discovery_turn_is_preserved_when_refresh_has_time(self):
        planner=self.planner();meta=self.put();planner.observe('63129','milk','aldi',True,meta)
        fetch=Mock(return_value=self.payload('eggs'));worker=Ingestor(self.catalog,self.cache,threading.Lock(),fetch,queries=['eggs'],clock=self.clock)
        worker.refresh_turn=4;worker.durations=[54];self.now[0]=1110
        self.assertTrue(worker.tick());self.assertEqual(fetch.call_args.args[1],['eggs'])
    def test_versioned_switch_defaults_off_and_environment_can_disable_it(self):
        import os
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(policy_enabled(self.root))
            (self.root/'freshness-config.json').write_text('{"enabled":true}')
            self.assertTrue(policy_enabled(self.root))
            with patch.dict(os.environ,{'INSTACART_FRESHNESS_V07':'false'}):self.assertFalse(policy_enabled(self.root))
    def test_versioned_switch_rejects_string_boolean(self):
        import os
        (self.root/'freshness-config.json').write_text('{"enabled":"false"}')
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError):policy_enabled(self.root)
    def test_refresh_window_covers_in_progress_read_and_next_read_budgets(self):
        planner=self.planner();meta=self.put();planner.observe('63129','milk','aldi',True,meta)
        due=planner.admitted()[0]['next_due']
        # Existing read 90s + scheduler delay 15s + refresh read 90s must
        # complete before the original quote expiry at 1300.
        self.assertLess(due+90+15+90,1300)

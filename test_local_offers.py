import copy,json,os,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import Mock,MagicMock,patch
from urllib.request import urlopen
from connector import normalize,ConnectorError
from ingestion import Catalog,Ingestor
from location_context import set_location
from search_cache import SearchCache
from work_coordinator import PriorityLock
from savings import calculate,SavingsError
import cancellation
import test_api
import api_server as api

class OfferCorrectnessTests(unittest.TestCase):
    def setUp(self):set_location({'postal_code':'63129','country_code':'US'})
    def tearDown(self):set_location(None)
    def item(self,store='aldi',price='3.69',suffix='each'):
        return normalize([{'name':'Coca Cola Original','text':'Current price: $'+price+' '+suffix+'\n2 L\n$0.05/fl oz','url':'/products/12345-coca-cola?retailerSlug='+store}],'Coca Cola',store,'63129')[0]
    def quote(self):
        return {'s':{'created':1000,'expires':1300,'payload':{'location':{'requested_zip':'63129','observed_zip':'63129','context_id':'zip:63129','verified':True},'items':[self.item(),self.item('target','4.19')]}}}
    def refs(self):return {'offers':[{'search_id':'s','item_id':self.item()['id']},{'search_id':'s','item_id':self.item('target')['id']}],'quantity':3}
    def test_primary_each_not_secondary_volume(self):
        i=self.item();self.assertEqual(i['price_basis'],'per_item');self.assertEqual(i['price_unit'],'item');self.assertTrue(i['price_basis_verified']);self.assertEqual(i['unit_price'],{'amount':.05,'unit':'fl oz'})
    def test_unknown_basis_is_not_guessed_from_pack_size_or_secondary_rate(self):
        i=self.item(suffix='');self.assertEqual(i['price_basis'],'unspecified');self.assertFalse(i['price_basis_verified'])
    def test_product_scoped_add_one_count_is_unit_evidence(self):
        card={'name':'Coca Cola Original','text':'Current price: $3.69\n2 L','quantity_label':'Add 1 ct Coca Cola Original'}
        i=normalize([card],'Coca Cola','aldi','63129')[0]
        self.assertEqual(i['price_basis'],'per_item');self.assertEqual(i['price_unit_evidence'],'quantity_control_label')
        card['quantity_label']='Add 1 ct Different product'
        self.assertFalse(normalize([card],'Coca Cola','aldi','63129')[0]['price_basis_verified'])
    def test_renamed_street_address_field_is_supported(self):
        from connector import establish_location
        class EmptyField:
            def count(self):return 0
            @property
            def first(self):return self
            def wait_for(self,**kw):raise TimeoutError()
        page=Mock();control=Mock();field=Mock();option=Mock();save=Mock();dialog=Mock();state={'modal':False,'label':'current address: 30043. Or select a new address.'}
        control.count.return_value=1;control.first.get_attribute.side_effect=lambda *a,**kw:state['label'];control.first.click.side_effect=lambda **kw:state.update(modal=True)
        field.count.return_value=1;dialog.count.side_effect=lambda:int(state['modal'])
        option.first.inner_text.return_value='5430 Telegraph Road St. Louis, MO 63129, USA'
        save.count.return_value=1;save.first.is_enabled.return_value=True
        save.first.click.side_effect=lambda **kw:state.update(modal=False,label='current address: 5430 Telegraph Road. Or select a new address.')
        def locator(role,**kw):
            if role=='dialog':return dialog
            if role=='textbox':
                n=kw.get('name');return field if hasattr(n,'search') and n.search('Street address') else EmptyField()
            if kw['name'].pattern.startswith('^current address'):return control
            if kw['name'].pattern.startswith('^Save'):return save
            return option
        page.get_by_role.side_effect=locator;page.locator.return_value.inner_text.return_value=''
        set_location({'postal_code':'63129','country_code':'US','address':{'line1':'5430 Telegraph Road','city':'St. Louis','region':'MO'}})
        self.assertEqual(establish_location(page,'63129',False),('63129',True));field.fill.assert_called_once()
    def test_weighted_primary_differs_from_secondary_rate(self):
        i=self.item(suffix='/lb');self.assertEqual(i['price_basis'],'per_lb');self.assertEqual(i['price_unit'],'lb')
    def test_verified_decimal_savings(self):
        r=calculate(self.quote(),self.refs(),now=1001)
        self.assertEqual((r['original_subtotal'],r['replacement_subtotal'],r['savings'],r['savings_percent']),(12.57,11.07,1.5,11.93))
    def test_expiry_boundary(self):
        with self.assertRaises(SavingsError) as e:calculate(self.quote(),self.refs(),now=1300)
        self.assertEqual(e.exception.code,'price_expired')
    def test_wrong_zip_cannot_produce_savings_even_if_verified_flag_true(self):
        q=self.quote();q['s']['payload']['location']['observed_zip']='30043'
        with self.assertRaises(SavingsError) as e:calculate(q,self.refs(),1001)
        self.assertEqual(e.exception.code,'location_unverified')
    def test_same_named_different_product_is_not_equivalent(self):
        q=self.quote();q['s']['payload']['items'][1]['url']='https://www.instacart.com/products/999-different'
        with self.assertRaises(SavingsError) as e:calculate(q,self.refs(),1001)
        self.assertEqual(e.exception.code,'product_mismatch')
    def test_size_unit_promotion_and_merchant_gates(self):
        for field,value,code in [('size','1 L','product_mismatch'),('price_basis_verified',False,'price_unit_unverified'),('promotion_text','Buy 2 get 1','conditional_price'),('retailer','aldi','different_merchants_required'),('currency','CAD','price_unit_mismatch')]:
            q=self.quote();q['s']['payload']['items'][1][field]=value
            with self.subTest(field=field),self.assertRaises(SavingsError) as e:calculate(q,self.refs(),1001)
            self.assertEqual(e.exception.code,code)
    def test_context_isolation_across_searches(self):
        q=self.quote();q['other']=copy.deepcopy(q['s']);q['other']['payload']['location']['context_id']='ctx:other';body=self.refs();body['offers'][1]['search_id']='other'
        with self.assertRaises(SavingsError) as e:calculate(q,body,1001)
        self.assertEqual(e.exception.code,'mixed_location_context')
    def test_optional_pagination_timeout_keeps_first_verified_page(self):
        import sys,types,deadline
        from connector import search_browser
        page=Mock();browser=Mock();context=Mock();browser.new_context.return_value=context;context.new_page.return_value=page
        state={'cards':[{'name':'Coca Cola Original','text':'Current price: $3.69 each\n2 L','url':'/products/12345-cola?retailerSlug=aldi'}],'retailers':[]}
        page.evaluate.return_value=state
        module=types.ModuleType('playwright.sync_api');module.TimeoutError=TimeoutError
        module.sync_playwright=MagicMock();module.sync_playwright.return_value.__enter__.return_value.chromium.launch.return_value=browser
        with patch.dict(sys.modules,{'playwright':types.ModuleType('playwright'),'playwright.sync_api':module}),patch('connector.establish_location',return_value=('63129',True)),patch('connector.observed_location',return_value='63129'),patch('connector.wait_for_catalog',return_value=(state,True)),patch('pagination.expand',side_effect=deadline.DeadlineExceeded('optional traversal exhausted')):
            result=search_browser('63129',['Coca Cola'])
        self.assertEqual(len(result['items']),1);self.assertTrue(result['location']['verified']);self.assertTrue(result['warnings']);context.close.assert_called_once();browser.close.assert_called_once()

    def test_reference_observation_not_local_context(self):
        with tempfile.TemporaryDirectory() as d:
            c=Catalog(Path(d)/'db');c.ingest('63129','Coca Cola',{'items':[self.item()],'location':{'observed_zip':'30043','context_id':'zip:63129','verified':False}},300,now=1000)
            i=c.search('Coca Cola','63129',now=1001)[0]
            self.assertFalse(i['context_compatible']);self.assertFalse(i['local_offer_eligible']);self.assertTrue(i['discovery_only'])
            counts=c.counts(now=1001);self.assertEqual(counts['fresh_zip_acknowledged_offers'],0);self.assertEqual(counts['fresh_unverified_observations'],1)
    def test_foreground_interrupts_opted_in_background_only(self):
        lock=PriorityLock();event=threading.Event();lock.acquire(blocking=False);lock.register_background(event)
        acquired=[]
        t=threading.Thread(target=lambda:acquired.append(lock.acquire(timeout=1)));t.start()
        self.assertTrue(event.wait(1));lock.release();t.join();self.assertEqual(acquired,[True]);lock.release()
        lock.acquire();other=threading.Event()
        t=threading.Thread(target=lambda:other.set() if lock.acquire(timeout=.02) else None);t.start();t.join();self.assertFalse(other.is_set());lock.release()
    def test_ingestion_yield_is_not_failure_or_provider_suspension(self):
        with tempfile.TemporaryDirectory() as d:
            c=Catalog(Path(d)/'db');lock=PriorityLock();cache=SearchCache().attach(c,'instacart');started=threading.Event()
            def fetch(*args):
                started.set()
                while True:cancellation.check();time.sleep(.005)
            worker=Ingestor(c,cache,lock,fetch,queries=['milk'])
            thread=threading.Thread(target=worker.tick);thread.start();self.assertTrue(started.wait(1))
            self.assertTrue(lock.acquire(timeout=1));lock.release();thread.join(1)
            self.assertFalse(thread.is_alive());self.assertEqual(worker.jobs[0]['last_status'],'yielded_to_foreground');self.assertEqual(worker.jobs[0]['failures'],0);self.assertFalse(worker.blocked_contexts)

class LocalOfferApiTests(unittest.TestCase):
    setUpClass=classmethod(test_api.MiddlewareTests.setUpClass.__func__)
    tearDownClass=classmethod(test_api.MiddlewareTests.tearDownClass.__func__)
    setUp=test_api.MiddlewareTests.setUp
    post=test_api.MiddlewareTests.post
    def test_catalog_default_withholds_wrong_zip_but_explicit_discovery_retains_it(self):
        with tempfile.TemporaryDirectory() as d,patch.object(api,'CATALOG',Catalog(Path(d)/'db')):
            item=OfferCorrectnessTests().item();api.CATALOG.ingest('63129','Coca Cola',{'items':[item],'location':{'observed_zip':'30043','verified':False,'context_id':'zip:63129'}},300)
            strict=json.load(urlopen(self.url+'/api/v1/catalog?zip=63129&query=Coca%20Cola'))
            self.assertEqual(strict['items'],[]);self.assertEqual(strict['excluded_unverified_observations'],1)
            discovery=json.load(urlopen(self.url+'/api/v1/catalog?zip=63129&query=Coca%20Cola&allow_unverified_location=true'))
            self.assertEqual(len(discovery['items']),1);self.assertFalse(discovery['items'][0]['selectable']);self.assertFalse(discovery['items'][0]['context_compatible'])
    def test_http_savings_recomputes_server_prices_and_rejects_client_money(self):
        fixture=OfferCorrectnessTests();q=fixture.quote();q['s']['created']=time.time();q['s']['expires']=time.time()+300;api.RESULTS['s']=q['s']
        code,r=self.post('/api/v1/savings',fixture.refs());self.assertEqual(code,200);self.assertEqual(r['savings'],1.5)
        code,r=self.post('/api/v1/savings',{**fixture.refs(),'price':1});self.assertEqual(code,422)

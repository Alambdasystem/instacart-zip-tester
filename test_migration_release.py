import unittest,tempfile,threading,json,io,time
from pathlib import Path
from unittest.mock import Mock,patch
from ingestion import Catalog,Ingestor
from search_cache import SearchCache
from location_context import set_location,context_id
from migration_validation import validate_pair
from direct_http import parse_html,compare_results
from page_state import classify_dialog
import deadline
class ReleaseTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.c=Catalog(Path(self.tmp.name)/'db');set_location({'postal_code':'63129','country_code':'US'})
    def tearDown(self):set_location(None);self.tmp.cleanup()
    def item(self):return {'id':'1','name':'Milk','price':2.99,'size':'1 gal','currency':'USD','retailer':'aldi','url':'https://www.instacart.com/products/1-milk','image_url':'https://www.instacart.com/m.jpg'}
    def test_two_address_contexts_remain_separate_after_restart(self):
        for ctx,price in [('ctx:A',2.99),('ctx:B',4.99)]:self.c.ingest('63129','milk',{'items':[{**self.item(),'price':price}],'location':{'context_id':ctx,'verified':True,'observed_zip':'63129'}},300,now=1000)
        other=Catalog(self.c.path)
        self.assertEqual(len(other.search('milk','63129',now=1100)),2)
        self.assertEqual(other.search('milk','63129',now=1100,context='ctx:A')[0]['price'],2.99)
        self.assertFalse(any(i['context_compatible'] for i in other.search('milk','63129',now=1100)))
    def test_legacy_catalog_cannot_be_context_compatible(self):
        self.c.ingest('63129','milk',{'items':[self.item()],'location':{'verified':True,'observed_zip':'63129'}},300)
        self.assertFalse(self.c.search('milk','63129')[0]['context_compatible'])
    def pair(self):
        x={'status':'ok','items':[self.item()],'location':{'context_id':'ctx:A','verified':True,'observed_zip':'63129'},'coverage_complete':True}
        import copy
        return {'direct_http':copy.deepcopy(x),'browser':copy.deepcopy(x)}
    def test_missing_zip_and_conflicting_requested_zip_block(self):
        p=self.pair()
        for x in p.values():x['location'].pop('observed_zip')
        self.assertFalse(validate_pair(p)['gates']['same_verified_context'])
        p=self.pair();p['direct_http']['location']['requested_zip']='63110'
        self.assertFalse(validate_pair(p)['gates']['same_verified_context'])
    def test_duplicate_identity_and_negative_price_block(self):
        p=self.pair();p['direct_http']['items']*=2
        self.assertEqual(validate_pair(p)['decision'],'BLOCKED')
        p=self.pair()
        for x in p.values():x['items'][0]['price']=-1
        self.assertFalse(validate_pair(p)['gates']['same_price_size_currency'])
    def test_summary_computes_pair_parity(self):
        p=self.pair();self.assertTrue(compare_results(p['direct_http'],p['browser'])['equivalent_for_local_search'])
    def test_heading_and_srcset_card(self):
        rows=parse_html('<a data-item-card-button="true" href="/products/1-milk?retailerSlug=aldi"><h3>Milk</h3><img srcset="https://www.instacart.com/m.jpg 1x"><span>Current price: $2.99</span></a>','milk')
        self.assertEqual(rows[0]['name'],'Milk');self.assertEqual(rows[0]['image_url'],'https://www.instacart.com/m.jpg')
    def test_generic_continue_is_not_authentication(self):
        self.assertEqual(classify_dialog('Continue with your delivery address'),'address_dialog')
        self.assertEqual(classify_dialog('Log in using your email address'),'login_required')
    def test_deadline_limits_waits(self):
        token=deadline.begin(.02)
        try:
            self.assertLessEqual(deadline.bounded_ms(10000),20)
            with patch('deadline.time.monotonic',return_value=time.monotonic()+1):
                with self.assertRaises(deadline.DeadlineExceeded):deadline.check()
        finally:deadline.reset(token)
    def test_durable_hydration_preserves_expiry_and_then_uses_memory(self):
        now=[1000];a=SearchCache(clock=lambda:now[0]).attach(self.c,'instacart');key=a.key('63129',['milk'],'',False);a.put(key,{'items':[self.item()]});now[0]=1100
        b=SearchCache(clock=lambda:now[0]).attach(self.c,'instacart');_,first=b.get(key);_,second=b.get(key)
        self.assertEqual(first['source'],'persistent_cache');self.assertEqual(second['source'],'memory_cache');self.assertEqual(second['expires_in'],200)
    def test_recovery_requires_success_and_never_probes_screening(self):
        fetch=Mock(return_value={'items':[self.item()],'location':{'verified':False}});worker=Ingestor(self.c,SearchCache(),threading.Lock(),fetch,queries=['milk']);worker.pause_reason='login_required'
        worker.circuit={'zip':'63110','query':'eggs','retailer':'aldi'}
        self.assertEqual(worker.recover()['status'],'recovered');self.assertIsNone(worker.pause_reason)
        fetch.assert_called_once_with('63110',['eggs'],'aldi',True)
        worker.pause_reason='provider_blocked';fetch.reset_mock()
        from connector import ConnectorError
        with self.assertRaises(ConnectorError):worker.recover()
        fetch.assert_not_called()
        worker.pause_reason='login_required';worker.circuit={'evidence':'legacy_unavailable'}
        with self.assertRaises(ConnectorError):worker.recover()
        fetch.assert_not_called()

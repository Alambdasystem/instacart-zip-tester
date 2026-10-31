"""Synthetic contract tests; never make paid provider requests."""
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from ingestion import Catalog
from search_cache import SearchCache
from provider_comparison import Comparison
from satsuma_provider import ProviderError
import vercel_provider as v

class VercelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.catalog=Catalog(self.temp.name+'/db')
        self.c=Comparison(self.catalog,SearchCache(),threading.Lock(),Mock())
    def tearDown(self):self.temp.cleanup()
    def fixture(self,path,params):
        provider=params['providerId']
        if path.endswith('/stores/search'):
            return {'stores':[{'providerId':provider,'providerLocationId':str(i),'name':'Store '+str(i)} for i in range(5)]}
        return {'items':[{'providerId':provider,'providerItemId':'milk','providerLocationId':params['locationId'],'title':'Milk','priceCents':325,'currency':'USD','imageUrl':'https://example.com/milk.jpg','raw':{'unitToken':'must-not-leak'}}]}
    def test_bounded_search_and_store_identity(self):
        req=Mock(side_effect=self.fixture)
        result=v.search('63110',['milk'],'vercel_kroger',req)
        self.assertEqual(req.call_count,3)
        self.assertEqual(len(result['retailers']),2)
        self.assertEqual(len({p['id'] for p in result['items']}),2)
        self.assertEqual(req.call_args_list[0].args[1]['zipCode'],'63110')
        self.assertFalse(result['location']['verified'])
        self.assertNotIn('must-not-leak',str(result))
        self.assertTrue(all(p['price'] is None and not p['selectable'] for p in result['items']))
    def test_credentials_are_host_scoped_and_no_writes(self):
        with patch.dict(os.environ,{'VERCEL_MEALME_API_KEY':'proxy-test','SATSUMA_API_KEY':'other-test'}),patch.object(v,'get_json',return_value={'stores':[]}) as get:
            v.request('/api/stores/search',{'providerId':'kroger','zipCode':'63129'})
            url,headers=get.call_args.args
            self.assertTrue(url.startswith(v.BASE_URL+'/api/stores/search?'))
            self.assertEqual(headers['Authorization'],'Bearer proxy-test')
            with self.assertRaises(ProviderError):v.request('/api/carts',{'providerId':'kroger'})
            self.assertEqual(get.call_count,1)
    def test_proxy_cache_db_and_satsuma_separate(self):
        with patch.object(self.c,'vercel_request',side_effect=self.fixture) as req:
            first=self.c.compare('63129',['milk'],'',True,'live',list(v.PROVIDERS))
            second=self.c.compare('63129',['milk'],'',True,'live',list(v.PROVIDERS))
        self.assertEqual([x['status'] for x in first['providers']],['ok','ok'])
        self.assertEqual(req.call_count,6)
        self.assertTrue(all(x['cache_hit'] for x in second['providers']))
        self.assertEqual(len(self.catalog.search(provider='vercel_kroger')),2)
        self.assertEqual(len(self.catalog.search(provider='vercel_square')),2)
        self.assertEqual(self.catalog.search(provider='mealme_satsuma'),[])
    def test_missing_key_and_zero_budget_do_not_call_provider(self):
        with patch.dict(os.environ,{},clear=True),patch.object(v,'request') as req:
            with self.assertRaises(ProviderError) as cm:self.c.vercel_request('/api/stores/search',{'providerId':'kroger'})
            self.assertEqual(cm.exception.code,'not_configured')
            with patch.dict(os.environ,{'VERCEL_MEALME_API_KEY':'test'}):
                with self.assertRaises(ProviderError) as cm:self.c.vercel_request('/api/stores/search',{'providerId':'kroger'})
                self.assertEqual(cm.exception.code,'budget_exhausted')
            req.assert_not_called()
    def test_shared_atomic_budget_and_cooldown(self):
        with patch.dict(os.environ,{'VERCEL_MEALME_API_KEY':'test','VERCEL_MEALME_READ_BUDGET':'1'}),patch.object(v,'request',side_effect=ProviderError('service_unavailable','Unavailable',503)) as req:
            for provider in ('kroger','square'):
                with self.assertRaises(ProviderError):self.c.vercel_request('/api/stores/search',{'providerId':provider})
            self.assertEqual(req.call_count,1)
            with self.catalog.db() as db:db.execute('DELETE FROM provider_state')
            with self.assertRaises(ProviderError) as cm:self.c.vercel_request('/api/stores/search',{'providerId':'square'})
            self.assertEqual(cm.exception.code,'budget_exhausted')
            self.assertEqual(self.c.status()['read_attempts_in_database'],0)
    def test_store_discovery_survives_catalog_failure_without_success_cache(self):
        def fail(path,params):
            if path.endswith('/catalog/search'):raise ProviderError('service_unavailable','Unavailable',503)
            return self.fixture(path,params)
        with patch.object(self.c,'vercel_request',side_effect=fail):
            result=self.c.one('vercel_kroger','63129',['milk'],'',True,'live')
        self.assertEqual(result['status'],'error')
        self.assertEqual(len(result['retailers']),2)
        self.assertEqual(result['error']['http_status'],503)
        self.assertFalse(self.c.vcaches['vercel_kroger'].entries)
        self.assertEqual(self.catalog.search(),[])
    def test_invalid_payload_is_not_empty_success(self):
        req=Mock(return_value={'html':'login'})
        with self.assertRaises(ProviderError):v.search('63129',['milk'],'vercel_square',req)
        self.assertEqual(req.call_count,1)
        with self.assertRaises(ProviderError):v.normalize({'items':[{'id':'unknown'}]},'vercel_kroger',{'id':'1','name':'Store'},'milk','63129')
    def test_identity_mismatch_rejected(self):
        with self.assertRaises(ProviderError):v.normalize({'items':[{'providerItemId':'1','title':'Milk','providerLocationId':'other'}]},'vercel_kroger',{'id':'1','name':'Store'},'milk','63129')
    def test_billing_failure_classified(self):
        with patch.dict(os.environ,{'VERCEL_MEALME_API_KEY':'test'}),patch.object(v,'get_json',side_effect=ProviderError('http_error','402',402)):
            with self.assertRaises(ProviderError) as cm:v.request('/api/stores/search',{'providerId':'kroger'})
            self.assertEqual(cm.exception.code,'billing_required')

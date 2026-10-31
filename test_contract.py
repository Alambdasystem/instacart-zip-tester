"""Run with jsonschema and openapi-spec-validator installed (development only)."""
import json, os, tempfile, time, unittest
from unittest.mock import patch
from urllib.request import urlopen
import jsonschema
from openapi_spec_validator import validate
import test_api
import api_server as api
from ingestion import Catalog
from connector import normalize

class ContractTests(unittest.TestCase):
    setUpClass = classmethod(test_api.MiddlewareTests.setUpClass.__func__)
    tearDownClass = classmethod(test_api.MiddlewareTests.tearDownClass.__func__)
    setUp = test_api.MiddlewareTests.setUp
    post = test_api.MiddlewareTests.post
    def test_openapi_valid_and_examples_match(self):
        validate(api.SPEC)
        for path in api.SPEC['paths'].values():
            for operation in path.values():
                for media in operation.get('requestBody',{}).get('content',{}).values():
                    self.check(media['example'],media['schema'])
    def check(self,data,schema):
        jsonschema.Draft202012Validator({**schema,'components':api.SPEC['components']}).validate(data)
    def test_models_match_http_responses(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(api,'CATALOG',Catalog(folder+'/test.sqlite')):
            item=normalize([{'name':'Milk','text':'Current price: $2.78\n1 gal'}],'milk','aldi',None)[0]
            payload={'items':[item],'location':{'requested_zip':'63129','observed_zip':None,'verified':False},'warnings':[]}
            with patch.dict(os.environ,{'CONNECTOR_ENABLED':'true'}),patch.object(api,'search_browser',return_value=payload):
                code,result=self.post('/api/v1/search',{'zip':'63129','queries':['milk'],'allow_unverified_location':True})
            self.assertEqual(code,200);self.check(result,{'$ref':'#/components/schemas/SearchResponse'})
            catalog=json.load(urlopen(self.url+'/api/v1/catalog?zip=63129&queries=milk&allow_unverified_location=true'))
            self.check(catalog,{'$ref':'#/components/schemas/CatalogResponse'})
            code,cart=self.post('/api/v1/cart',{'lines':[{'search_id':catalog['search_id'],'item_id':item['id'],'quantity':2}]})
            self.assertEqual(code,200);self.check(cart,{'$ref':'#/components/schemas/CartResponse'})
            code,selection=self.post('/api/v1/select',{'search_id':result['search_id'],'item_ids':[item['id']]})
            self.assertEqual(code,200);self.check(selection,{'$ref':'#/components/schemas/SelectionResponse'})
            code,error=self.post('/api/v1/search',{'zip':'bad','queries':[]})
            self.assertEqual(code,422);self.check(error,{'$ref':'#/components/schemas/Error'})
    def test_comparison_contract(self):
        from provider_comparison import Comparison
        from search_cache import SearchCache
        import threading
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            catalog=Catalog(folder+'/test.sqlite')
            comparison=Comparison(catalog,SearchCache(),threading.Lock(),Mock())
            with patch.object(api,'COMPARISON',comparison):
                code,data=self.post('/api/v1/compare',{'zip':'63110','queries':['milk'],'mode':'catalog'})
                self.assertEqual(code,200);self.check(data,{'$ref':'#/components/schemas/ComparisonResponse'})
                self.assertEqual([p['status'] for p in data['providers']],['not_cached','not_cached'])
                code,all_sources=self.post('/api/v1/compare',{'zip':'63110','queries':['milk'],'mode':'catalog','providers':['instacart','mealme_satsuma','vercel_kroger','vercel_square']})
                self.assertEqual(code,200);self.check(all_sources,{'$ref':'#/components/schemas/ComparisonResponse'})
                self.assertEqual(len(all_sources['providers']),4)
                self.assertTrue(all(p['status']=='not_cached' for p in all_sources['providers']))
                self.assertEqual(self.post('/api/v1/compare',{'zip':'63110','queries':['milk'],'providers':['other']})[0],422)
                self.assertEqual(self.post('/api/v1/compare',{'zip':'63110','queries':['milk'],'mode':'write'})[0],422)

    def test_docs_routes(self):
        for path in ['/docs','/swagger']:
            body=urlopen(self.url+path).read().decode();self.assertIn('SwaggerUIBundle',body)
        self.assertIn('InstaSearch API Contract',urlopen(self.url+'/android-contract.md').read().decode())

if __name__=='__main__':unittest.main()

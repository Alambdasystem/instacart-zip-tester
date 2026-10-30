import unittest
from search_cache import SearchCache

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.cache=SearchCache(clock=lambda:self.now)
        self.key=self.cache.key('63129',[' Milk '],'aldi',False)
        self.payload={'items':[{'id':'milk','retailer':'aldi','price':2.78}]}
    def test_normalized_key_and_location_isolation(self):
        self.assertEqual(self.key,self.cache.key('63129',['milk'],'aldi',False))
        self.assertNotEqual(self.key,self.cache.key('63101',['milk'],'aldi',False))
        self.assertNotEqual(self.key,self.cache.key('63129',['milk'],'aldi',True))
    def test_non_sliding_expiry_and_copy_isolation(self):
        self.cache.put(self.key,self.payload);self.now=299
        payload,meta=self.cache.get(self.key)
        self.assertTrue(meta['hit']);self.assertEqual(meta['expires_in'],1)
        payload['items'][0]['price']=0
        self.assertEqual(self.cache.get(self.key)[0]['items'][0]['price'],2.78)
        self.now=300;self.assertIsNone(self.cache.get(self.key))
    def test_store_epoch_expires_newer_products_together(self):
        self.cache.put(self.key,self.payload);self.now=240
        eggs=self.cache.key('63129',['eggs'],'aldi',False)
        meta=self.cache.put(eggs,self.payload)
        self.assertEqual(meta['expires_in'],60)
        self.now=301
        self.assertIsNone(self.cache.get(self.key));self.assertIsNone(self.cache.get(eggs))
    def test_invalidation_includes_cross_store_queries(self):
        key=self.cache.key('63129',['milk'],'',True)
        self.cache.put(key,self.payload)
        self.assertEqual(self.cache.invalidate('aldi','63129'),1)
        self.assertIsNone(self.cache.get(key))

if __name__=='__main__':unittest.main()

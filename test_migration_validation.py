import unittest
from migration_validation import validate_pair
class MigrationGateTests(unittest.TestCase):
    def pair(self):
        item={'id':'1','price':2.99,'size':'1 gal','currency':'USD','retailer':'aldi','image_url':'https://www.instacart.com/a.jpg'}
        result={'status':'ok','items':[item],'location':{'verified':True,'context_id':'ctx:test','observed_zip':'63129'},'coverage_complete':True}
        import copy
        return {'direct_http':copy.deepcopy(result),'browser':copy.deepcopy(result)}
    def test_matching_counts_do_not_hide_different_location(self):
        p=self.pair();p['direct_http']['location']['context_id']='ctx:other'
        self.assertEqual(validate_pair(p)['decision'],'BLOCKED')
    def test_missing_or_different_price_blocks(self):
        for price in [None,3.18,float('nan')]:
            p=self.pair();p['direct_http']['items'][0]['price']=price
            self.assertFalse(validate_pair(p)['gates']['same_price_size_currency'])
    def test_extra_product_blocks_subset_parity(self):
        p=self.pair();p['browser']['items'].append({**p['browser']['items'][0],'id':'2'})
        self.assertEqual(validate_pair(p)['decision'],'BLOCKED')
    def test_perfect_pair_is_not_rollout_approval(self):
        p=self.pair();r=validate_pair(p)
        self.assertEqual(r['decision'],'PAIR_PARITY_ONLY');self.assertFalse(r['release_approved'])
    def test_unknown_coverage_blocks(self):
        p=self.pair();p['direct_http'].pop('coverage_complete')
        self.assertEqual(validate_pair(p)['decision'],'BLOCKED')

    def test_encoded_hydration_counts_unique_records_and_missing_details(self):
        import json
        from urllib.parse import quote
        from migration_validation import audit_html
        payload={'items':[{'__typename':'ItemsItem','id':'a'},{'__typename':'ItemsItem','id':'a'}], 'results':{'__typename':'SearchRetailerResults','retailerId':'r','shopId':'s','itemIds':['a','b']}}
        raw='<script type="application/json">'+quote(json.dumps(payload))+'</script>'
        result=audit_html(raw)
        self.assertEqual(result['fully_populated_unique_items'],1)
        self.assertEqual(result['references_missing_item_details'],1)

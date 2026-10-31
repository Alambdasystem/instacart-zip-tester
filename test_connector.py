import unittest
from unittest.mock import Mock,patch
from connector import normalize,balanced_items,wait_for_catalog,establish_location,observed_location,discovered_retailers,ConnectorError
from api_server import validate,merge_parts


def cards(store,n):
    return [{'name':store+' milk '+str(i),'text':'Current price: $2.99\n1 gal','url':'/products/'+str(i)+'?retailerSlug='+store} for i in range(n)]

class Empty:
    def count(self):return 0
    @property
    def first(self):return self
    def wait_for(self,**kwargs):raise TimeoutError()

class RenderPage:
    def __init__(self):self.t=0;self.calls=0
    def locator(self,_):return Mock(inner_text=Mock(return_value='Current price: $2.99'))
    def get_by_role(self,*a,**kw):return Empty()
    def evaluate(self,script):
        self.calls+=1
        c=cards('kroger',7)
        if self.t>=3:c+=cards('aldi',7)
        return {'cards':c,'retailers':[{'url':'/store/kroger/s?k=milk','name':'Kroger'}]+([{'url':'/store/aldi/s?k=milk','name':'ALDI'}] if self.t>=3 else [])}
    def wait_for_timeout(self,ms):self.t+=ms/1000

class ConnectorTests(unittest.TestCase):
    def test_all_retailers_survive_first_shelf_and_cap(self):
        data=normalize(cards('kroger',100)+cards('aldi',30)+cards('costco',10),'milk','',None)
        self.assertEqual(len(data),24)
        self.assertEqual([i['retailer'] for i in data[:3]],['kroger','aldi','costco'])
        self.assertEqual({s:sum(i['retailer']==s for i in data) for s in ['kroger','aldi','costco']},{'kroger':8,'aldi':8,'costco':8})
    def test_cross_retailer_identical_names_remain_distinct(self):
        raw=[{'name':'Milk','text':'Current price: $2.99','url':'/products/1?retailerSlug='+s} for s in ['aldi','kroger']]
        result=normalize(raw,'milk','',None)
        self.assertEqual(len(result),2);self.assertNotEqual(result[0]['id'],result[1]['id'])
    def test_waits_for_later_retailer_and_hydration(self):
        page=RenderPage();state,settled=wait_for_catalog(page,clock=lambda:page.t)
        self.assertTrue(settled);self.assertGreaterEqual(page.t,8)
        self.assertEqual(len(state['cards']),14)
        discovered=discovered_retailers(state,normalize(state['cards'],'milk','',None))
        self.assertEqual([r['id'] for r in discovered],['kroger','aldi'])
        self.assertTrue(all(not r['branch_verified'] for r in discovered))
    def test_empty_does_not_count_as_stable_success(self):
        page=RenderPage();page.evaluate=Mock(return_value={'cards':[],'retailers':[]})
        state,settled=wait_for_catalog(page,timeout=3,clock=lambda:page.t)
        self.assertFalse(settled);self.assertEqual(state['cards'],[])
    def test_stop_on_provider_screening(self):
        page=RenderPage();page.locator=Mock(return_value=Mock(inner_text=Mock(return_value='Verify you are human')))
        with self.assertRaises(ConnectorError) as e:wait_for_catalog(page,clock=lambda:page.t)
        self.assertEqual(e.exception.status,'provider_blocked')
    def test_unrelated_header_zip_cannot_verify_location(self):
        page=Mock();page.get_by_role.return_value=Empty()
        page.locator.return_value.inner_text.return_value='Special offer 63129'
        self.assertIsNone(observed_location(page))
        with self.assertRaises(ConnectorError):establish_location(page,'63129',False)
        self.assertEqual(establish_location(page,'63129',True),(None,False))
    def test_detached_address_control_is_unverified_not_timeout(self):
        page=Mock();page.get_by_role.return_value.count.return_value=1
        page.get_by_role.return_value.first.get_attribute.side_effect=TimeoutError('detached')
        self.assertIsNone(observed_location(page))
    def test_body_replacement_during_catalog_render_recovers(self):
        page=RenderPage();page.locator=Mock(return_value=Mock(inner_text=Mock(side_effect=[TimeoutError('replaced')]+['Current price: $2.99']*40)))
        state,settled=wait_for_catalog(page,clock=lambda:page.t)
        self.assertTrue(settled);self.assertEqual(len(state['cards']),14)
    def test_unverified_collection_does_not_open_required_address_form(self):
        page=Mock();page.locator.return_value.inner_text.return_value=''
        control=Mock();control.count.return_value=1;control.first.get_attribute.return_value='current address: 67570.'
        page.get_by_role.side_effect=lambda role,**kw:control if role=='button' else Empty()
        self.assertEqual(establish_location(page,'63129',True),('67570',False))
        control.first.click.assert_not_called()
    def test_preserves_actual_different_zip(self):
        page=Mock();page.locator.return_value.inner_text.return_value='';control=Mock();control.count.return_value=1;control.first.get_attribute.return_value='current address: 67570. Or select a new address.'
        page.get_by_role.side_effect=lambda role,**kw:control if role=='button' else Mock(count=Mock(return_value=1),first=Mock(inner_text=Mock(return_value='Choose address')))
        self.assertEqual(establish_location(page,'63129',True),('67570',False))
    def test_retailer_slugs_safe_and_not_aldi_only(self):
        self.assertEqual(validate({'zip':'63129','query':'milk','retailer':'costco'})[2],'costco')
        for value in ['../../secrets','https://example.com','ALDI',[],None]:
            with self.assertRaises(ValueError):validate({'zip':'63129','query':'milk','retailer':value})
    def test_diagnostics_survive_cache_merge_and_geo_change(self):
        p={'items':[],'location':{'requested_zip':'63129','observed_zip':'67570','verified':False},'retailers':[{'id':'aldi'}],'diagnostics':[{'settled':True}]}
        q={**p,'location':{'requested_zip':'63129','observed_zip':'63129','verified':True}}
        m={'age_seconds':0,'expires_in':300}
        result,_=merge_parts([(p,m),(q,m)],[],['milk','eggs'])
        self.assertFalse(result['location']['verified']);self.assertIsNone(result['location']['observed_zip'])
        self.assertEqual(len(result['retailers']),1);self.assertEqual(len(result['diagnostics']),2)

if __name__=='__main__':unittest.main()

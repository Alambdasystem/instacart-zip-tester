import unittest,io,json
from urllib.error import HTTPError
from unittest.mock import Mock
from direct_http import parse_html,search_http,compare_results
HTML='''<script>Current price: $0.01</script><a data-item-card-button="true" href="/products/123-whole-milk?retailerSlug=aldi"><img alt="Whole Milk" src="https://www.instacart.com/milk.jpg"><span>Current price: $3.18</span><span>1 gal</span></a>'''
class DirectTests(unittest.TestCase):
    def test_extracts_cards_images_prices_without_claiming_location(self):
        row=parse_html(HTML,'milk')[0]
        self.assertEqual(row['price'],3.18);self.assertEqual(row['size'],'1 gal')
        self.assertEqual(row['retailer'],'aldi');self.assertIsNone(row['observed_zip'])
        self.assertFalse(row['selectable']);self.assertEqual(row['source'],'instacart_public_http')
    def test_ignores_script_and_unrelated_cards(self):
        self.assertEqual(parse_html(HTML,'chicken'),[])
        self.assertEqual(parse_html('<script>'+HTML.split('</script>',1)[1]+'</script>','milk'),[])
    def test_hydrated_image_template_preserves_card_image(self):
        payload=[{'__typename':'RetailersRetailer','id':'1','slug':'aldi'},
                 {'__typename':'SearchRetailerResults','retailerId':'1','itemIds':['item1']},
                 {'__typename':'ItemsItem','id':'item1','evergreenUrl':'123-whole-milk','name':'Whole Milk','size':'1 gal',
                  'price':{'viewSection':{'itemCard':{'priceString':'$3.18'}}},
                  'viewSection':{'itemImage':{'templateUrl':'https://www.instacart.com/image-server/{width=}x{height=}/milk.jpg'}}}]
        script='<script type="application/json">'+json.dumps(payload)+'</script>'
        rows=parse_html(HTML+script,'milk')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['image_url'],'https://www.instacart.com/milk.jpg')
        self.assertEqual(parse_html(script,'milk')[0]['image_url'],'https://www.instacart.com/image-server/197x197/milk.jpg')
    def test_http_errors_do_not_trigger_retries(self):
        opener=Mock(side_effect=HTTPError('https://www.instacart.com',403,'Forbidden',{},None))
        result=search_http('63129','milk',opener=opener)
        self.assertEqual(result['status'],'http_error');opener.assert_called_once()
    def response(self,body):
        r=Mock();r.status=200;r.headers={'Content-Type':'text/html'};r.read.side_effect=io.BytesIO(body.encode()).read;r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False);return r
    def test_screening_stops_without_extracting(self):
        result=search_http('63129','milk',opener=Mock(return_value=self.response('Verify you are human'+HTML)))
        self.assertEqual(result['status'],'provider_blocked');self.assertEqual(result['items'],[])
    def test_same_price_and_id_do_not_establish_same_location(self):
        rows=parse_html(HTML,'milk');summary=compare_results({'items':rows},{'items':rows})
        self.assertEqual(summary['matching_product_retailer_ids'],1)
        self.assertEqual(summary['equal_prices_on_matching_ids'],1)
        self.assertFalse(summary['equivalent_for_local_search'])

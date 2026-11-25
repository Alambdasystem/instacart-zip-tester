import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from northfork_checkout import CheckoutError, CheckoutSessions, NorthforkClient, retailer_handoff_url


class FakeNorthfork:
    def __init__(self, store_id='1727', retailer='schnucks', provider='schnucks-online', redirect='https://www.schnucks.com/cart?transfer=demo'):
        self.store_id, self.retailer, self.provider, self.redirect = store_id, retailer, provider, redirect
        self.pushed = None

    def stores(self, zip_code):
        return {'data': [{'identifier': self.store_id, 'provider': self.provider, 'name': 'Schnucks',
                          'retailer_key': self.retailer, 'zip': zip_code}]}

    def resolve(self, store, ingredients):
        return {'data': {'store': {'identifier': store['identifier'], 'provider': self.provider}, 'ingredients': [
            {'name': entry['name'], 'products': [{'id': 20653045, 'external_id': '20653045',
                'name': 'Whole Milk', 'brand': 'Schnucks', 'price': 399, 'currency_code': 'USD',
                'size': '1 gal', 'in_stock': True}]} for entry in ingredients]}}

    def push(self, store, zip_code, product_quantities):
        self.pushed = (store, zip_code, product_quantities)
        return {'handoff_status': 302, 'location': self.redirect}


class NorthforkCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeNorthfork()
        self.checkout = CheckoutSessions(client_factory=lambda: self.client)
        self.search = {'location': {'requested_zip': '63129'}, 'items': [
            {'id': 'xbyte-milk', 'name': 'Whole Milk', 'brand': 'Schnucks', 'size': '1 gal',
             'retailer': 'schnucks', 'source_provider': 'xbyte_foodspark', 'price': 3.99}]}


    def test_store_discovery_requests_all_configured_retailers(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'{"data": []}'
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()
        opener = Opener()
        with patch('northfork_checkout.build_opener', return_value=opener):
            NorthforkClient(api_key='test-token').stores('63129')
        self.assertEqual(parse_qs(urlsplit(opener.request.full_url).query),
                         {'providers': ['all'], 'offset': ['0'], 'limit': ['10'], 'zip': ['63129']})

    def test_complete_resolve_to_selected_retailer_handoff(self):
        stores = self.checkout.discover_stores('63129')
        self.assertEqual(stores['source_provider'], 'northfork')
        self.assertEqual(stores['stores'][0]['identifier'], '1727')
        self.assertEqual(stores['retailer_scope'], 'enabled_non_instacart_northfork_retailers')
        self.assertTrue(stores['checkout_available'])
        self.assertEqual(stores['excluded_instacart_store_count'], 0)
        resolved = self.checkout.resolve(self.search, {'store_id': '1727', 'store_provider': 'schnucks-online', 'lines': [{'item_id': 'xbyte-milk', 'quantity': 2}]})
        self.assertEqual(resolved['status'], 'products_resolved')
        self.assertFalse(resolved['cart_updated'])
        self.assertEqual(resolved['lines'][0]['recommended_product_id'], '20653045')
        handoff = self.checkout.handoff(resolved['checkout_id'], [{'item_id': 'xbyte-milk', 'product_id': '20653045', 'quantity': 2}])
        self.assertEqual(handoff['status'], 'handoff_ready')
        self.assertEqual(handoff['retailer_provider'], 'schnucks-online')
        self.assertIn('schnucks.com', handoff['handoff_url'])
        self.assertNotIn('marketplace', handoff)
        self.assertTrue(handoff['handoff_started'])
        self.assertFalse(handoff['cart_updated'])
        self.assertFalse(handoff['order_placed'])
        self.assertEqual(self.client.pushed[1:], ('63129', [('20653045', 2)]))


    def test_instacart_rows_are_excluded_and_cannot_be_selected(self):
        self.client.provider = 'instacart'
        stores = self.checkout.discover_stores('63129')
        self.assertEqual(stores['stores'], [])
        self.assertFalse(stores['checkout_available'])
        self.assertEqual(stores['excluded_instacart_store_count'], 1)
        with self.assertRaises(CheckoutError) as error:
            self.checkout.resolve(self.search, {'store_id': '1727', 'store_provider': 'instacart',
                                                'lines': [{'item_id': 'xbyte-milk'}]})
        self.assertEqual(error.exception.code, 'unsupported_retailer')

    def test_rejects_wrong_retailer_store(self):
        self.client.retailer = 'walmart'
        with self.assertRaises(CheckoutError) as error:
            self.checkout.resolve(self.search, {'store_id': '1727', 'store_provider': 'schnucks-online', 'lines': [{'item_id': 'xbyte-milk'}]})
        self.assertEqual(error.exception.code, 'store_not_found')
        self.assertIsNone(self.client.pushed)

    def test_rejects_non_xbyte_source(self):
        self.search['items'][0]['source_provider'] = 'instacart_browser'
        with self.assertRaises(CheckoutError) as error:
            self.checkout.resolve(self.search, {'store_id': '1727', 'store_provider': 'schnucks-online', 'lines': [{'item_id': 'xbyte-milk'}]})
        self.assertEqual(error.exception.code, 'xbyte_source_required')

    def test_handoff_only_accepts_products_returned_by_northfork(self):
        resolved = self.checkout.resolve(self.search, {'store_id': '1727', 'store_provider': 'schnucks-online', 'lines': [{'item_id': 'xbyte-milk'}]})
        with self.assertRaises(CheckoutError) as error:
            self.checkout.handoff(resolved['checkout_id'], [{'item_id': 'xbyte-milk', 'product_id': 'made-up'}])
        self.assertEqual(error.exception.code, 'invalid_selection')
        self.assertIsNone(self.client.pushed)

    def test_rejects_unsafe_handoff_url(self):
        self.client.redirect = 'javascript:alert(1)'
        resolved = self.checkout.resolve(self.search, {'store_id': '1727', 'store_provider': 'schnucks-online', 'lines': [{'item_id': 'xbyte-milk'}]})
        with self.assertRaises(CheckoutError) as error:
            self.checkout.handoff(resolved['checkout_id'], [{'item_id': 'xbyte-milk', 'product_id': '20653045'}])
        self.assertEqual(error.exception.code, 'handoff_failed')
        self.assertFalse(retailer_handoff_url('javascript:alert(1)'))
        self.assertFalse(retailer_handoff_url('https://user:pass@shop.example/cart'))
        self.assertTrue(retailer_handoff_url('https://shop.example/cart'))
        self.assertFalse(retailer_handoff_url('https://www.instacart.com/store/cart'))

    def test_handoff_is_one_shot(self):
        resolved = self.checkout.resolve(self.search, {'store_id': '1727', 'store_provider': 'schnucks-online', 'lines': [{'item_id': 'xbyte-milk'}]})
        line = [{'item_id': 'xbyte-milk', 'product_id': '20653045'}]
        self.checkout.handoff(resolved['checkout_id'], line)
        with self.assertRaises(CheckoutError) as error:
            self.checkout.handoff(resolved['checkout_id'], line)
        self.assertEqual(error.exception.code, 'handoff_already_started')
        self.assertEqual(self.client.pushed[2], [('20653045', 1)])


if __name__ == '__main__':
    unittest.main()

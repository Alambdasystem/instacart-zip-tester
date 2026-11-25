import unittest

from northfork_checkout import CheckoutError, CheckoutSessions, instacart_handoff_url


class FakeNorthfork:
    def __init__(self, store_id='1727', retailer='schnucks', redirect='https://www.instacart.com/store/checkout?transfer=demo'):
        self.store_id, self.retailer, self.redirect = store_id, retailer, redirect
        self.pushed = None

    def stores(self, zip_code):
        return {'data': [{'identifier': self.store_id, 'provider': 'instacart', 'name': 'Schnucks',
                          'retailer_key': self.retailer, 'zip': zip_code}]}

    def resolve(self, store, ingredients):
        return {'data': {'store': {'identifier': store['identifier'], 'provider': 'instacart'}, 'ingredients': [
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

    def test_complete_resolve_to_instacart_handoff(self):
        stores = self.checkout.discover_stores('63129')
        self.assertEqual(stores['source_provider'], 'northfork')
        self.assertEqual(stores['stores'][0]['identifier'], '1727')
        resolved = self.checkout.resolve(self.search, {'store_id': '1727', 'lines': [{'item_id': 'xbyte-milk', 'quantity': 2}]})
        self.assertEqual(resolved['status'], 'products_resolved')
        self.assertFalse(resolved['cart_updated'])
        self.assertEqual(resolved['lines'][0]['recommended_product_id'], '20653045')
        handoff = self.checkout.handoff(resolved['checkout_id'], [{'item_id': 'xbyte-milk', 'product_id': '20653045', 'quantity': 2}])
        self.assertEqual(handoff['status'], 'handoff_ready')
        self.assertTrue(handoff['handoff_started'])
        self.assertFalse(handoff['cart_updated'])
        self.assertFalse(handoff['order_placed'])
        self.assertEqual(self.client.pushed[1:], ('63129', [('20653045', 2)]))

    def test_rejects_wrong_retailer_store(self):
        self.client.retailer = 'walmart'
        with self.assertRaises(CheckoutError) as error:
            self.checkout.resolve(self.search, {'store_id': '1727', 'lines': [{'item_id': 'xbyte-milk'}]})
        self.assertEqual(error.exception.code, 'store_not_found')
        self.assertIsNone(self.client.pushed)

    def test_rejects_non_xbyte_source(self):
        self.search['items'][0]['source_provider'] = 'instacart_browser'
        with self.assertRaises(CheckoutError) as error:
            self.checkout.resolve(self.search, {'store_id': '1727', 'lines': [{'item_id': 'xbyte-milk'}]})
        self.assertEqual(error.exception.code, 'xbyte_source_required')

    def test_handoff_only_accepts_products_returned_by_northfork(self):
        resolved = self.checkout.resolve(self.search, {'store_id': '1727', 'lines': [{'item_id': 'xbyte-milk'}]})
        with self.assertRaises(CheckoutError) as error:
            self.checkout.handoff(resolved['checkout_id'], [{'item_id': 'xbyte-milk', 'product_id': 'made-up'}])
        self.assertEqual(error.exception.code, 'invalid_selection')
        self.assertIsNone(self.client.pushed)

    def test_rejects_unsafe_handoff_url(self):
        self.client.redirect = 'https://example.org/steal'
        resolved = self.checkout.resolve(self.search, {'store_id': '1727', 'lines': [{'item_id': 'xbyte-milk'}]})
        with self.assertRaises(CheckoutError) as error:
            self.checkout.handoff(resolved['checkout_id'], [{'item_id': 'xbyte-milk', 'product_id': '20653045'}])
        self.assertEqual(error.exception.code, 'handoff_failed')
        self.assertFalse(instacart_handoff_url('javascript:alert(1)'))

    def test_handoff_is_one_shot(self):
        resolved = self.checkout.resolve(self.search, {'store_id': '1727', 'lines': [{'item_id': 'xbyte-milk'}]})
        line = [{'item_id': 'xbyte-milk', 'product_id': '20653045'}]
        self.checkout.handoff(resolved['checkout_id'], line)
        with self.assertRaises(CheckoutError) as error:
            self.checkout.handoff(resolved['checkout_id'], line)
        self.assertEqual(error.exception.code, 'handoff_already_started')
        self.assertEqual(self.client.pushed[2], [('20653045', 1)])


if __name__ == '__main__':
    unittest.main()

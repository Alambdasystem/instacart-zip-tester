"""Synthetic fixtures for XBYTE-01 arithmetic and offer eligibility; no live calls."""
from decimal import Decimal
import unittest

from savings import SavingsError, calculate, validate_offer


class XByteSavingsTests(unittest.TestCase):
    def offer(self, retailer='target', price='4.19'):
        return {
            'id': 'observation:' + retailer,
            'name': 'Example Brand Whole Milk', 'brand': 'Example Brand',
            'size': '1 gal', 'pack_count': 1, 'price': price, 'currency': 'USD',
            'retailer': retailer, 'provider': 'instacart', 'source': 'foodspark',
            'data_provider': 'xbyte_foodspark', 'platform': 'instacart',
            'price_source': 'instacart_marketplace',
            'provider_product_id': 'vendor-product-1', 'upc': '012345678905',
            'price_basis': 'per_item', 'price_basis_verified': True, 'price_unit': 'item',
            'source_freshness': 'fresh', 'captured_at': '2026-11-17T12:00:00Z',
            'selectable': True,
        }

    def fixture(self):
        a, b = self.offer(), self.offer('schnucks', '3.69')
        return {'s': {'created': 1000, 'expires': 1300, 'payload': {
            'items': [a, b], 'location': {
                'requested_zip': '63131', 'observed_zip': '63131',
                'verified': True, 'context_id': 'zip:63131',
            },
        }}}

    def body(self):
        return {'offers': [
            {'search_id': 's', 'item_id': 'observation:target'},
            {'search_id': 's', 'item_id': 'observation:schnucks'},
        ], 'quantity': 3}

    def assert_code(self, code, call, *args, **kwargs):
        with self.assertRaises(SavingsError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_verified_equivalent_observations_use_decimal_totals(self):
        result = calculate(self.fixture(), self.body(), now=1001)
        self.assertEqual(result['original_subtotal'], 12.57)
        self.assertEqual(result['replacement_subtotal'], 11.07)
        self.assertEqual(result['savings'], 1.50)
        self.assertEqual(result['savings_percent'], 11.93)
        self.assertEqual(result['offers'][0]['price_source'], 'instacart_marketplace')
        self.assertEqual(result['offers'][0]['captured_at'], '2026-11-17T12:00:00Z')

    def test_explicit_pack_and_brand_and_size_all_must_match(self):
        for field, value in (('brand', 'Other Brand'), ('size', '0.5 gal'),
                             ('pack_count', 2), ('pack_count', None),
                             ('pack_count', True), ('pack_count', 0), ('brand', None)):
            fixture = self.fixture()
            fixture['s']['payload']['items'][1][field] = value
            with self.subTest(field=field, value=value):
                self.assert_code('product_mismatch', calculate, fixture, self.body(), 1001)

    def test_display_unit_spelling_normalizes_without_guessing_conversions(self):
        fixture = self.fixture()
        fixture['s']['payload']['items'][1]['size'] = '1.0 GALLON'
        self.assertEqual(calculate(fixture, self.body(), 1001)['savings'], 1.5)
        fixture['s']['payload']['items'][1]['size'] = '128 fl oz'
        self.assert_code('product_mismatch', calculate, fixture, self.body(), 1001)

    def test_identical_display_names_and_observation_ids_are_not_product_identity(self):
        fixture = self.fixture()
        for offer in fixture['s']['payload']['items']:
            offer['upc'] = None
            offer['provider_product_id'] = offer['id']
        self.assert_code('product_mismatch', calculate, fixture, self.body(), 1001)
        for offer in fixture['s']['payload']['items']:
            offer['provider_product_id'] = 'synthetic:same-product'
        self.assert_code('product_mismatch', calculate, fixture, self.body(), 1001)

    def test_real_provider_id_or_upc_can_match_but_conflicting_upc_cannot(self):
        fixture = self.fixture()
        for offer in fixture['s']['payload']['items']:
            offer['upc'] = None
        self.assertEqual(calculate(fixture, self.body(), 1001)['savings'], 1.5)
        fixture = self.fixture()
        for offer in fixture['s']['payload']['items']:
            offer['provider_product_id'] = None
        self.assertEqual(calculate(fixture, self.body(), 1001)['savings'], 1.5)
        fixture = self.fixture()
        fixture['s']['payload']['items'][1]['upc'] = '098765432109'
        self.assert_code('product_mismatch', calculate, fixture, self.body(), 1001)

    def test_numeric_upc_does_not_silently_lose_leading_zero_identity(self):
        fixture = self.fixture()
        for offer in fixture['s']['payload']['items']:
            offer['upc'] = 12345678905
            offer['provider_product_id'] = None
        self.assert_code('product_mismatch', calculate, fixture, self.body(), 1001)

    def test_platform_and_price_source_are_not_inferred_or_merged(self):
        for field, value in (('platform', 'doordash'), ('platform', None),
                             ('price_source', 'direct_retailer'), ('price_source', None)):
            fixture = self.fixture()
            fixture['s']['payload']['items'][1][field] = value
            with self.subTest(field=field, value=value):
                self.assert_code('price_source_mismatch', calculate, fixture, self.body(), 1001)

    def test_browser_and_xbyte_identity_cannot_be_cross_matched(self):
        fixture = self.fixture()
        offer = fixture['s']['payload']['items'][1]
        offer['source'] = 'instacart'
        offer.pop('data_provider')
        self.assert_code('provider_unsupported', calculate, fixture, self.body(), 1001)

    def test_local_quote_expiry_and_vendor_freshness_are_independent(self):
        self.assert_code('price_expired', calculate, self.fixture(), self.body(), 1300)
        for freshness, code in (('stale', 'source_stale'), ('unknown', 'source_freshness_unknown'),
                                (None, 'source_freshness_unknown')):
            fixture = self.fixture()
            fixture['s']['payload']['items'][1]['source_freshness'] = freshness
            with self.subTest(freshness=freshness):
                self.assert_code(code, calculate, fixture, self.body(), 1001)

    def test_unknown_or_nonfinite_money_is_never_zero(self):
        for price, code in ((None, 'price_unavailable'), ('', 'price_unavailable'),
                            ('NaN', 'invalid_price'), ('Infinity', 'invalid_price'),
                            (float('-inf'), 'invalid_price'), (-1, 'invalid_price'),
                            (True, 'invalid_price'), ('1.001', 'invalid_price')):
            with self.subTest(price=price):
                self.assert_code(code, validate_offer, {**self.offer(), 'price': price})
        self.assertEqual(validate_offer({**self.offer(), 'price': 0}), Decimal('0'))
        self.assertEqual(validate_offer(self.offer()), Decimal('4.19'))

    def test_unknown_units_estimates_and_unselectable_items_cannot_total(self):
        for patch, code in (({'price_basis_verified': False}, 'price_unit_unverified'),
                             ({'price_basis': 'per_lb'}, 'weighted_quantity_required'),
                             ({'price_unit': None}, 'price_unit_unverified'),
                             ({'price_kind': 'starting_at'}, 'conditional_price'),
                             ({'price_status': 'estimated'}, 'conditional_price'),
                             ({'price_label': 'From $4.19'}, 'conditional_price'),
                             ({'is_estimated': True}, 'conditional_price'),
                             ({'promotion_text': 'Must buy two'}, 'conditional_price'),
                             ({'selectable': False}, 'offer_unselectable'),
                             ({'currency': 'CAD'}, 'price_unit_mismatch')):
            with self.subTest(patch=patch):
                self.assert_code(code, validate_offer, {**self.offer(), **patch})

    def test_legacy_browser_validation_does_not_invent_new_basis_requirement(self):
        self.assertEqual(validate_offer({'price': 2.78, 'currency': 'USD'}), Decimal('2.78'))
        self.assert_code('offer_unselectable', validate_offer,
                         {'price': 2.78, 'currency': 'USD', 'selectable': False})
        self.assert_code('price_unavailable', validate_offer, {'currency': 'USD'})


if __name__ == '__main__':
    unittest.main()

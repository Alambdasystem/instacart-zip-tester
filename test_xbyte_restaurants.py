"""Offline restaurant tests using wholly synthetic data. Never issue requests.

All fixtures, restaurant names, addresses, URLs, prices and edge-case payloads
are fictional test inputs. They do not establish real restaurant coverage.
"""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from connector import ConnectorError
import xbyte_restaurants as restaurants

FIXTURES = Path(__file__).with_name('fixtures') / 'xbyte'
URL = 'https://www.yelp.com/biz/fixture-menu-cafe'
PILOT_TERMS = ['burgers', 'cheeseburger', 'pizza', 'Mexican', 'burrito', 'tacos',
               'wings', 'healthy', 'grilled chicken salad', 'breakfast',
               'breakfast burrito', 'sushi', 'pasta', 'sports bar', 'happy hour']


def fixture(name):
    return json.loads((FIXTURES / ('restaurant_synthetic_' + name + '.json')).read_text())


def synthetic_discovery(has_menu=True):
    return {'request_log': {'status': 200}, 'response': {'results': [
        {'restaurant_name': 'Fixture Diner', 'restaurant_url': URL, 'has_menu': has_menu}
    ], 'message': 'Success'}}


def synthetic_menu(products):
    return {'request_log': {'status': 200}, 'response': {'results': {
        'restaurant_name': 'Fixture Diner', 'restaurant_url': URL,
        'address': 'Fixture address; not a coverage claim',
        'categories': [{'category': 'Fixture menu', 'products': products}],
    }, 'message': 'Success'}}


def item(name, price, **fields):
    return {'product_name': name, 'product_price': price, 'product_description': '', **fields}


class RestaurantContractTests(unittest.TestCase):
    def setUp(self):
        self.flags = patch.dict(os.environ, {'FOODSPARK_ENABLED': 'true', 'FOODSPARK_RESTAURANTS_ENABLED': 'true'})
        self.flags.start()
        restaurants._DISCOVERIES.clear()
        upstream_patch = patch.object(restaurants, 'request_json')
        self.upstream = upstream_patch.start()
        self.addCleanup(upstream_patch.stop)
        self.addCleanup(self.flags.stop)

    def discover(self, payload=None):
        self.upstream.return_value = payload or synthetic_discovery()
        return restaurants.discovery('63131', 'pizza')

    def menu(self, products, terms=None, budget=None, budget_operator='lt'):
        discovery = self.discover()
        self.upstream.return_value = synthetic_menu(products)
        return restaurants.menu(discovery['discovery_id'], URL, 'yelp', terms=terms, budget=budget,
                                budget_operator=budget_operator)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(ConnectorError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, code)

    def test_synthetic_listing_is_discovery_only_with_unknown_location(self):
        result = self.discover(fixture('listing'))
        self.assertEqual(len(result['items']), 3)
        self.assertEqual(result['items'][0]['name'], 'Fixture Cafe One')
        self.assertIsNone(result['items'][0]['location']['address'])
        self.assertFalse(result['items'][0]['location']['verified'])
        self.assertIsNone(result['items'][0]['captured_at'])
        self.assertEqual(result['items'][0]['freshness'], 'unknown')
        self.assertFalse(result['items'][2]['has_menu'])
        self.upstream.assert_called_once()
        path, params = self.upstream.call_args.args
        self.assertEqual(path, '/restaurant_listing')
        self.assertEqual(params['zip-code'], '63131')
        self.assertNotIn('zipcode', params)
        self.assertNotIn('api_key', params)

    def test_all_pilot_keywords_pass_through_without_fetching_menus(self):
        self.upstream.return_value = fixture('listing')
        for term in PILOT_TERMS:
            with self.subTest(term=term):
                restaurants.discovery('63131', term, page_no=10)
                self.assertEqual(self.upstream.call_args.args[1]['search_keyword'], term)
                self.assertEqual(self.upstream.call_args.args[1]['page_no'], 10)
                self.assertEqual(self.upstream.call_args.args[0], '/restaurant_listing')
        self.assertEqual(self.upstream.call_count, len(PILOT_TERMS))

    def test_broad_query_does_not_filter_restaurant_name_as_menu_match(self):
        self.upstream.return_value = fixture('listing')
        result = restaurants.discovery('63131', 'healthy')
        self.assertEqual(len(result['items']), 3)
        self.assertIn('do not establish a menu-item match', result['warnings'][0])

    def test_invalid_pagination_is_rejected_before_vendor_call(self):
        for page in [1, -10, True, 1001, '10']:
            with self.subTest(page=page):
                self.assert_code('invalid_request', restaurants.discovery, '63131', 'pizza', page_no=page)
        self.upstream.assert_not_called()

    def test_no_match_listing_is_an_explicit_outcome(self):
        self.upstream.return_value = {'response': {'results': []}}
        result = restaurants.discovery('63131', 'zzzx-no-such-food-123')
        self.assertEqual(result['status'], 'no_match')
        self.assertFalse(result['has_more'])
        self.assertIsNone(result['next_page_no'])
        self.upstream.side_effect = ConnectorError('no_match', 'No matching results.')
        self.assertEqual(restaurants.discovery('63131', 'zzzx')['status'], 'no_match')

    def test_disabled_and_unimplemented_platforms_make_no_reads(self):
        with patch.dict(os.environ, {'FOODSPARK_RESTAURANTS_ENABLED': 'false'}):
            self.assert_code('provider_disabled', restaurants.discovery, '63131', 'pizza')
        for platform in ['doordash', 'ubereats', 'uber_eats']:
            self.assert_code('unsupported_platform', restaurants.discovery, '63131', 'pizza', platform)
        self.upstream.assert_not_called()

    def test_synthetic_menu_preserves_price_address_and_link(self):
        discovery = self.discover()
        self.upstream.return_value = fixture('menu')
        result = restaurants.menu(discovery['discovery_id'], URL, 'yelp', ['Fixture Side Salad'], '15.00')
        product = result['items'][0]
        self.assertEqual(product['name'], 'Fixture Side Salad')
        self.assertEqual(product['price'], 6.40)
        self.assertEqual(product['price_type'], 'exact')
        self.assertEqual(product['currency'], 'USD')
        self.assertEqual(product['platform'], 'yelp')
        self.assertIn('/menu/fixture-menu-cafe/item/fixture-side-salad', product['menu_url'])
        self.assertIn('00000 (synthetic)', product['location']['address'])
        self.assertEqual(product['location']['requested_zip'], '63131')
        self.assertFalse(product['location']['verified'])
        self.assertIsNone(product['captured_at'])
        self.assertEqual(product['freshness'], 'unknown')
        self.assertIsNone(product['required_modifiers'])
        self.assertFalse(product['modifiers_complete'])
        assessment = result['budget_assessment']
        self.assertEqual(assessment['listed_subtotal'], 6.40)
        self.assertTrue(assessment['listed_subtotal_within_budget'])
        self.assertIsNone(assessment['order_within_budget'])
        self.assertIn('required_modifiers_unknown', assessment['reason_codes'])
        self.assertFalse(result['cart_transfer_supported'])
        self.assertFalse(result['cross_platform_matching_supported'])

    def test_menu_fetches_only_the_selected_discovered_restaurant(self):
        discovery = self.discover()
        self.upstream.return_value = fixture('menu')
        restaurants.menu(discovery['discovery_id'], URL, 'yelp')
        self.assertEqual(self.upstream.call_count, 2)
        path, params = self.upstream.call_args.args
        self.assertEqual(path, '/restaurant_menu')
        self.assertEqual(params['restaurant_url'], URL)
        self.assertEqual(params['endpoint_type'], 'dl295_restaurant_menu_by_restaurant_url')

    def test_arbitrary_or_unreturned_url_cannot_trigger_menu_fetch(self):
        discovery = self.discover()
        self.upstream.reset_mock()
        for url, code in [('http://www.yelp.com/biz/test', 'invalid_request'),
                          ('https://127.0.0.1/biz/test', 'invalid_request'),
                          ('https://www.yelp.com@evil.example/biz/test', 'invalid_request'),
                          ('https://www.yelp.com/biz/unreturned', 'discovery_mismatch')]:
            self.assert_code(code, restaurants.menu, discovery['discovery_id'], url, 'yelp')
        self.upstream.assert_not_called()

    def test_expired_discovery_does_not_trigger_menu_fetch(self):
        with patch.object(restaurants.time, 'monotonic', return_value=100):
            discovery = self.discover()
        self.upstream.reset_mock()
        with patch.object(restaurants.time, 'monotonic', return_value=401):
            self.assert_code('discovery_expired', restaurants.menu, discovery['discovery_id'], URL, 'yelp')
        self.upstream.assert_not_called()

    def test_discovery_storage_is_bounded(self):
        with patch.object(restaurants, 'DISCOVERY_LIMIT', 2):
            first = self.discover()
            self.discover()
            self.discover()
            self.assertEqual(len(restaurants._DISCOVERIES), 2)
            self.assertNotIn(first['discovery_id'], restaurants._DISCOVERIES)

    def test_discovery_has_no_menu_flag_prevents_lookup(self):
        discovery = self.discover(synthetic_discovery(False))
        self.upstream.reset_mock()
        self.assert_code('menu_unavailable', restaurants.menu, discovery['discovery_id'], URL, 'yelp')
        self.upstream.assert_not_called()

    def test_menu_identity_mismatch_is_not_returned_as_requested_restaurant(self):
        discovery = self.discover()
        payload = fixture('menu')
        payload['response']['results']['restaurant_url'] = 'https://www.yelp.com/biz/another'
        self.upstream.return_value = payload
        self.assert_code('identity_mismatch', restaurants.menu, discovery['discovery_id'], URL, 'yelp')

    def test_unrelated_restaurant_menu_link_is_not_passed_to_client(self):
        result = self.menu([item('Cheeseburger', '$10.00', product_url='https://www.yelp.com/menu/other-restaurant/item/cheeseburger')])
        self.assertIsNone(result['items'][0]['menu_url'])

    def test_starting_at_price_cannot_establish_budget_match(self):
        result = self.menu([item('Cheeseburger', 'Starting at $10.00'), item('Fries', '$3.25')],
                           terms=['cheeseburger', 'fries'], budget='15.00')
        assessment = result['budget_assessment']
        self.assertEqual(assessment['minimum_listed_subtotal'], 13.25)
        self.assertIsNone(assessment['listed_subtotal'])
        self.assertIsNone(assessment['listed_subtotal_within_budget'])
        self.assertIsNone(assessment['order_within_budget'])
        self.assertIn('starting_at_price', assessment['reason_codes'])
        self.assertEqual(result['items'][0]['listed_price_text'], 'Starting at $10.00')

    def test_required_paid_modifier_can_establish_order_is_over_budget(self):
        result = self.menu([item('Cheeseburger', '$10.00', required_modifiers=[{'name': 'Required side', 'price': '$2.00'}], modifiers_complete=True,
                                modifier_pricing_semantics='additive', modifier_pricing_semantics_verified=True),
                            item('Fries', '$4.00', required_modifiers=[], modifiers_complete=True)],
                           terms=['cheeseburger', 'fries'], budget='15.00')
        assessment = result['budget_assessment']
        self.assertEqual(assessment['listed_subtotal'], 14.0)
        self.assertEqual(assessment['known_required_modifier_total'], 2.0)
        self.assertEqual(assessment['minimum_subtotal_with_known_required_modifiers'], 16.0)
        self.assertEqual(assessment['status'], 'over_budget')
        self.assertFalse(assessment['order_within_budget'])
        self.assertEqual(result['items'][0]['required_modifiers'][0]['name'], 'Required side')

    def test_unknown_modifier_price_is_explicit(self):
        result = self.menu([item('Cheeseburger', '$10.00', required_modifiers=[{'name': 'Required size', 'price': None}], modifiers_complete=True)],
                           terms=['cheeseburger'], budget='15.00')
        self.assertIsNone(result['items'][0]['required_modifiers'][0]['price'])
        self.assertIn('required_modifiers_unknown', result['budget_assessment']['reason_codes'])
        self.assertIsNone(result['budget_assessment']['order_within_budget'])

    def test_money_uses_decimal_arithmetic(self):
        result = self.menu([item('Cheeseburger', '$0.10'), item('Fries', '$0.20')],
                           terms=['cheeseburger', 'fries'], budget='0.30', budget_operator='lte')
        self.assertEqual(result['budget_assessment']['listed_subtotal'], 0.3)
        self.assertTrue(result['budget_assessment']['listed_subtotal_within_budget'])

    def test_description_without_fries_does_not_supply_the_requested_side(self):
        result = self.menu([item('Cheeseburger', '$10.00', product_description='Without fries')],
                           terms=['cheeseburger', 'fries'], budget='15.00')
        assessment = result['budget_assessment']
        self.assertEqual(assessment['unmatched_terms'], ['fries'])
        self.assertIsNone(assessment['listed_subtotal'])
        self.assertIsNone(assessment['order_within_budget'])
        self.assertIn('requested_items_not_matched', assessment['reason_codes'])

    def test_exact_budget_boundary_distinguishes_under_from_at_most(self):
        for operator, expected in [('lt', False), ('lte', True)]:
            with self.subTest(operator=operator):
                result = self.menu([item('Cheeseburger', '$12.00'), item('Fries', '$3.00')],
                                   terms=['cheeseburger', 'fries'], budget='15.00', budget_operator=operator)
                assessment = result['budget_assessment']
                self.assertEqual(result['budget_operator'], operator)
                self.assertEqual(assessment['budget_operator'], operator)
                self.assertEqual(assessment['listed_subtotal'], 15.0)
                self.assertEqual(assessment['listed_subtotal_within_budget'], expected)
                self.assertIsNone(assessment['order_within_budget'])
                self.assertFalse(assessment['minimum_subtotal_established'])

    def test_under_is_the_default_budget_operator(self):
        result = self.menu([item('Cheeseburger', '$15.00')], terms=['cheeseburger'], budget='15.00')
        self.assertEqual(result['budget_operator'], 'lt')
        self.assertFalse(result['budget_assessment']['listed_subtotal_within_budget'])

    def test_unverified_flat_modifier_prices_are_preserved_but_not_summed(self):
        result = self.menu([item('Cheeseburger', '$10.00', required_modifiers=[
            {'name': 'Fries side', 'price': '$5.00'}, {'name': 'Salad side', 'price': '$6.00'}], modifiers_complete=True)],
            terms=['cheeseburger'], budget='15.00')
        assessment = result['budget_assessment']
        self.assertEqual([m['price'] for m in result['items'][0]['required_modifiers']], [5.0, 6.0])
        self.assertIsNone(assessment['known_required_modifier_total'])
        self.assertIsNone(assessment['minimum_subtotal_with_known_required_modifiers'])
        self.assertIsNone(assessment['order_within_budget'])
        self.assertIn('required_modifier_selection_unverified', assessment['reason_codes'])

    def test_one_of_modifier_choices_are_not_treated_as_additive(self):
        result = self.menu([item('Cheeseburger', '$10.00', required_modifiers=[
            {'name': 'Fries side', 'price': '$5.00'}, {'name': 'Salad side', 'price': '$6.00'}], modifiers_complete=True,
            modifier_pricing_semantics='one_of', modifier_pricing_semantics_verified=True)],
            terms=['cheeseburger'], budget='15.00')
        assessment = result['budget_assessment']
        self.assertEqual(result['items'][0]['modifier_pricing_semantics'], 'one_of')
        self.assertIsNone(assessment['known_required_modifier_total'])
        self.assertIsNone(assessment['order_within_budget'])
        self.assertEqual(assessment['status'], 'uncertain')

    def test_required_fries_and_separate_fries_are_a_component_overlap(self):
        result = self.menu([
            item('Cheeseburger', '$10.00', required_modifiers=[{'name': 'Fries', 'price': '$4.00'}],
                 modifiers_complete=True, modifier_pricing_semantics='additive', modifier_pricing_semantics_verified=True),
            item('Fries', '$3.00', required_modifiers=[], modifiers_complete=True)],
            terms=['cheeseburger', 'fries'], budget='15.00')
        assessment = result['budget_assessment']
        self.assertEqual(assessment['listed_subtotal'], 13.0)
        self.assertEqual(assessment['known_required_modifier_total'], 4.0)
        self.assertIsNone(assessment['minimum_subtotal_with_known_required_modifiers'])
        self.assertIsNone(assessment['order_within_budget'])
        self.assertIn('required_modifier_component_overlap', assessment['reason_codes'])
        self.assertEqual(assessment['modifier_component_overlaps'][0]['requested_term'], 'fries')

    def test_fully_evidenced_minimum_at_exclusive_boundary_is_outside_budget(self):
        result = self.menu([item('Cheeseburger', '$15.00', required_modifiers=[], modifiers_complete=True)],
                           terms=['cheeseburger'], budget='15.00')
        assessment = result['budget_assessment']
        self.assertTrue(assessment['minimum_subtotal_established'])
        self.assertEqual(assessment['minimum_subtotal_with_known_required_modifiers'], 15.0)
        self.assertFalse(assessment['order_within_budget'])
        self.assertEqual(assessment['status'], 'over_budget')

    def test_invalid_budget_operator_does_not_issue_menu_request(self):
        discovery = self.discover()
        self.upstream.reset_mock()
        self.assert_code('invalid_request', restaurants.menu, discovery['discovery_id'], URL, 'yelp',
                         budget='15.00', budget_operator='eq')
        self.upstream.assert_not_called()

    def test_missing_price_is_unknown_and_not_zero(self):
        result = self.menu([item('Cheeseburger', None), item('Fries', '$3.25')],
                           terms=['cheeseburger', 'fries'], budget='15.00')
        self.assertIsNone(result['items'][0]['price'])
        self.assertEqual(result['items'][0]['price_type'], 'unknown')
        self.assertIsNone(result['budget_assessment']['minimum_listed_subtotal'])
        self.assertIsNone(result['budget_assessment']['order_within_budget'])

    def test_unmatched_requested_item_does_not_establish_full_order_price(self):
        result = self.menu([item('Cheeseburger', '$10.00')], terms=['cheeseburger', 'fries'], budget='15.00')
        self.assertEqual(result['budget_assessment']['unmatched_terms'], ['fries'])
        self.assertIsNone(result['budget_assessment']['minimum_listed_subtotal'])
        self.assertIsNone(result['budget_assessment']['order_within_budget'])

    def test_menu_no_match_filters_unrelated_items(self):
        result = self.menu([item('Mushroom Pizza', '$9.99')], terms=['cheeseburger'], budget='15.00')
        self.assertEqual(result['status'], 'no_match')
        self.assertEqual(result['items'], [])
        self.assertEqual(result['budget_assessment']['unmatched_terms'], ['cheeseburger'])

    def test_transport_errors_remain_distinct(self):
        for code in ['provider_auth', 'provider_timeout', 'no_coverage', 'provider_error', 'budget_exhausted']:
            with self.subTest(code=code):
                self.upstream.side_effect = ConnectorError(code, 'Sanitized upstream failure.')
                self.assert_code(code, restaurants.discovery, '63131', 'pizza')

    def test_invalid_budget_is_rejected_before_menu_request(self):
        discovery = self.discover()
        self.upstream.reset_mock()
        for budget in [True, -1, 'NaN', 'Infinity', '1.001', 0, 10001]:
            with self.subTest(budget=budget):
                self.assert_code('invalid_request', restaurants.menu, discovery['discovery_id'], URL, 'yelp', budget=budget)
        self.upstream.assert_not_called()

    def test_unrecognized_and_nonfinite_price_texts_are_not_usable_prices(self):
        for price in ['$9.99–$15.00', '$10.00 /lb', 'NaN', '$' + '9' * 100]:
            result = self.menu([item('Cheeseburger', price)], terms=['cheeseburger'], budget='15.00')
            self.assertIsNone(result['items'][0]['price'])
            self.assertIsNone(result['budget_assessment']['order_within_budget'])

    def test_query_url_and_vendor_log_do_not_leak_into_output(self):
        payload = synthetic_discovery()
        payload['request_log']['request_url'] = 'https://vendor.invalid?api_key=fixture-secret'
        payload['response']['results'][0]['restaurant_url'] += '?api_key=fixture-secret'
        result = self.discover(payload)
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertEqual(result['items'][0]['restaurant_url'], URL)


if __name__ == '__main__':
    unittest.main()

"""Synthetic vendor contract tests: all urllib traffic is mocked."""
import io
import json
import os
import unittest
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from unittest.mock import Mock, patch

import provider_test_adapters as adapters


class Response(io.BytesIO):
    def __init__(self, payload, status=200, raw=False, on_exit=None):
        super().__init__(payload if raw else json.dumps(payload).encode())
        self.status = status
        self.on_exit = on_exit

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.on_exit:
            self.on_exit()
        self.close()


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.nf_key = 'synthetic/northfork+secret&value'
        self.xb_key = 'synthetic/foodspark+secret&value'
        self.environment = patch.dict(os.environ, {
            'NORTHFORK_API_KEY': self.nf_key,
            'FOODSPARK_API_KEY': self.xb_key,
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.build = patch.object(adapters, 'build_opener').start()
        self.addCleanup(patch.stopall)
        self.opener = self.build.return_value
        self.events = []
        self.reserve = Mock(side_effect=lambda: self.events.append('reserve'))
        self.request = {
            'zip': '63129', 'retailer': 'schnucks', 'queries': ['1 gallon whole milk'],
            'location': {'postal_code': '63129', 'country_code': 'US', 'address': {
                'line1': '123 Example Road', 'city': 'Saint Louis', 'region': 'MO',
            }}, 'options': {},
        }
        self.store = {'provider': 'instacart', 'identifier': '1727',
                      'name': 'Schnucks', 'retailer_key': 'schnucks', 'zip': '63129'}

    def replies(self, *responses):
        iterator = iter(responses)

        def opened(request, timeout):
            self.assertEqual(self.events[-1], 'reserve')
            self.events.append('open')
            self.assertTrue(0 < timeout <= adapters.REQUEST_TIMEOUT_SECONDS)
            response = next(iterator)
            if isinstance(response, Exception):
                raise response
            return response if isinstance(response, Response) else Response(response)

        self.opener.open.side_effect = opened

    def nf_stores(self, stores=None):
        return {'meta': {'code': 200}, 'data': [self.store] if stores is None else stores}

    def nf_products(self, store=None):
        return {'meta': {'code': 200}, 'data': {
            'store': {'provider': 'instacart', 'identifier': '1727'} if store is None else store,
            'ingredients': [{'name': '1 gallon whole milk', 'products': [{
                'id': 2173479, 'external_id': '20653045', 'price': 399,
                'currency_code': 'USD', 'quantity': 1, 'unit_of_order': 'pce',
                'name': 'Schnucks Whole Milk', 'size': 128, 'unit_of_size': 'fl oz',
                'in_stock': None, 'pdp_url': None,
            }]}],
        }}

    def xb_products(self):
        return {
            'request_log': {'status': 200, 'request_url': 'https://vendor.invalid/?api_key=' + self.xb_key},
            'response': {
                'store_details': {
                    'Store Name': 'Schnucks', 'Store Slug': 'schnucks',
                    'Store Location ID': '1727', 'Shop ID': '123', 'Zone ID': '456',
                    'Delivery Address': '123 Example Road', 'Delivery Zip Code': '63129',
                },
                'results': [{'Product Name': 'Milk', 'Price': '$3.99',
                             'Unit Size': '1 gallon', 'Availability': None}],
                'message': 'Success',
            },
        }

    def call(self, provider, request=None):
        return adapters.fetch_provider(provider, request or self.request, self.reserve)

    def assert_safe_error(self, error):
        text = json.dumps({'code': error.code, 'message': str(error), 'details': error.details})
        for key in (self.nf_key, self.xb_key, quote(self.nf_key, safe=''), quote(self.xb_key, safe='')):
            self.assertNotIn(key, text)

    def test_northfork_runtime_discovery_and_resolve_preserve_cents_and_ids(self):
        extra = dict(self.store, identifier='160507', name='Schnucks Now', retailer_key='schnucks-now')
        self.replies(self.nf_stores([extra, self.store]), self.nf_products())
        result = self.call('northfork')
        calls = self.opener.open.call_args_list
        first = calls[0].args[0]
        self.assertEqual(urlsplit(first.full_url).netloc, 'api.northfork.se')
        self.assertEqual(parse_qs(urlsplit(first.full_url).query), {'zip': ['63129'], 'providers': ['instacart']})
        self.assertEqual(first.get_header('X-custom-auth'), self.nf_key)
        second = calls[1].args[0]
        self.assertEqual(urlsplit(second.full_url).path, '/api/smart-cart/ingredients/resolve')
        self.assertEqual(second.method, 'POST')
        body = json.loads(second.data)
        self.assertEqual(body['store'], {k: self.store[k] for k in ('provider', 'identifier', 'name')})
        self.assertEqual(body['options'], {'in_stock': False, 'substitute_count': 1})
        self.assertEqual(body['ingredients'], [{'name': '1 gallon whole milk'}])
        product = result['payload']['data']['ingredients'][0]['products'][0]
        self.assertEqual((product['id'], product['external_id'], product['price']), (2173479, '20653045', 399))
        self.assertIs(type(product['price']), int)
        self.assertEqual(result['store']['requested_zip'], '63129')
        self.assertEqual(result['requests'], 2)
        self.assertEqual(self.events, ['reserve', 'open', 'reserve', 'open'])

    def test_northfork_nearby_branch_zip_and_exact_name_are_supported(self):
        store = {'provider': 'target', 'identifier': 1509, 'name': 'South County', 'zip': '63128'}
        self.request.update(retailer=' south COUNTY ', options={'marketplace': 'target', 'store_id': '1509', 'in_stock': True})
        self.replies(self.nf_stores([store]), self.nf_products({'provider': 'target', 'identifier': '1509'}))
        result = self.call('northfork')
        self.assertEqual(result['store']['identifier'], 1509)
        self.assertEqual(result['store']['zip'], '63128')
        self.assertEqual(result['store']['requested_zip'], '63129')
        body = json.loads(self.opener.open.call_args_list[1].args[0].data)
        self.assertTrue(body['options']['in_stock'])
        self.assertEqual(parse_qs(urlsplit(self.opener.open.call_args_list[0].args[0].full_url).query)['providers'], ['target'])

    def test_ambiguous_store_requires_returned_store_id(self):
        second = dict(self.store, identifier='second', api_key=self.nf_key)
        self.replies(self.nf_stores([self.store, second]))
        with self.assertRaises(adapters.ProviderReadError) as caught:
            self.call('northfork')
        error = caught.exception
        self.assertEqual((error.code, error.http_status), ('ambiguous_store', 409))
        self.assertEqual(error.details['candidate_count'], 2)
        self.assertEqual(len(error.details['candidates']), 2)
        self.assert_safe_error(error)
        self.assertEqual(self.reserve.call_count, 1)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_store_id_only_disambiguates_current_matching_candidates(self):
        second = dict(self.store, identifier='second')
        self.request['options'] = {'store_id': 'second'}
        self.replies(self.nf_stores([self.store, second]), self.nf_products({'provider': 'instacart', 'identifier': 'second'}))
        result = self.call('northfork')
        self.assertEqual(result['store']['identifier'], 'second')
        self.request['options'] = {'store_id': 'not-returned'}
        self.replies(self.nf_stores())
        with self.assertRaises(adapters.ProviderReadError) as caught:
            self.call('northfork')
        self.assertEqual(caught.exception.code, 'store_not_found')
        self.assertEqual(self.opener.open.call_count, 3)

    def test_store_id_cannot_select_other_retailer_or_marketplace(self):
        for other in (dict(self.store, name='Other', retailer_key='other'), dict(self.store, provider='walmart')):
            with self.subTest(store=other):
                self.request['options'] = {'store_id': '1727'}
                self.replies(self.nf_stores([other]))
                before = self.opener.open.call_count
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call('northfork')
                self.assertEqual(caught.exception.code, 'store_not_found')
                self.assertEqual(self.opener.open.call_count, before + 1)

    def test_northfork_resolver_must_confirm_selected_store(self):
        for changed in ({'provider': 'walmart', 'identifier': '1727'}, {'provider': 'instacart', 'identifier': 'other'}):
            with self.subTest(store=changed):
                self.replies(self.nf_stores(), self.nf_products(changed))
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call('northfork')
                self.assertEqual(caught.exception.code, 'location_mismatch')

    def test_northfork_meta_error_in_http_200_stops_before_resolver(self):
        self.replies({'meta': {'code': 401, 'message': self.nf_key}, 'data': [self.store]})
        with self.assertRaises(adapters.ProviderReadError) as caught:
            self.call('northfork')
        self.assertEqual(caught.exception.code, 'provider_authentication_failed')
        self.assertEqual(self.opener.open.call_count, 1)
        self.assert_safe_error(caught.exception)

    def test_xbyte_uses_documented_endpoint_per_query_and_preserves_prices(self):
        self.request['queries'] = ['milk', 'eggs']
        first, second = self.xb_products(), self.xb_products()
        second['response']['results'][0]['Price'] = '$22.00 /pkg (est.)'
        second['response']['results'][0]['Product ID'] = 'vendor-123'
        self.replies(first, second)
        result = self.call('xbyte_foodspark')
        self.assertEqual(result['requests'], 2)
        self.assertEqual([r['query'] for r in result['payload']['queries']], ['milk', 'eggs'])
        for i, call in enumerate(self.opener.open.call_args_list):
            request = call.args[0]
            self.assertEqual(urlsplit(request.full_url).netloc, 'foodsparkfastapiframework.xbyte.io')
            self.assertEqual(urlsplit(request.full_url).path, '/search')
            self.assertEqual(request.method, 'GET')
            params = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(params, {
                'country': ['usa'], 'platform': ['web'], 'domain': ['instacart'],
                'endpoint_type': ['dl295_search_by_addr_brand_slug_and_keyword'],
                'brand_slug': ['schnucks'], 'search_term': [self.request['queries'][i]],
                'zipcode': ['63129'], 'address': ['123 Example Road, Saint Louis, MO 63129'],
                'api_key': [self.xb_key],
            })
            self.assertIsNone(request.get_header('X-custom-auth'))
        product = result['payload']['queries'][1]['response']['results'][0]
        self.assertEqual(product['Price'], '$22.00 /pkg (est.)')
        self.assertEqual(product['Product ID'], 'vendor-123')
        self.assertNotIn('request_log', json.dumps(result))
        self.assertEqual(self.events, ['reserve', 'open', 'reserve', 'open'])

    def test_xbyte_missing_address_or_retailer_never_reserves_or_falls_back(self):
        cases = [dict(self.request, location=None), dict(self.request, retailer=''),
                 dict(self.request, location={'address': {'line1': '123 Example Road'}})]
        for request in cases:
            with self.subTest(request=request):
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call('xbyte_foodspark', request)
                self.assertIn(caught.exception.code, ('location_required', 'retailer_required'))
                self.assertEqual(caught.exception.http_status, 422)
        self.reserve.assert_not_called()
        self.opener.open.assert_not_called()

    def test_xbyte_rejects_invalid_retailer_slug_before_network(self):
        for retailer in ('Schnucks', 'south county', '../schnucks', 'a' * 65):
            with self.subTest(retailer=retailer):
                request = dict(self.request, retailer=retailer)
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call('xbyte_foodspark', request)
                self.assertEqual((caught.exception.code, caught.exception.http_status), ('invalid_retailer', 422))
        self.reserve.assert_not_called()
        self.opener.open.assert_not_called()

    def test_xbyte_rejects_zip_retailer_and_address_mismatches(self):
        for field, value in [('Delivery Zip Code', '63131'), ('Store Slug', 'other'), ('Delivery Address', '999 Other Road')]:
            with self.subTest(field=field):
                payload = self.xb_products()
                payload['response']['store_details'][field] = value
                self.replies(payload)
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call('xbyte_foodspark')
                self.assertEqual(caught.exception.code, 'location_mismatch')

    def test_xbyte_unit_and_full_address_must_match(self):
        self.request['location']['address']['line2'] = 'Unit 2'
        self.replies(self.xb_products())
        with self.assertRaises(adapters.ProviderReadError) as caught:
            self.call('xbyte_foodspark')
        self.assertEqual(caught.exception.code, 'location_mismatch')
        payload = self.xb_products()
        payload['response']['store_details']['Delivery Address'] = '123 Example Road, Unit 2, Saint Louis, MO 63129'
        self.replies(payload)
        self.assertEqual(self.call('xbyte_foodspark')['requests'], 1)

    def test_xbyte_rejects_mixed_store_context_between_queries(self):
        self.request['queries'] = ['milk', 'eggs']
        second = self.xb_products()
        second['response']['store_details']['Shop ID'] = 'other-shop'
        self.replies(self.xb_products(), second)
        with self.assertRaises(adapters.ProviderReadError) as caught:
            self.call('xbyte_foodspark')
        self.assertEqual(caught.exception.code, 'location_mismatch')
        self.assertEqual(self.reserve.call_count, 2)

    def test_actual_xbyte_http_200_nested_error_shape_is_not_empty_success(self):
        self.replies({'request_log': {'status': 200}, 'response': {
            'store_details': {}, 'results': [],
            'errors': [{'error_code': 400, 'error_message': 'Store not found ' + self.xb_key}],
            'message': 'Store not found please try another location.',
        }})
        with self.assertRaises(adapters.ProviderReadError) as caught:
            self.call('xbyte_foodspark')
        self.assertEqual(caught.exception.code, 'provider_error')
        self.assertEqual(caught.exception.details['upstream_http_status'], 400)
        self.assert_safe_error(caught.exception)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_nested_status_and_failure_message_cannot_look_successful(self):
        for mutation in ({'status': 500}, {'message': 'No More Results'}, {'success': False}):
            with self.subTest(mutation=mutation):
                payload = self.xb_products()
                payload['response'].update(mutation)
                self.replies(payload)
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call('xbyte_foodspark')
                self.assertEqual(caught.exception.code, 'provider_error')

    def test_raw_payload_redacts_credentials_recursively_without_changing_cents(self):
        payload = self.nf_products()
        product = payload['data']['ingredients'][0]['products'][0]
        product.update({'api_key': self.nf_key, 'debug': self.nf_key,
                        'link': 'https://vendor.invalid/?api_key=' + quote(self.nf_key, safe=''),
                        'authorization': 'Bearer ' + self.nf_key,
                        'request_log': {'request_url': self.nf_key}})
        payload['request_log'] = {'request_url': self.nf_key}
        self.replies(self.nf_stores(), payload)
        result = self.call('northfork')
        text = json.dumps(result)
        self.assertNotIn(self.nf_key, text)
        self.assertNotIn(quote(self.nf_key, safe=''), text)
        self.assertNotIn('request_log', text)
        self.assertNotIn('authorization', text)
        self.assertEqual(result['payload']['data']['ingredients'][0]['products'][0]['price'], 399)

    def test_missing_credentials_never_make_requests(self):
        with patch.dict(os.environ, {}, clear=True):
            for provider in ('xbyte_foodspark', 'northfork'):
                with self.subTest(provider=provider):
                    with self.assertRaises(adapters.ProviderReadError) as caught:
                        self.call(provider)
                    self.assertEqual(caught.exception.code, 'not_configured')
        self.reserve.assert_not_called()
        self.opener.open.assert_not_called()

    def test_actowiz_and_unknown_provider_never_contact_any_host(self):
        for provider in ('actowiz', 'http://unsafe.invalid'):
            with self.subTest(provider=provider):
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call(provider)
                self.assertEqual(caught.exception.code, 'unsupported_operation')
                self.assertEqual(caught.exception.http_status, 422)
        self.reserve.assert_not_called()
        self.build.assert_not_called()

    def test_reservation_rejection_propagates_without_open(self):
        rejected = RuntimeError('controller denied the attempt')
        self.reserve.side_effect = rejected
        with self.assertRaises(RuntimeError) as caught:
            self.call('northfork')
        self.assertIs(caught.exception, rejected)
        self.opener.open.assert_not_called()

    def test_redirect_is_never_followed_and_http_error_text_is_not_exposed(self):
        error = HTTPError('https://vendor.invalid/?api_key=' + self.xb_key, 302,
                          self.xb_key, {'Location': 'http://unsafe.invalid'}, None)
        self.replies(error)
        with self.assertRaises(adapters.ProviderReadError) as caught:
            self.call('xbyte_foodspark')
        self.assertEqual(caught.exception.code, 'provider_redirect')
        self.assert_safe_error(caught.exception)
        self.assertEqual(self.opener.open.call_count, 1)
        handler = self.build.call_args.args[0]
        self.assertIsInstance(handler, adapters.NoRedirect)
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'http://unsafe.invalid'))

    def test_timeout_and_network_error_do_not_retry_or_leak_provider_text(self):
        for error, code in ((TimeoutError(self.xb_key), 'provider_timeout'),
                            (URLError(self.xb_key), 'provider_unavailable')):
            with self.subTest(code=code):
                self.replies(error)
                before = self.opener.open.call_count
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call('xbyte_foodspark')
                self.assertEqual(caught.exception.code, code)
                self.assert_safe_error(caught.exception)
                self.assertEqual(self.opener.open.call_count, before + 1)

    def test_response_size_and_structure_are_bounded(self):
        with patch.object(adapters, 'MAX_RESPONSE_BYTES', 100):
            self.replies(Response(b'x' * 101, raw=True))
            with self.assertRaises(adapters.ProviderReadError) as caught:
                self.call('northfork')
            self.assertEqual(caught.exception.code, 'response_too_large')
        for payload in ([], {'data': 'not a list'}):
            self.replies(payload)
            with self.assertRaises(adapters.ProviderReadError) as caught:
                self.call('northfork')
            self.assertEqual(caught.exception.code, 'invalid_response')

    def test_truncated_http_and_non_json_errors_never_expose_response_text(self):
        for response in (IncompleteRead(self.xb_key.encode()), Response(self.xb_key.encode(), raw=True)):
            with self.subTest(response_type=type(response).__name__):
                self.replies(response)
                with self.assertRaises(adapters.ProviderReadError) as caught:
                    self.call('xbyte_foodspark')
                self.assertEqual(caught.exception.code, 'invalid_response')
                self.assert_safe_error(caught.exception)

    def test_remaining_overall_budget_caps_next_network_timeout(self):
        now = [0.0]
        self.replies(Response(self.nf_stores(), on_exit=lambda: now.__setitem__(0, 45.0)), self.nf_products())
        with patch.object(adapters.time, 'monotonic', side_effect=lambda: now[0]):
            result = self.call('northfork')
        self.assertEqual(result['requests'], 2)
        self.assertEqual([call.kwargs['timeout'] for call in self.opener.open.call_args_list], [30.0, 15.0])

    def test_exhausted_total_budget_prevents_another_reservation(self):
        now = [0.0]
        self.replies(Response(self.nf_stores(), on_exit=lambda: now.__setitem__(0, 61.0)))
        with patch.object(adapters.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(adapters.ProviderReadError) as caught:
                self.call('northfork')
        self.assertEqual(caught.exception.code, 'provider_timeout')
        self.assertEqual(self.reserve.call_count, 1)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_existing_request_context_deadline_also_caps_transport(self):
        self.replies(self.nf_stores(), self.nf_products())
        with patch.object(adapters.deadline, 'remaining', return_value=7.0):
            self.call('northfork')
        self.assertTrue(all(0 < call.kwargs['timeout'] <= 7.0 for call in self.opener.open.call_args_list))
        self.replies(self.nf_stores())
        before = self.reserve.call_count
        with patch.object(adapters.deadline, 'remaining', return_value=-1.0):
            with self.assertRaises(adapters.ProviderReadError) as caught:
                self.call('northfork')
        self.assertEqual(caught.exception.code, 'provider_timeout')
        self.assertEqual(self.reserve.call_count, before)


if __name__ == '__main__':
    unittest.main()

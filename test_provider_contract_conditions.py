"""Request condition parity and complete canonical-item contract, without vendor calls."""
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from provider_tests import TestRequestError, fixture, validate_request
from provider_test_normalize import normalize


SPEC = json.loads((Path(__file__).parent / 'openapi.json').read_text())
SINGLE = {'provider': 'northfork', 'zip': '63129', 'queries': ['milk'],
          'retailer': 'schnucks', 'mode': 'fixture'}
COMPARE = {**{k: v for k, v in SINGLE.items() if k != 'provider'},
           'providers': ['northfork', 'xbyte_foodspark']}
ADDRESS = {'line1': '123 Synthetic Street', 'city': 'Example City', 'region': 'MO'}


class ProviderContractConditions(unittest.TestCase):
    def schema(self, name):
        return Draft202012Validator({'$ref': '#/components/schemas/' + name,
                                     'components': SPEC['components']})

    def assertParity(self, body, valid, comparison=False):
        name = 'ProviderTestCompareRequest' if comparison else 'ProviderTestRequest'
        errors = list(self.schema(name).iter_errors(body))
        self.assertEqual(not errors, valid, [e.message for e in errors])
        if valid:
            validate_request(body, comparison)
        else:
            with self.assertRaises(TestRequestError):
                validate_request(body, comparison)

    def test_selected_supplier_options_are_valid(self):
        for provider in ('northfork', 'xbyte_foodspark', 'actowiz'):
            with self.subTest(provider=provider):
                self.assertParity({**SINGLE, 'provider': provider, 'options': {}}, True)
        self.assertParity({**SINGLE, 'options': {'marketplace': 'instacart',
                                               'store_id': '1727', 'in_stock': False}}, True)
        self.assertParity({**COMPARE, 'options': {'northfork': {'store_id': '1727'},
                                                'xbyte_foodspark': {}}}, True, True)
        self.assertParity({**COMPARE, 'providers': ['actowiz'],
                           'options': {'actowiz': {}}}, True, True)

    def test_northfork_options_cannot_be_sent_to_other_suppliers(self):
        for provider in ('xbyte_foodspark', 'actowiz'):
            for options in ({'marketplace': 'instacart'}, {'store_id': '1727'}, {'in_stock': False}):
                with self.subTest(provider=provider, options=options):
                    self.assertParity({**SINGLE, 'provider': provider, 'options': options}, False)

    def test_comparison_options_must_belong_to_selected_suppliers(self):
        for selected, unselected in [('northfork', 'actowiz'), ('xbyte_foodspark', 'northfork'),
                                     ('actowiz', 'xbyte_foodspark')]:
            with self.subTest(selected=selected, unselected=unselected):
                self.assertParity({**COMPARE, 'providers': [selected],
                                   'options': {unselected: {}}}, False, True)

    def test_unknown_options_and_invalid_option_types_are_rejected(self):
        for options in ({'api_key': 'synthetic'}, {'marketplace': 'unknown'},
                        {'store_id': 1727}, {'in_stock': 'false'}, {'store_id': 'bad id'}):
            with self.subTest(options=options):
                self.assertParity({**SINGLE, 'options': options}, False)
                self.assertParity({**COMPARE, 'options': {'northfork': options}}, False, True)
        self.assertParity({**COMPARE, 'options': {'unknown': {}}}, False, True)
        self.assertParity({**COMPARE, 'options': {'xbyte_foodspark': {'in_stock': False}}}, False, True)

    def test_blank_queries_and_retailer_controls_are_rejected(self):
        for body in ({**SINGLE, 'queries': ['']}, {**SINGLE, 'queries': [' \t\n']},
                     {**SINGLE, 'retailer': '   '}, {**SINGLE, 'retailer': 'schnucks\n'},
                     {**SINGLE, 'retailer': '\x01'}, {**SINGLE, 'retailer': 'a\x00b'}):
            with self.subTest(body=body):
                self.assertParity(body, False)
                compare = {**body, 'providers': ['northfork']}
                compare.pop('provider')
                self.assertParity(compare, False, True)

    def test_blank_address_parts_are_rejected(self):
        for field in ('line1', 'city', 'region'):
            for blank in ('', ' \t\n'):
                with self.subTest(field=field, blank=blank):
                    body = {**SINGLE, 'location': {'postal_code': '63129',
                            'country_code': 'US', 'address': {**ADDRESS, field: blank}}}
                    self.assertParity(body, False)
        self.assertParity({**SINGLE, 'location': {'postal_code': '63129',
                           'address': {**ADDRESS, 'line2': ''}}}, True)
        location_only = {**SINGLE, 'location': {'postal_code': '63129', 'address': ADDRESS}}
        location_only.pop('zip')
        self.assertParity(location_only, True)

    def test_every_documented_canonical_item_key_is_emitted(self):
        schema = SPEC['components']['schemas']['ProviderTestItem']
        self.assertEqual(set(schema['required']), set(schema['properties']))
        for provider in ('northfork', 'xbyte_foodspark', 'actowiz'):
            response = fixture(provider)
            items = normalize(provider, response['payload'], SINGLE, response['store'],
                              '2026-11-19T23:00:00Z', mode='fixture')
            self.assertTrue(items)
            for item in items:
                with self.subTest(provider=provider):
                    self.assertEqual(set(item), set(schema['required']))
                    self.schema('ProviderTestItem').validate(item)
                    # Explicit nulls are contract data. Omission must not silently pass.
                    without_price = copy.deepcopy(item)
                    without_price.pop('price_decimal')
                    self.assertTrue(list(self.schema('ProviderTestItem').iter_errors(without_price)))


if __name__ == '__main__':
    unittest.main()

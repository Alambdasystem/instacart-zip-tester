"""Synthetic fixtures: no merchant products, customer locations or live calls."""

import json
import unittest

from provider_test_normalize import normalize


RETRIEVED = "2000-01-03T00:00:00Z"
REQUEST = {"zip": "00000", "queries": ["fixture product"], "retailer": "fixture-retailer"}


def northfork(product=None, **updates):
    row = {
        "id": 900001, "external_id": "fixture-product-a", "name": "Fixture Product A",
        "brand": "na", "price": 399, "currency_code": "USD", "size": 12,
        "unit_of_size": "oz", "quantity": 2, "unit_of_order": "pce", "in_stock": True,
        "pdp_url": None, "image_url": "https://images.example.test/fixture-a.jpg",
        "unit_price": None,
    }
    if product is not None:
        row = dict(product)
    row.update(updates)
    return {"data": {"store": {"identifier": "90001", "provider": "instacart"},
                     "ingredients": [{"name": "fixture product", "products": [row]}]}}


def foodspark(**updates):
    row = {"Product Name": "Fixture Product A", "Price": "$3.99", "Unit Size": "12 oz",
           "Availability": None}
    row.update(updates)
    return {"queries": [{"query": "fixture product", "response": {
        "store_details": {"Store Slug": "fixture-retailer", "Store Location ID": "90001",
                          "Delivery Zip Code": "00000", "Delivery Address": "PRIVATE ADDRESS OMITTED"},
        "results": [row],
    }}]}


def actowiz(**updates):
    row = {"item_name": "Fixture Product A", "product_id": "fixture-product-a",
           "upc": "000000000001", "source": "walmart", "price": "3.99", "currency": "USD",
           "size": "12 oz", "availability": "OUT_OF_STOCK", "store_number": "90001",
           "store_zip": "00001", "timestamp": "2000-01-02T03:04:05Z"}
    row.update(updates)
    return row


class ProviderTestNormalizationTests(unittest.TestCase):
    def one(self, provider, payload, store=None, request=None, mode="fixture"):
        return normalize(provider, payload, request or REQUEST, store or {}, RETRIEVED, mode)[0]

    def test_provider_specific_money_units(self):
        cases = [("northfork", northfork(), "minor_currency_units"),
                 ("xbyte_foodspark", foodspark(), "formatted_major_currency"),
                 ("actowiz", actowiz(), "major_currency_units")]
        for provider, payload, units in cases:
            with self.subTest(provider=provider):
                item = self.one(provider, payload)
                self.assertEqual(item["price"], 3.99)
                self.assertEqual(item["price_decimal"], "3.99")
                self.assertEqual(item["price_minor"], 399)
                self.assertEqual(item["currency"], "USD")
                self.assertEqual(item["raw_price_unit"], units)

    def test_missing_primary_price_never_uses_promotion_or_zero(self):
        item = self.one("actowiz", actowiz(price=None, regular_price="4.50", promotional_price="2.99"))
        for field in ("price", "price_decimal", "price_minor"):
            self.assertIsNone(item[field])
        self.assertEqual(item["price_kind"], "unknown")
        self.assertIn("price_missing", item["quality_flags"])
        self.assertEqual(item["raw_field_types"]["price"], "null")
        self.assertTrue(item["list_display_supported"])

    def test_false_and_missing_stock_are_distinct_and_visible(self):
        for value, expected in [(False, "out_of_stock"), (None, "unknown"),
                                ("false", "out_of_stock"), ("IN_STOCK", "in_stock"),
                                ("limited", "unknown")]:
            with self.subTest(value=value):
                item = self.one("actowiz", actowiz(availability=value))
                self.assertEqual(item["availability"], expected)
                self.assertEqual(item["availability_raw"], value)
                self.assertEqual(type(item["availability_raw"]), type(value))
                self.assertEqual(item["price"], 3.99)
                self.assertTrue(item["list_display_supported"])

    def test_boolean_prices_are_not_numbers(self):
        for provider, payload in [("northfork", northfork(price=True)),
                                  ("actowiz", actowiz(price=False)),
                                  ("xbyte_foodspark", foodspark(Price=True))]:
            item = self.one(provider, payload)
            self.assertIsNone(item["price"])
            self.assertIn("price_invalid_type", item["quality_flags"])

    def test_actowiz_actual_item_name_and_upc_leading_zero(self):
        item = self.one("actowiz", actowiz())
        self.assertEqual(item["name"], "Fixture Product A")
        self.assertEqual(item["upc"], "000000000001")
        self.assertEqual(item["provider_product_id"], "fixture-product-a")
        self.assertEqual(item["raw_field_types"]["item_name"], "string")

    def test_numeric_upc_is_not_padded(self):
        item = self.one("actowiz", actowiz(upc=1001))
        self.assertEqual(item["upc"], "1001")
        self.assertIn("numeric_upc_may_have_lost_leading_zeros", item["quality_flags"])

    def test_no_invented_urls_and_unsafe_urls_removed(self):
        item = self.one("actowiz", actowiz())
        self.assertIsNone(item["url"])
        self.assertIsNone(item["image_url"])
        for url in ["http://example.test/product", "https://user:pass@example.test/p",
                    "https://example.test/p?api_key=fixture-secret", "https://127.0.0.1/p",
                    "https://localhost/p", "javascript:alert(1)"]:
            item = self.one("actowiz", actowiz(product_url=url, image=url))
            self.assertIsNone(item["url"])
            self.assertIsNone(item["image_url"])
            self.assertNotIn("fixture-secret", json.dumps(item))

    def test_valid_https_urls_preserved_without_fetching(self):
        url = "https://retailer.example.test/fixture-product-a"
        image = "https://images.example.test/fixture-product-a.jpg"
        item = self.one("actowiz", actowiz(product_url=url, images=[image]))
        self.assertEqual(item["url"], url)
        self.assertEqual(item["image_url"], image)

    def test_capture_time_is_not_retrieval_time(self):
        for raw in [None, "2000-01-02 03:04:05", "01/02/00", 946684800]:
            item = self.one("actowiz", actowiz(timestamp=raw))
            self.assertIsNone(item["captured_at"])
            self.assertEqual(item["retrieved_at"], RETRIEVED)
        item = self.one("actowiz", actowiz(timestamp="2000-01-02T04:04:05+01:00"))
        self.assertEqual(item["captured_at"], "2000-01-02T03:04:05Z")

    def test_ids_separate_providers_and_fixture_from_live(self):
        ids = set()
        for provider, payload in [("actowiz", actowiz()), ("northfork", northfork()),
                                  ("xbyte_foodspark", foodspark())]:
            item = self.one(provider, payload)
            ids.add(item["id"])
            self.assertTrue(item["id"].startswith("test:" + provider + ":"))
            self.assertEqual(item["source_mode"], "fixture")
            self.assertFalse(item["live"])
            self.assertFalse(item["selectable"])
            self.assertFalse(item["transfer_verified"])
        self.assertEqual(len(ids), 3)
        fixture = self.one("actowiz", actowiz())
        live = self.one("actowiz", actowiz(), mode="live")
        self.assertNotEqual(fixture["id"], live["id"])
        self.assertTrue(live["live"])

    def test_northfork_recommendation_quantity_and_size_preserved(self):
        item = self.one("northfork", northfork())
        self.assertEqual(item["size"], "12 oz")
        self.assertEqual(item["provider_quantity"], 2)
        self.assertEqual(item["unit_of_order"], "pce")
        self.assertTrue(item["cart_transfer_supported"])
        self.assertFalse(item["transfer_verified"])
        self.assertIsNone(item["brand"])

    def test_northfork_nearby_branch_zip_is_not_shopping_zip_mismatch(self):
        selected = {"identifier": "90001", "zip": "00001", "requested_zip": "00000",
                    "provider": "instacart", "name": "Fixture Retailer",
                    "address": "PRIVATE ADDRESS OMITTED"}
        item = self.one("northfork", northfork(), selected, mode="historical")
        self.assertEqual(item["requested_zip"], "00000")
        self.assertEqual(item["observed_zip"], "00001")
        self.assertEqual(item["provider_store_id"], "90001")
        self.assertEqual(item["location_evidence"], "request_acknowledged")
        self.assertNotIn("zip_mismatch", item["quality_flags"])
        self.assertNotIn("PRIVATE ADDRESS", json.dumps(item))
        self.assertNotIn("branch_verified", item)
        self.assertNotIn("delivery_verified", item)

    def test_actowiz_compares_physical_store_context_not_shopping_zip(self):
        selected = {"store_id": "90001", "store_zip": "00001"}
        item = self.one("actowiz", actowiz(), selected, mode="historical")
        self.assertEqual(item["location_evidence"], "request_acknowledged")
        wrong = self.one("actowiz", actowiz(store_number="90002", store_zip="00002"), selected, mode="historical")
        self.assertEqual(wrong["location_evidence"], "mismatch")
        self.assertIn("store_id_mismatch", wrong["quality_flags"])
        self.assertIn("zip_mismatch", wrong["quality_flags"])

    def test_xbyte_echo_is_request_acknowledgment_only(self):
        item = self.one("xbyte_foodspark", foodspark(), mode="historical")
        self.assertEqual(item["location_evidence"], "request_acknowledged")
        self.assertEqual(item["observed_zip"], "00000")
        self.assertNotIn("PRIVATE ADDRESS", json.dumps(item))
        self.assertFalse(item["cart_transfer_supported"])

    def test_xbyte_documented_unit_price_and_availability_fields(self):
        item = self.one("xbyte_foodspark", foodspark(**{
            "Price Per Unit": "$0.25 / oz", "Availability": "highlyInStock",
        }))
        self.assertEqual(item["availability"], "in_stock")
        self.assertEqual(item["availability_raw"], "highlyInStock")
        self.assertEqual(item["unit_price"]["amount_decimal"], "0.25")
        self.assertEqual(item["unit_price"]["unit"], "oz")
        self.assertEqual(item["raw_field_types"]["Price Per Unit"], "string")

    def test_non_usd_minor_scale_is_not_assumed(self):
        item = self.one("northfork", northfork(price=399, currency_code="ISK"))
        self.assertIsNone(item["price"])
        self.assertEqual(item["currency"], "ISK")
        self.assertEqual(item["raw_price"], 399)
        self.assertIn("minor_currency_scale_unknown", item["quality_flags"])

    def test_invalid_quantity_does_not_become_one(self):
        item = self.one("northfork", northfork(quantity=True))
        self.assertIsNone(item["provider_quantity"])
        self.assertEqual(item["raw_field_types"]["quantity"], "boolean")
        self.assertIn("provider_quantity_invalid", item["quality_flags"])

    def test_fixture_never_claims_request_location_acknowledgment(self):
        item = self.one("northfork", northfork(), {"identifier": "90001", "zip": "00001"})
        self.assertEqual(item["requested_zip"], "00000")
        self.assertEqual(item["observed_zip"], "00001")
        self.assertEqual(item["location_evidence"], "unknown")
        self.assertEqual(item["source_mode"], "fixture")
        self.assertFalse(item["live"])
        self.assertIn("synthetic_fixture", item["quality_flags"])

    def test_no_location_evidence_is_unknown(self):
        item = self.one("actowiz", actowiz(store_number=None, store_zip=None))
        self.assertIsNone(item["provider_store_id"])
        self.assertIsNone(item["observed_zip"])
        self.assertEqual(item["location_evidence"], "unknown")

    def test_estimated_and_starting_prices_are_not_exact(self):
        for price, kind in [("Estimated $3.99", "estimated"), ("from $3.99", "starting_at"),
                            ("$3.99+", "starting_at")]:
            item = self.one("xbyte_foodspark", foodspark(Price=price))
            self.assertEqual(item["price"], 3.99)
            self.assertEqual(item["price_kind"], kind)
            self.assertIn("price_" + kind, item["quality_flags"])
            self.assertEqual(item["raw_price"], price)

    def test_malformed_prices_not_silently_coerced(self):
        for raw in ["$3.99-$4.99", "3,99", "N/A", -1, float("nan"), float("inf"), {}, []]:
            item = self.one("actowiz", actowiz(price=raw))
            self.assertIsNone(item["price"])
            self.assertIsNone(item["price_decimal"])
            json.dumps(item, allow_nan=False)

    def test_fractional_minor_price_rejected_and_unknown_currency_not_guessed(self):
        item = self.one("northfork", northfork(price=399.5))
        self.assertIsNone(item["price"])
        self.assertIn("minor_price_not_integer", item["quality_flags"])
        unknown = self.one("northfork", northfork(currency_code=None))
        self.assertIsNone(unknown["price"])
        self.assertEqual(unknown["raw_price"], 399)
        self.assertIn("minor_currency_scale_unknown", unknown["quality_flags"])

    def test_explicit_unit_price_only(self):
        item = self.one("actowiz", actowiz(price_per_unit="$0.25 / oz"))
        self.assertEqual(item["unit_price"], {"amount": 0.25, "amount_decimal": "0.25",
                                              "currency": "USD", "quantity": 1.0, "unit": "oz"})
        unknown = self.one("actowiz", actowiz(price_per_unit="0.25"))
        self.assertIsNone(unknown["unit_price"])
        self.assertIn("unit_price_ambiguous", unknown["quality_flags"])

    def test_currency_conflicts_not_silently_resolved(self):
        item = self.one("actowiz", actowiz(price="EUR 3.99", currency="USD"))
        self.assertIsNone(item["price"])
        self.assertIsNone(item["currency"])
        self.assertIn("currency_conflict", item["quality_flags"])

    def test_raw_types_are_whitelisted_without_address_or_credentials(self):
        item = self.one("actowiz", actowiz(security_key="fixture-secret", address="PRIVATE ADDRESS OMITTED"))
        self.assertNotIn("security_key", item["raw_field_types"])
        self.assertNotIn("address", item["raw_field_types"])
        self.assertNotIn("fixture-secret", json.dumps(item))
        self.assertNotIn("PRIVATE ADDRESS", json.dumps(item))

    def test_error_envelopes_cannot_become_products(self):
        with self.assertRaisesRegex(ValueError, "provider_error_payload"):
            normalize("actowiz", {"status_code": 200, "error": "fixture error"}, REQUEST, {}, RETRIEVED)
        with self.assertRaisesRegex(ValueError, "invalid_xbyte_foodspark_payload"):
            normalize("xbyte_foodspark", {"request_log": {}}, REQUEST, {}, RETRIEVED)


if __name__ == "__main__":
    unittest.main()

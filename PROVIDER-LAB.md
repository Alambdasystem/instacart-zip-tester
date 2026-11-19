# Independent supplier tests

Open `/provider-lab` to inspect each supplier separately or compare selected suppliers. This page uses `/api/v1/provider-tests/*`; it does not change the existing `/api/v1/search` routing.

## What the modes mean

- **Fixture** uses explicitly synthetic schema specimens. It makes no vendor requests and proves neither current availability nor product matching. The Actowiz specimen represents a known Walmart product lookup, because its delivered trial does not support grocery keyword search.
- **Live** calls the requested supplier directly. The response reports its actual supplier, upstream read count, duration, data observations and errors. Each supplier keeps its own result; fallback and caching are disabled.

All test products have `selectable: false`. Test IDs are not shopping quote IDs and cannot be used with `/api/v1/select` or `/api/v1/cart`. The test controller does not write products into the existing catalog or search cache.

## Server configuration

Fixtures are available without vendor credentials. Live mode requires all of the following:

| Setting | Purpose |
| --- | --- |
| `PROVIDER_TESTS_LIVE_ENABLED=true` | Explicitly enables live tests. |
| `PROVIDER_TEST_TOKEN` | Dedicated bearer token for these test routes. Keep it separate from vendor keys and other operator credentials. |
| `FOODSPARK_API_KEY` | X-Byte / FoodSpark credential, held on the server. |
| `XBYTE_TEST_READ_LIMIT` | Cumulative upstream read allowance for these X-Byte experiments; defaults to zero. |
| `NORTHFORK_API_KEY` | Northfork credential, held on the server. |
| `NORTHFORK_TEST_READ_LIMIT` | Cumulative upstream read allowance for these Northfork experiments; defaults to zero. |

Only configure the suppliers being tested. The separate persistent allowance counts attempted upstream calls, including calls that fail. Increasing the configured limit allows further reads; server restarts do not reset the counter. These allowances are separate from existing ingestion and comparison limits.

A normal Northfork test makes two calls: store discovery, then ingredient resolution. X-Byte makes one call per list query and requires the retailer slug plus a complete street address. Actowiz live keyword search returns `unsupported_operation`; it does not silently substitute another vendor or use the insecure trial endpoint.

The browser sends the dedicated test token only in the `Authorization` header. It does not save the token in local storage, URLs, request previews or response files. Vendor credentials never belong in this page.

## API requests

Inspect readiness with `GET /api/v1/provider-tests/status`.

Run one supplier with `POST /api/v1/provider-tests/search`:

```json
{
  "provider": "northfork",
  "zip": "63129",
  "queries": ["1 gallon whole milk", "12 eggs"],
  "retailer": "schnucks",
  "mode": "fixture",
  "fallback_policy": "none",
  "options": {
    "marketplace": "instacart",
    "in_stock": false
  }
}
```

Set `mode` to `live` and send `Authorization: Bearer <dedicated-test-token>` for a real supplier read. Northfork options can also include a `store_id` returned by discovery. Use the exact retailer or discovered store name; a nearby physical branch can have a different ZIP from the requested delivery ZIP.

Compare with `POST /api/v1/provider-tests/compare`:

```json
{
  "providers": ["xbyte_foodspark", "northfork", "actowiz"],
  "zip": "63129",
  "queries": ["1 gallon whole milk", "12 eggs"],
  "retailer": "schnucks",
  "mode": "fixture",
  "fallback_policy": "none",
  "options": {
    "northfork": {
      "marketplace": "instacart",
      "in_stock": false
    }
  }
}
```

Single-supplier `options` is the chosen supplier's direct options object. Comparison `options` is keyed by supplier ID. X-Byte and Actowiz currently accept no additional options. Add a location for X-Byte live tests:

```json
{
  "postal_code": "63129",
  "country_code": "US",
  "address": {
    "line1": "YOUR STREET ADDRESS",
    "city": "YOUR CITY",
    "region": "MO"
  }
}
```

Pass that object as the request's `location`. Its postal code must match `zip`. The placeholder address is not a usable test address.

## Reading results

Each outcome distinguishes requested supplier, actual supplier, fixture/live mode, cached data, fallback, duration and attempted upstream calls. A comparison can return HTTP 200 with per-supplier errors; inspect each outcome's `status` and `error`.

The common product shape retains raw price value, raw unit convention and raw field types alongside normalized amounts. Northfork minor currency units must be interpreted differently from X-Byte's formatted major-unit strings and Actowiz's major-unit numbers or strings. Missing prices, stock, size and source timestamps stay unknown.

Use **Fields & types** to inspect provider product IDs, UPCs, location evidence, source capture timestamps, API retrieval timestamps and quality flags. Retrieval time is not proof of the vendor's data capture time. Store acknowledgment does not prove a physical branch or delivery serviceability. Similar product names do not establish equivalent package sizes, identical products or valid savings.

Unknown stock does not prevent a record from appearing. The workbench assesses what a supplier returns; it does not place orders, transfer a basket or change Neadz's default data source.

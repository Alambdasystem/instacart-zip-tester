# Neadz agent handoff: Grocery API 0.10.1

Apply this to the current Neadz APK branch/build. Keep the latest build; an older APK does not resolve the contract mismatch. The API work does not require a new customer production account. Supplier access remains configured on the server; the app sends its selected location and retailer.

Base URL: `https://alambda-grocery-api.onrender.com`

Use the [hosted OpenAPI schema](https://alambda-grocery-api.onrender.com/openapi.json) and [Android contract](https://alambda-grocery-api.onrender.com/android-contract.md). Confirm `/health.version` is `0.10.1` before treating the deployment as this contract. This handoff is a repository file, not a new API endpoint. Examples below are synthetic; reference IDs and addresses are placeholders.

## Changes to apply

| Release | Change | APK action |
| --- | --- | --- |
| **0.10.0 breaking provider removal** | MealMe/Satsuma and Vercel Kroger/Square adapters removed. Retired names are `mealme`, `satsuma`, `mealme_satsuma`, `vercel_kroger`, `vercel_square`, `vercel_proxy`. Compare/catalog/export reject them with 410 `provider_removed`. | Delete their choices, defaults, DTO routing and retry paths. Remove provider credentials from app configuration. |
| **0.10.0 catalog/compare narrowing** | `/compare` only supports/defaults `providers:["instacart"]`; catalog `provider` is `instacart` or `all`, and `all` hides retired rows. | Use normal shopping routes for quotes; supplier tests use their separate contract. |
| **0.10.1 schema corrections** | Existing `ok`/`partial`/`cache_miss` search variants, job nullable results/errors, savings fields, operational responses and error bodies are now explicit. Existing wire types are preserved. | Regenerate/update DTOs and branch by status; do not force every 200 response into an `ok` result. |
| **0.10.1 additive contract coverage** | `/ready`, typed provider/ingestion status, source/context/catalog metadata, exact auth/error rules and documented endpoints. These fields/routes already existed at runtime. | Treat source metadata separately from the cache namespace; use optional evidence fields safely. |
| **0.10.1 small runtime fixes** | X-Byte address confirmation accepts normalized full requested address or street plus unit, while still matching ZIP/retailer. Shared deadlines return typed `provider_timeout`; recovery requires literal boolean `true`, not numeric `1`. | Preserve strict location behavior. Decode timeout errors. Do not treat the address fix as proof that the live vendor problem is resolved. |

Normal shopping fetches use **X-Byte/FoodSpark first only with configured server access + retailer slug + full address**. ZIP-only/missing-retailer requests use the Instacart browser. Cache hits may come from either source. There is no automatic Northfork routing and no supplier fallback after an X-Byte error. `/api/v1/providers/status.routing` reports this policy; configuration is not an upstream success test.

## Implement the main flow

### 1. Search using the existing app location

```http
POST /api/v1/search/jobs
Content-Type: application/json
```

```json
{
  "location": {
    "postal_code": "63129",
    "country_code": "US",
    "address": {"line1":"123 Example Lane","line2":"","city":"Example City","region":"MO"}
  },
  "queries": ["milk"],
  "retailer": "aldi",
  "allow_unverified_location": false,
  "mode": "auto"
}
```

One to three queries, each 1–120 nonblank characters. `query` is the single-term alias; `queries` takes precedence. A top-level matching `zip` may supply a missing `location.postal_code`. Optional address line2 accepts 0–100 characters. Jobs accept only `auto`, `refresh` or `cache_only`. Synchronous `/search` also accepts legacy `live` and `catalog`, but both act as cache-first `auto`; they are not a stored-only option on that route.

Creation response, HTTP 202:

```json
{"job_id":"job-a","job_token":"secret-capability","status":"queued","expires_in":600}
```

Poll `GET /api/v1/search/jobs/job-a` with `Authorization: Bearer secret-capability`. Keep the token in memory; never put it in a URL. Poll every 1–3 seconds. Read `status` even when HTTP is 200:

```json
{
  "job_id": "job-a",
  "status": "succeeded",
  "created_at": 1795219200.0,
  "started_at": 1795219200.1,
  "finished_at": 1795219201.0,
  "expires_in": 598,
  "cancel_requested": false,
  "result": {
    "status": "cache_miss",
    "items": [],
    "missing_queries": ["milk"],
    "location": {"requested_zip":"63129","verified":false},
    "cache": {"hit":false,"age_seconds":0,"expires_in":0,"sources":[]}
  },
  "error": null
}
```

This example shows a completed **cache-only** miss; `succeeded` describes completion of the job, not the existence of products. A job created in normal `auto` mode with no priced results fails instead. For a successful shopping result, `result.status` is `ok` or `partial` and includes `search_id`, `expires_in`, `cart_updated:false`, products, location, cache, `missing_queries`, `partial_errors`, `source_providers`, `coverage_complete:false`, warnings, retailers, diagnostics and retrieved_at. On `partial`, render available items while identifying missing queries. On `cache_miss`, there is no search_id or top-level expires_in: do not create cart/savings references.

Job timestamps are numeric epoch seconds; started/finished times, result and error can be null. `failed` or `cancelled` use `error:{"code":"...","message":"..."}`. Job retention is 600 seconds; quote expiry is separate and at most 300 seconds. Unknown/expired jobs or invalid tokens return 403 `job_access_denied`.

To cancel, POST exactly `{}` to `/api/v1/search/jobs/job-a/cancel` with the same token. Continue polling if status is still queued/running with `cancel_requested:true`. Only terminal `cancelled` confirms cancellation; success can win the race. Do not rely on a dropped socket as cancellation.

### 2. Decode products and keep provenance

A minimal FoodSpark product shape can contain:

```json
{
  "id":"item-a","name":"Example Milk","size":"1 gallon","price":3.49,
  "currency":"USD","retailer":"aldi","query":"milk","source":"foodspark",
  "source_provider":"xbyte_foodspark","url":null,"image_url":null,"availability":null,
  "price_basis":"unspecified","price_basis_verified":false
}
```

`size`, `image_url`, `url` and `availability` are nullable. Price is a JSON number; use a decimal money representation. Load an image placeholder and omit link actions when URL is null. Do not invent stock, unit or URL evidence.

Use `source_provider` (`xbyte_foodspark` / `instacart_browser`) for the actual fetch source. `provider:instacart` is a legacy storage namespace and can contain X-Byte data. Search `source_providers` lists represented sources; cache sources describe storage access. Optional nested `provider_context` is browser evidence and may be absent on X-Byte responses.

Location verification is limited: `verified`/matching observed ZIP is not delivery eligibility or merchant branch verification. Keep `context_id` opaque. Fresh items expose price/context/evidence flags. Expired catalog rows can still contain a numeric historical price; require `selectable:true` to use a catalog reference and never infer freshness from a non-null amount.

### 3. Validate app cart; calculate savings only when eligible

```http
POST /api/v1/cart
Content-Type: application/json
```

```json
{"lines":[{"search_id":"search-a","item_id":"item-a","quantity":2}]}
```

Maximum 60 distinct pairs; integer quantity 1–99. `item_count` is summed quantity. The response carries server-computed line totals/subtotal and both `cart_updated:false` and `provider_cart_updated:false`. The app still owns its cart. `{}` or empty lines produces an empty cart response. `/select` is optional; it deduplicates IDs and does not create a provider basket.

```http
POST /api/v1/savings
Content-Type: application/json
```

```json
{"offers":[{"search_id":"search-a","item_id":"merchant-a-item"},{"search_id":"search-b","item_id":"merchant-b-item"}],"quantity":2}
```

Only server-issued references are accepted, never client prices. Required eligibility: two distinct merchants; same acknowledged opaque location/ZIP; unexpired searches; matching provider product identity from its Instacart product URL, name and size; USD; verified matching per-item charge unit; no unvalidated promotion. Current FoodSpark output lacks verified charge-unit/product-URL evidence, so it does **not** qualify merely because it has a price and matching address.

The successful savings response is fully typed in OpenAPI. Its totals portion looks like this (the actual response also includes both complete product offers with their search_ids and the location object):

```json
{
  "status":"verified_observation_comparison","compared_at":"2026-11-21T00:00:00Z",
  "quantity":2,"currency":"USD","original_subtotal":8.0,"replacement_subtotal":6.0,
  "savings":2.0,"savings_percent":25.0,
  "lower_price_merchant":"merchant-b","higher_price_merchant":"merchant-a",
  "expires_in":270,
  "scope":"Observed merchandise prices only; excludes taxes, fees, delivery and checkout verification."
}
```

The higher price determines `original_subtotal` and the lower price determines `replacement_subtotal`, regardless of request order; returned offers stay in request order. Expire the savings display at the earliest quote expiry and recalculate when product, quantity or context changes. A fixture can prove decoding/math; it cannot establish live local savings.

## Exact failures the APK must handle

| HTTP / code | Required behavior |
| --- | --- |
| 403 `job_access_denied` | Job/token missing, wrong, expired or lost on restart. Stop polling that job. |
| 404 `search_expired` from select | Run a new search and select with new references. |
| 409 `price_expired` / `mixed_location_context` from cart | Refresh affected offers or keep the cart in one context. |
| 409 from savings | Hide the savings claim. Codes: `price_expired`, `location_unverified`, `price_unit_unverified`, `conditional_price`, `provider_unsupported`, `mixed_location_context`, `different_merchants_required`, `product_mismatch`, `price_unit_mismatch`, `weighted_quantity_required`. |
| 410 `provider_removed` | Delete the old choice and stop retrying it. |
| 422 `invalid_request` / `invalid_provider` / `location_required` / `location_unsupported` | Correct input or show the location limitation. Do not silently loosen location verification. |
| 429 `busy` / `rate_limited` | Back off with jitter, e.g. 5/15/30 seconds with a cap; avoid duplicate jobs. |
| 502 provider failure | Read `error ?? status` and optional message. Known codes include timeout, quota, screening/login, no_results, context_changed and connector_error. Preserve the exact code and request ID. No automatic provider fallback. |
| 503 `connector_disabled` | Show temporary shopping-service unavailability. |

Non-2xx bodies can omit message/items/location. Job failure codes are nested under `error.code` within HTTP 200 status responses. Missing optional fields must not crash decoding. Keep money as numbers, booleans as booleans, job times as numbers; `providers/status.activity[].cache_hit` is the legacy exception, an integer 0/1.

## Verification and remaining live gate

Run local fixture/contract tests for ok/partial/cache_miss, job polling and cancellation, source labels, null images/URLs, reference expiry/restart, mixed-context cart, retired-provider errors and every savings eligibility failure. The repository's contract test suites exercise real local HTTP handlers with mocked/synthetic providers; they do not spend supplier credits.

Verify deployment with `/health` (version) and `/ready` (SQLite). Read `/api/v1/providers/status` for conditional routing and `/api/v1/ingestion/status` for operational state. Neither endpoint proves supplier availability. `running:true` in ingestion can coexist with blocked retailer contexts and zero fresh acknowledged local offers. Existing observed catalog data must not be relabeled as local inventory.

The pre-patch live X-Byte path returned 422 `location_unsupported`; the full-address acceptance fix addresses a real implementation mismatch, but that prior upstream failure's cause remains unproven until an authorized live retest. Preserve the actual location error if it persists. Release claims about working live X-Byte search or verified savings require that live result plus acceptance on the current installed APK, including build number, request IDs, source/observed ZIP, expiry and screenshots.

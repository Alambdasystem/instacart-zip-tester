# Neadz / InstaSearch API Contract v0.11.0

Base URL: `https://alambda-grocery-api.onrender.com`

- [OpenAPI 3.1 schema](https://alambda-grocery-api.onrender.com/openapi.json)
- [Interactive API documentation](https://alambda-grocery-api.onrender.com/docs)
- [Hosted Android contract](https://alambda-grocery-api.onrender.com/android-contract.md)
- [Neadz migration handoff](https://github.com/Alambdasystem/instacart-zip-tester/blob/main/neadz-migration-0.10.1.md)

This document describes the current wire contract. Version 0.12.1 makes checkout retailer-neutral; the 0.10.1 contract explicitly typed existing search variants, jobs, savings and operational responses; it does not rename their fields or change JSON number/boolean/string types. All POST bodies are UTF-8 JSON objects with `Content-Type: application/json`, between 1 and 10,000 bytes. JSON responses include `X-Request-ID`; retain it with the app build and HTTP status when diagnosing a failure, without logging credentials or full addresses.

## Provider routing and provenance

New fetches for `/api/v1/search` and `/api/v1/search/jobs` use **X-Byte/FoodSpark when all three requirements are present**: configured server `FOODSPARK_API_KEY`, a retailer slug, and a full `location.address` containing `line1`, `city` and `region`. Supply the ZIP as `location.postal_code` or top-level `zip`. Missing any requirement selects the existing Instacart browser. Both shopping routes also require the server's `CONNECTOR_ENABLED` gate. An X-Byte failure returns an error; it does not trigger a browser or Northfork retry.

Fresh exact snapshots can satisfy a request before a new fetch. Therefore the configured routing preference is not proof of the source of every returned item:

| Field | Meaning |
| --- | --- |
| `item.source_provider` | Actual shopping fetch transport: `xbyte_foodspark` or `instacart_browser`. Preserve it for labels and diagnostics. |
| `source_providers` | Transports represented in the returned search items; a partial/cache result can contain more than one. |
| `item.provider` | Legacy `instacart` cache/catalog namespace, when present. It can contain X-Byte observations. |
| `item.source` | Older provenance string, such as `foodspark` or `instacart_public_browser`; retain for compatibility when `source_provider` is absent in older saved app data. |
| `cache.sources` / `item.cache_source` | Where a snapshot was read, such as `live`, `memory_cache`, `persistent_cache` or `catalog_discovery`; these are not supplier names. |

MealMe, Satsuma and the Vercel Kroger/Square adapters were removed in 0.10.0. Do not offer or send `mealme`, `satsuma`, `mealme_satsuma`, `vercel_kroger`, `vercel_square` or `vercel_proxy`. Explicit retired selections on compare, catalog and export return **410 `provider_removed`**. Catalog `provider=all` excludes retired rows. Historical rows retained in the database are not active API results.

X-Byte and Northfork also have an **independent test contract** at `/api/v1/provider-tests/*`, described in [PROVIDER-LAB.md](https://github.com/Alambdasystem/instacart-zip-tester/blob/main/PROVIDER-LAB.md) and [/provider-lab](https://alambda-grocery-api.onrender.com/provider-lab). Its fixture mode is synthetic; live mode requires the dedicated test bearer token and allowance. Those results never issue shopping quotes, populate a cart or replace normal shopping routing. Actowiz live keyword search is unsupported. `live_ready` in test status means configuration and allowance pass, not that a live supplier request has succeeded. Background ingestion remains browser based.

## Core request and mode rules

Shopping search requires a five-digit US ZIP and `query` or `queries`. `queries` takes precedence when both are supplied. Send one to three nonblank strings of at most 120 characters; the server trims and deduplicates them. `retailer` is empty or a lowercase slug matching `[a-z0-9][a-z0-9-]{0,63}`. Empty retailer discovers visible provider-context retailers, subject to provider coverage.

A request can supply `zip`, `location.postal_code`, or both with the same value. `location.postal_code` can be omitted when top-level `zip` supplies it. `country_code` defaults to `US`. Address `line1`, `city` and `region` must be nonblank strings of at most 200 characters; `line2` is optional, may be empty, and is limited to 100 characters. Coordinates require finite numeric latitude/longitude in their normal ranges. Send an address or coordinates, not both. Acceptance of the input shape does not establish supplier support or location verification.

```json
{
  "location": {
    "postal_code": "63129",
    "country_code": "US",
    "address": {
      "line1": "123 Example Lane",
      "line2": "",
      "city": "Example City",
      "region": "MO"
    }
  },
  "queries": ["milk", "eggs"],
  "retailer": "aldi",
  "allow_unverified_location": false,
  "mode": "auto"
}
```

This is a synthetic shape example. The app supplies its selected address. Do not silently replace it, or retry with `allow_unverified_location=true` after a strict-location error.

| Route / mode | Behavior |
| --- | --- |
| Shopping `auto` | Read fresh memory/exact persistent snapshots; fetch missing queries. |
| Shopping `cache_only` | No upstream fetch and no refresh scheduling. May return `partial` or `cache_miss`. |
| Shopping `refresh` | Ask for a fresh snapshot; concurrent refreshes may coalesce. It does not renew an already issued quote ID. |
| Synchronous `/search` `live` or `catalog` | Legacy aliases of cache-first `auto`. **They are not stored-only modes on this route.** |
| `/search/jobs` modes | Only `auto`, `refresh` and `cache_only`; `live`/`catalog` return 422. |
| `/compare` `catalog` | Retained catalog observations only, including expired data with flags. |
| `/compare` `cache_only` | Fresh exact snapshots only. |
| `/compare` `auto` / `live` / `refresh` | Fresh fetches use the browser; shared cached data may have X-Byte provenance. |

## Recommended shopping sequence

1. Optionally read `/api/v1/catalog?zip=63129&queries=milk&queries=eggs` for a stored preview. Repeat `queries`; do not comma-join terms. Matching is OR. Empty results do not prove local unavailability. An address-specific shopping flow should obtain fresh compatible search results before committing selections.
2. Create a search job, or use synchronous `/api/v1/search`. Decode the HTTP status and then the response `status`.
3. Render returned product metadata and its freshness/location evidence. Persist selected lines in the app as `search_id`, `item_id` and integer `quantity`. IDs are opaque; do not invent identifiers, reuse an item with a different search ID or infer cross-merchant equivalence from a name.
4. Optionally call `/api/v1/select` for IDs from one search. It deduplicates repeated IDs and returns a selection subtotal, not a persistent basket. `selection_id` is not needed by cart or savings.
5. Call `/api/v1/cart` to validate local lines against server-held prices. The app owns persistence and removal. Revalidate after quantity/context changes and before displaying a current subtotal.
6. Call `/api/v1/savings` only when two referenced offers qualify for its stricter equivalence and charge-unit rules. Use its returned totals and expiry.

### Shopping response variants

| HTTP / `status` | Shape and action |
| --- | --- |
| 200 `ok` | `search_id`, `items`, `location`, `cache`, `expires_in`, `cart_updated:false`, `missing_queries:[]`, `partial_errors`, `source_providers`, `coverage_complete:false`, `warnings`, `retailers`, `diagnostics`, `retrieved_at`. Continue with returned references. |
| 200 `partial` | Same shape, with uncovered `missing_queries`; `partial_errors` may explain why. Display available results and clearly identify missing searches. Do not report that every term succeeded. |
| 200 `cache_miss` | Only `status`, empty `items`, `missing_queries`, `location` and `cache`. **No `search_id`, top-level `expires_in` or `cart_updated`.** Ask for an explicit normal/refresh search when appropriate. |
| Non-2xx | Decode `error ?? status`, optional `message`, `items` and `location`. Empty items on an error are not a successful empty search. |

The schema models these variants separately. Do not make a search ID mandatory for every HTTP 200 body. `retrieved_at` is a string; it can be empty for older retained snapshots lacking a timestamp. Cache age and expiry remain authoritative.

### Cancellable jobs and authentication

`POST /api/v1/search/jobs` is public and returns HTTP 202:

```json
{"job_id":"opaque-job-id","job_token":"opaque-capability","status":"queued","expires_in":600}
```

The token is disclosed only on creation. Keep it in memory; send it in `Authorization: Bearer <job_token>` to `GET /api/v1/search/jobs/{id}` and `POST /api/v1/search/jobs/{id}/cancel` with exactly `{}`. Do not place it in URLs, analytics or app logs.

Poll at a modest interval, for example one second initially and two to three seconds for longer waits. `queued` and `running` continue; `succeeded`, `failed` and `cancelled` are terminal. Poll/cancel returns HTTP 200 even when the job failed. On `succeeded`, `result` contains the shopping union above, including possible `partial` or `cache_miss`. On failure/cancellation inspect `error.code` and `error.message`.

`created_at`, nullable `started_at` and nullable `finished_at` are **Unix epoch numbers**, not date strings. `result` and `error` are nullable. Jobs are retained for 600 seconds from creation; quote expiry inside the result is shorter and independent. Polling renews neither. Missing/invalid tokens, unknown IDs, expired jobs and jobs lost on restart all return 403 `job_access_denied`, not 404.

Cancellation is asynchronous: its response can still say `running` with `cancel_requested:true`. Keep polling to a terminal state; only `cancelled` confirms cancellation. A completed result can win the race. A network disconnect alone is not an acknowledged cancellation. Two active jobs and 32 retained jobs are allowed; creation is rate-limited even in `cache_only` mode.

## Products, location and catalog eligibility

`price` is a JSON number in USD, not a formatted string. Use a decimal money type in Android. `size`, `image_url`, `url` and `availability` can be null. Use an image placeholder, and show a provider-link action only when a returned usable URL exists. Never manufacture X-Byte images, URLs, stock states or checkout links.

Fresh shopping items already include `price_fresh`, `price_expires_in`, `age_seconds`, `requested_zip`, `observed_zip`, `location_verified`, `context_id`, `local_offer_eligible` and `discovery_only`. Retained older products can lack newer evidence fields. Missing evidence must not be interpreted as verification. Product `provider_context` and location `provider_context` are optional; FoodSpark does not currently provide the nested browser location evidence object.

`location.verified` acknowledges the requested ZIP/context for these observations. It does not verify a merchant branch, local stock or delivery serviceability. `observed_zip` can be null or different from the request. `context_id` is an opaque identity; compare it exactly, without deriving it from an address. `address_selected` does not upgrade `verification_scope:zip_only` into a delivery guarantee. `store_branch_verified` and `delivery_serviceability_verified` currently remain false.

Catalog and export use `provider=instacart` by default or `all` for the allowed namespace only. Without `zip`, `location.requested_zip` is null. With a ZIP, mismatched/unacknowledged observations are excluded unless the exact query-string value `allow_unverified_location=true` is supplied. `excluded_unverified_observations` explains withheld rows. Query values are limited to the first three and truncated to 120 characters by the catalog handler.

An expired catalog item's numeric `price` remains historical; it is **not automatically erased or changed to null**. `last_seen_price`, `price_status`, `price_fresh`, `age_seconds` and `last_seen` describe its history. Only `selectable:true` catalog items are included in the server snapshot behind the returned catalog `search_id`. A response with no selectable items still has a display ID but cannot validate selections. `local_offer_eligible` additionally requires verified primary charge-unit evidence; catalog `selectable` alone does not establish savings eligibility.

Catalog metadata includes nullable `brand`, `provider_location_id` and `fulfillment`, opaque `catalog_key` and `product_key`, and boolean `context_compatible`, `branch_verified` and `delivery_verified`. Do not replace item IDs with storage keys. The catalog is bounded discovery data, not complete inventory.

## X-Byte to Northfork checkout handoff

The intended product flow is retailer-first X-Byte grocery search, product selection, Northfork product resolution for the selected Northfork checkout store, then a browser handoff to the selected retailer’s own cart. X-Byte remains the source of the initial grocery product observations. The currently documented X-Byte grocery search requires `retailer` plus a complete street address and yields one store context; X-Byte ZIP-only store discovery is not present in the delivered grocery API docs. `GET /api/v1/checkout/stores?zip=63129` returns all Northfork-enabled checkout store candidates, clearly labeled with `source_provider: "northfork"`; it is not a replacement or claim of X-Byte store discovery.

1. Run `/api/v1/search` with the chosen retailer slug and address. Keep `search_id`, `item.id`, `item.source_provider`, `item.retailer`, product name/brand/size, and ZIP. Checkout requires `source_provider: "xbyte_foodspark"`; browser-observed or cached Instacart products cannot be passed as X-Byte results.
2. List candidate Northfork checkout stores with `GET /api/v1/checkout/stores?zip=<ZIP>`. The list covers the retailers enabled for this Northfork account and ZIP; it is not a claim that Northfork supports every store worldwide. The chosen store must match the X-Byte retailer and ZIP. Store IDs are provider-specific; never pass an X-Byte store/location ID as a Northfork store ID.
3. Resolve with `POST /api/v1/checkout/resolve`: `{"search_id":"<search_id>","store_id":"<Northfork store identifier>","store_provider":"<Northfork store provider>","lines":[{"item_id":"<X-Byte item.id>","quantity":1}]}`. The response includes a short-lived `checkout_id` and Northfork candidate products for each X-Byte selection. It does not update a cart.
4. The shopper chooses one Northfork candidate per line, then calls `POST /api/v1/checkout/handoff` with `{"checkout_id":"<checkout_id>","items":[{"item_id":"<X-Byte item.id>","product_id":"<Northfork candidate id>","quantity":1}]}`.
5. Open the returned `handoff_url` in the browser. Northfork returns a retailer-specific redirect and transfers the selected products into that retailer’s cart when its integration supports the selected store. This API does not follow the redirect and does not place an order. `handoff_started:true` means Northfork returned a redirect; `cart_updated:false` remains explicit until the shopper signs in and verifies the final cart. The shopper must inspect the selected retailer’s cart before checkout.

Northfork API access is held in server configuration (`NORTHFORK_API_KEY`). Never put vendor credentials in the mobile app. X-Byte store discovery remains a vendor contract dependency; do not present the Northfork candidate list as X-Byte discovery.

## Selection, cart and savings

Selection request:

```json
{"search_id":"returned-search-id","item_ids":["returned-item-id"]}
```

Selection supports 1–60 IDs, deduplicates them and rejects IDs absent from that search. A missing, expired or restarted search returns 404 `search_expired`. A valid selection response includes `selection_id`, items, `subtotal`, `currency:"USD"`, location, a message and `cart_updated:false`.

Cart request:

```json
{"lines":[{"search_id":"returned-search-id","item_id":"returned-item-id","quantity":2}]}
```

Cart supports up to 60 distinct `(search_id,item_id)` pairs with quantity 1–99. `{}` and `{"lines":[]}` both validate an empty cart. The response includes items with `quantity`, `line_total`, `search_id` and `location_verified`; `item_count` is the sum of quantities. Both `cart_updated` and `provider_cart_updated` are false. The endpoint validates a local subtotal; it neither persists a server cart nor transfers a provider basket. Different opaque contexts return 409 `mixed_location_context`; invalid/expired references return 409 `price_expired`.

Savings request:

```json
{"offers":[{"search_id":"fresh-search-a","item_id":"merchant-a-item"},{"search_id":"fresh-search-b","item_id":"merchant-b-item"}],"quantity":2}
```

Supply exactly two references and optional integer `quantity` (default 1, range 1–99). No client prices or extra fields are accepted. Both searches must remain unexpired and acknowledge the same opaque location context/ZIP. Merchants must differ. The implementation requires the same Instacart product identity in the HTTPS product URL, matching product name and package size, USD, matching explicitly verified primary charge units, `price_basis:per_item`, and no unvalidated promotion. The legacy provider namespace must be `instacart`. Current FoodSpark products have `price_basis:unspecified`, `price_basis_verified:false` and no product URL; that payload does not qualify for this savings calculation even when the address is acknowledged.

A successful response contains **all** these fields:

- `status:"verified_observation_comparison"`, UTC `compared_at`, and the two `offers` in input order, each with its `search_id`.
- `quantity`, `currency:"USD"`, `original_subtotal` (higher observed unit price × quantity), `replacement_subtotal` (lower observed unit price × quantity), `savings` and `savings_percent`. Input order does not choose the higher/lower role. Amounts and percent are rounded to two decimals; zero original subtotal produces zero percent.
- `lower_price_merchant`, `higher_price_merchant`, `expires_in` (the earlier reference expiry), `location` and `scope`.

The comparison covers observed merchandise prices only; taxes, fees, delivery and checkout verification are outside its scope. Remove the savings claim at expiry or after product, quantity or location changes until a new calculation succeeds. Unknown offers/malformed bodies return 422 `invalid_request`. Eligibility failures return 409 with one of:

`price_expired`, `location_unverified`, `price_unit_unverified`, `conditional_price`, `provider_unsupported`, `mixed_location_context`, `different_merchants_required`, `product_mismatch`, `price_unit_mismatch`, `weighted_quantity_required`.

## Errors, deadlines and retries

| HTTP | Routes / codes | App behavior |
| --- | --- | --- |
| 403 | Job `job_access_denied`; operator `operator_required`; live supplier-test auth errors | Obtain/use the correct existing capability for that route. A job cannot be recovered by guessing an ID. |
| 404 | Selection `search_expired` | Run a new search and use its new references. |
| 409 | Cart/savings eligibility; cancelled search; recovery state | Read the code and fix the relevant context, quote or operation. Do not retry expired references unchanged. |
| 410 | Compare/catalog/export `provider_removed` | Remove retired provider choices and stop retries. |
| 422 | `invalid_request`, catalog `invalid_provider`, search `location_required` / `location_unsupported` | Correct request shape or surface the supplier/location limitation. Do not enable unverified results silently. |
| 429 | `busy` / `rate_limited` | Back off with jitter. Suggested app policy: 5, 15, 30 seconds, at most three retries. |
| 502 | `provider_timeout`, `provider_blocked`, `login_required`, `provider_quota`, `no_results`, `provider_context_changed`, `connector_error` | Preserve the failure and its request ID; no alternate supplier/data is substituted. Stop automatic retries on login/screening restrictions. |
| 503 | Search `connector_disabled`; readiness `not_ready` | Surface temporary service unavailability. |

One provider search occupies the shared foreground slot. Synchronous fresh searches are limited to three per minute per source IP; several app users behind one backend can share it. Fresh job creation has its own capacity constraints and also consumes the per-IP creation limit. A single synchronous request with up to three terms is one request for this limit. Catalog reads are not a promise that a subsequent live request will succeed.

Foreground queue waits are bounded to 15 seconds normally or 65 seconds for coalesced refresh, within a shared 120-second request/job budget. Use a configurable read timeout that accommodates that budget and transport overhead. `provider_timeout` now reports shared deadline expiry explicitly instead of a generic connector failure or dropped connection. Do not overlap abandoned searches; cancel jobs when leaving their screen.

## Persistence and operations

Prices expire within 300 seconds without sliding renewal. `cache.hit`, `partial_hit`, `hit_queries`, `fetched_queries`, `sources`, `age_seconds`, `expires_in` and `ttl_seconds` describe returned snapshots. Background refresh can prepare a new snapshot but never extends a previously issued `search_id`. At expiry, fetch again and use new references.

The deployed Docker service uses SQLite on its configured persistent disk. Exact snapshots, descriptions and scheduler state can survive process restart, with their original expiry. Shopping quote IDs and jobs/tokens and precise demand-refresh contexts are process-local and can disappear earlier than their advertised lifetime on restart. Persistent storage does not turn expired data into current prices. The status endpoint exposes whether a persistent mount is configured.

| Endpoint | Authentication / meaning |
| --- | --- |
| `GET /health` | Public liveness, loaded contract version and freshness-policy flag. No supplier probe. |
| `GET /ready` | Public SQLite access check; 200 `ready` with storage flags or 503 `not_ready`. No supplier probe. |
| `GET /api/v1/providers/status` | Public routing/session configuration and recent comparison activity. `activity[].at` is epoch seconds; `activity[].cache_hit` is integer **0/1**, not a boolean. |
| `GET /api/v1/ingestion/status` | Public worker diagnostics. `offset` defaults 0, clamped nonnegative; `limit` defaults 50, clamped 1–100. Invalid numeric text returns 422. Disabled worker returns only `enabled:false` and `catalog`. |
| `POST /api/v1/ingestion/recover` | Requires server-local `OperatorBearer` and exactly `{"acknowledge":true}`. Numeric `1` is rejected. 200 is `already_running` or `recovered`; 403 auth failure; 409 disabled worker or provider recovery failure. |
| `POST /api/v1/cache/invalidate` | Currently public prototype maintenance endpoint, rate-limited once per retailer/ZIP group per minute. Takes retailer slug or `*`, plus optional ZIP; omitted or null ZIP means every ZIP. Not a normal APK search step. |

Normal shopping/search/select/cart/savings and public status routes currently require no application bearer token. Do not invent a shared API credential or embed supplier/operator/test secrets in the APK. Job capabilities, operator recovery credentials and isolated live-test credentials are different credentials with different scopes.

Enabled ingestion status exposes queue/pagination, nullable active query/last error/pause/circuit/heartbeat, scheduler tiers, admitted refresh capacity, resources and catalog counts. Queue/job/activity timestamps are numeric epoch seconds; product `last_seen`, comparison `compared_at` and savings `compared_at` are UTC strings. `running:true` does not mean every job or price is fresh. `work_state` is one of `scheduled`, `fetching`, `no_eligible_jobs`, `provider_paused` or `stopped`.

A login-required browser discovery context is suspended by ZIP/retailer. Other eligible contexts can continue; screening pauses the browser worker. Recovery uses one bounded probe of the retained failed context. An unrelated success does not clear a pause. Use the existing server-side recovery tool; never ship its credential to the APK.

## Comparison and diagnostics are separate from shopping

`POST /api/v1/compare` defaults to `providers:["instacart"]` and `mode:"catalog"`. HTTP 200 means the comparison envelope completed; inspect each `providers[].status` and `providers[].error`. Per-provider states are `ok`, `empty`, `error`, `partial`, `cached` and `not_cached`. All comparison items have `selectable:false`, retain source provenance and cannot be used as shopping quotes. Use provider-test routes for explicit X-Byte/Northfork testing, not retired comparator names.

`POST /api/v1/diagnostics/http-compare` accepts exactly one distinct query and returns either `status:compared` with both branches and a typed summary, or `status:provider_blocked` with `browser:null` when direct screening stops the comparison. **The direct anonymous HTTP request runs even when `browser_mode:cache_only`.** That mode controls only the browser branch. Direct HTML has no established local delivery context, creates no shopping quote and does not alter normal search routing. Diagnostic parity gates do not approve a production rollout.

## Android acceptance

Use the current APK branch/build and its existing networking/image libraries. Decode nullable/optional fields, every shopping status, job capabilities, expiry and provenance before relying on live inventory. Exercise partial and cache-miss results, job cancellation/expiry, null images/URLs, restarted search IDs, mixed-context carts, retired providers and every savings rejection path with controlled fixtures.

Device acceptance also needs a real authorized supplier response for the app's selected location and a qualifying pair of offers before claiming live local savings. Contract/fixture success proves parsing and guardrails, not supplier availability or delivery coverage. Record the APK build, request IDs, source transport, observed location, price-unit evidence, expiry and screenshots; retain the precise provider error when real data fails.

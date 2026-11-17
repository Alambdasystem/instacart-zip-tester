# InstaSearch API Contract v0.8.2

Base URL: https://alambda-grocery-api.onrender.com/
Interactive documentation: /docs
Machine-readable source of truth: /openapi.json (OpenAPI 3.1)
This version describes the v0.8.2 implementation. Check the running /health version and rollout state before assuming a capability is enabled. The optional X-Byte path remains disabled by default.
All POST bodies: Content-Type: application/json. UTF-8 JSON responses.

## Integration sequence

1. GET /api/v1/catalog?zip=63129&queries=milk&queries=eggs for stored results. Each query parameter is repeated, not comma-joined. Terms match with OR. An empty catalog means no matching observations; it does not establish local unavailability.
2. POST /api/v1/search if a fresh provider fetch is needed. Send {"zip":"63129","queries":["milk","eggs"],"retailer":"","allow_unverified_location":false}. Maximum 3 terms, 120 characters each; live retailer filters: empty (all visible provider-context retailers) or a retailer id returned in retailers. `query` is a single-term alias. `queries` takes precedence.
3. Render items using id, name, size, price, currency, retailer, image_url and url. IDs are opaque snapshot identifiers, not stable SKU/UPC identifiers. Deduplicate across providers only with an explicit verified mapping, never the name alone.
4. Persist selected lines on the Android side: search_id + item_id + quantity. Optional POST /api/v1/select validates item_ids within one search; selection_id isn't required for cart validation.
5. POST /api/v1/cart with {"lines":[{"search_id":"returned-search-id","item_id":"returned-item-id","quantity":2}]}. Maximum 60 distinct (search_id,item_id) lines; quantity is an integer 1–99. Empty lines is supported. The response contains item_count (sum of quantities), items with line_total, subtotal, currency and location_verified. Server totals exclude taxes and fees. No persistent server cart is created; Android owns cart storage and removal.
6. On 409 price_expired, rerun affected searches and require users to confirm replacement products/prices. Don't submit old item IDs against new search IDs. Snapshot state may disappear during a restart before five minutes.

## Model rules

- Product.size is nullable. Product.image_url: nullable HTTPS URL. Load asynchronously with your existing image library, cache by URL, and show a placeholder for null/HTTP failure. Images remain hosted by the provider and can change or expire.
- Product.price and currency are nullable. Preserve price_kind, price_label and price_basis; an observed amount can be exact, estimated, starting_at or unknown. Never decode a missing price as $0, assume USD for null currency, or assume the main amount is per item. The API owns monetary calculations; Android displays its returned totals.
- Product.price_fresh, price_expires_in, age_seconds and selectable can appear in both catalog and fresh-search products. last_seen is a UTC timestamp for the local observation when supplied. For X-Byte, source capture/expiry evidence is separate from retrieval time and cache TTL; cache freshness alone cannot make its price fresh. Missing optional fields must not crash decoding.
- Catalog: only selectable=true items can be submitted using the returned search_id. Stale metadata may remain visible, but stale prices aren't valid for cart validation. A catalog search_id with zero selectable items doesn't create a usable selection snapshot.
- SearchResponse is a status union. ok/partial supplies search_id, items, location, cache, expires_in and cart_updated=false; partial also identifies missing_queries. cache_miss supplies empty items, missing_queries, location and cache, with no search_id. no_match supplies empty items, error=no_match and message, with no quote/cache token. Catalog response remains status=catalog, search_id, items, location, cache, warnings, discovery and catalog.
- Location: requested_zip is the requested partition, observed_zip can be null, verified is authoritative. Never label unverified results as local inventory. Strict mode may return 422 location_required. Unverified demo results require an explicit user toggle; don't silently retry with allow_unverified_location=true.
- Preserve unknown fields for forward compatibility where practical. Missing optional fields must not crash Android decoding.

## Errors and retries

Non-2xx bodies may use `error` or legacy connector `status` as the error code; decode both with message, optional items and location. The schema intentionally describes the current service rather than inventing a unified envelope.

| HTTP | Code / meaning | Android handling |
| --- | --- | --- |
| 404 | search_expired | Search again before selecting |
| 409 | price_expired | Refresh affected cart lines |
| 422 | invalid_request / location_required | Fix input or show location limitation |
| 429 | busy / rate_limited | Back off with jitter; don't retry in a tight loop |
| 502 | provider_timeout / provider_blocked / login_required / no_results / connector_error | Show a provider error; retain previously displayed data with its original age |
| 503 | connector_disabled | Show temporary unavailability |

Live browser capacity is one concurrent search and three fresh searches per minute per source IP. Several mobile users behind one backend may share that rate limit. Suggested client backoff for busy/rate limit: 5s, 15s, 30s, maximum three attempts. Stop automatic retries for login_required/provider_blocked. Don't start overlapping searches. Cancel abandoned Android requests, and use a generous configurable read timeout (e.g. 120s) for live searches/cold starts; catalog reads should be fast after wake-up.

## Cache and operational boundaries

Local exact-cache/quote lifetime is at most 300 seconds, non-sliding. X-Byte source freshness additionally requires explicit source capture and expiry evidence; reading a daily snapshot now does not establish a fresh retailer price. Return cache.hit, partial_hit, hit_queries, fetched_queries, age_seconds and expires_in when present. Re-reading doesn't extend freshness. New ZIP/term payloads can enqueue collector jobs within bounded capacity; enqueue isn't a promise of fresh or geographically verified data. Background refresh only runs while this free service is awake. Catalog and snapshots aren't durable across deployment/restart on the current free setup.

POST /api/v1/cache/invalidate is an operator action, not a normal Android search step. It accepts a retailer slug or * plus optional ZIP and expires related price snapshots. GET /api/v1/ingestion/status and /api/v1/catalog/export are diagnostics.

## Authentication and provider scope

This is an unauthenticated prototype; don't embed an invented API key or send customer credentials/payment details. Production integration needs an agreed authenticated gateway and durable storage. The core grocery search keeps the existing Instacart provider interface. Optional provider comparison is documented below. X-Byte supplies an optional Instacart marketplace observation path under the existing provider identity; source=foodspark and data_provider=xbyte_foodspark identify the supplier. A client cannot enable the pilot or choose a new vendor route through the search body.

Both cart_updated and provider_cart_updated remain false. No Instacart basket mutation, ordering, payment, or delivery promise is provided.

## To mirror your app exactly

Provide the Android repository/branch and its current product/search/cart DTOs (or one sample request and response). Also identify the existing networking and image-loading libraries. That lets us supply a compiling adapter with exact field mappings instead of assuming Kotlin, React Native, or Capacitor.

## Optional provider-comparison contract

`POST /api/v1/compare` accepts the same ZIP/queries/retailer/location flag plus
`providers: ["instacart", "mealme_satsuma"]` and `mode: "catalog" | "live"`.
Inspect `providers[].status` and `providers[].error` separately: one source can
succeed while another fails. `items` are provider-scoped; never merge on display
name or reuse IDs across sources. Image URLs are optional. MealMe currently uses
Satsuma, not the Vercel proxy. Its raw `provider_price` and currency are diagnostic
until units are verified; `price: null` and `selectable: false` prohibit cart use.
Comparisons are observational, not a unified provider checkout.

## Retailer discovery and geographic scope

SearchResponse.retailers lists visible retailer sections (id, name, products_returned, branch_verified=false). diagnostics lists query, cards_seen, products_returned, retailers_with_products, settled, page_url and observed_zip. The public page is bounded to 500 products per query, without a per-retailer cap, collected round-robin after stabilization; it is not a complete catalog. Exact retailer requests filter returned links to that retailer id.

location.verified means only that the active address-control ZIP matches the requested ZIP after the search. verification_scope=zip_only, store_branch_verified=false and delivery_serviceability_verified=false. Do not infer a local store from a product brand, retailer slug, or requested ZIP partition. Unverified cached rows keep their source observed_zip and must remain labeled as provider-context observations.


## v0.4 location and cache flow
SearchRequest accepts legacy `zip` or `location.postal_code` with country US and optional address (line1, city, region) OR coordinates (latitude, longitude). Conflicting ZIPs and unknown fields return 422. Address/GPS values are not silently discarded: unsupported provider modes return location_unsupported. Address selection requires provider acknowledgment and does not prove delivery serviceability.

Modes: auto checks fresh memory then exact persistent snapshots and fetches misses; cache_only never fetches; refresh bypasses caches. Legacy live remains cache-first and catalog remains discovery-only. A cached result does not prove local stock. Search cache metadata includes sources; per-provider items include cache_source. Prices expire within five minutes; persistent reads do not extend expiry. Selection tokens remain process-local and expire on restart. Foreground searches wait up to 90 seconds for the single browser slot, ahead of the next background job.


### Diagnostic transport comparison

`POST /api/v1/diagnostics/http-compare` accepts the search request with exactly one query. It returns `direct_http`, `browser`, and `summary` (counts, image/price coverage, retailer IDs, elapsed time, and matching product/retailer IDs). The browser branch uses `auto` cache-first behavior. Direct HTML does not establish delivery location, does not create selection tokens, and never supplies cart quotes. Normal search behavior is unchanged. An explicit screening response stops the comparison.


## v0.5 migration safety

Normal search remains browser/cache-first. Anonymous HTTP is discovery-only and cannot establish local delivery context. Diagnostic `browser_mode` explicitly selects `auto`, `refresh`, or `cache_only`; frontend comparison requests `refresh`. Inspect `summary.comparability`, `summary.validation.gates`, source/cache status, coverage, and typed errors before comparing latency or prices.

Catalog observations now retain opaque address-context identity. Legacy/contextless or incompatible observations cannot issue selection quotes. Carts spanning different location contexts return `409 mixed_location_context`. Exact persistent cache reads warm memory without renewing expiry.

Authenticated operators can recover a login-paused discovery worker with one bounded read probe against the same ZIP/query/retailer that failed. Run `python operator_recover.py --acknowledge-provider-access` inside the service shell. The command reads the server-local credential without printing it. Legacy pauses without retained job scope return recovery_context_required and require authorized session setup. An unrelated successful search cannot clear the circuit. Screening pauses remain stopped; successful public reads do not authenticate a customer or establish delivery eligibility.


### Discovery isolation and provider handoff

A login-required ingestion attempt suspends its ZIP + retailer discovery context across queries. It does not stop other eligible contexts or other providers. The suspension persists across restart and new demand does not clear it. Screening still stops the entire Instacart browser worker. Unknown legacy pauses remain stopped unless an exact failed context can be reconstructed from retained evidence.

Ingestion status includes `blocked_contexts`, `blocked_jobs`, `eligible_jobs` and `work_state` (`scheduled`, `fetching`, `no_eligible_jobs`, `provider_paused`, `stopped`). `running` does not mean every queued job or price is fresh. Operator recovery probes one suspended context's original query and clears only that suspension on success.

For local basket handoff, use each selected item's HTTPS `url`. The tester labels these links **Open on Instacart**. No provider basket transfer or checkout session is created; the customer continues on the provider website.


### v0.6 cancellable browser search

`POST /api/v1/search/jobs` accepts the standard Instacart SearchRequest and returns HTTP 202 with `job_id`, `job_token`, `status=queued`, and `expires_in`. Keep the token in memory. Send `Authorization: Bearer <job_token>` to `GET /api/v1/search/jobs/{id}` and `POST /api/v1/search/jobs/{id}/cancel` with `{}`. Tokens are never placed in URLs. Invalid tokens return 403. Jobs are retained for ten minutes; at most two active and 32 retained jobs are allowed.

Poll status: queued, running, succeeded, failed or cancelled. On succeeded, `result` is the standard search response including `search_id`, so normal selection/cart endpoints apply. On failed/cancelled inspect `error.code` and `error.message`. Cancellation is acknowledged only when terminal status is cancelled. Browser execution is isolated in a child process with a shared deadline; cancellation terminates and reaps its running process tree on Linux. The regular synchronous search API remains supported. A client disconnect alone is not an acknowledged cancellation; clients should call the cancel endpoint. The tester attempts cancellation when leaving the page.

Location responses now carry `provider_context` with request context identity, observed ZIP, acknowledgment, nullable store branch ID, delivery verification and authorized-state configuration. A configured session is not proof of authenticated access or local delivery. Anonymous HTTP remains discovery-only.

Optional authorized provider state: operator supplies `INSTACART_STORAGE_STATE_PATH` pointing to a private owner-readable 0600/0400 Instacart-only Playwright state file. No public endpoint accepts session cookies or credentials. Session changes isolate exact caches and address-specific offers. No session is created by this release. Invalid configuration is reported explicitly. See https://playwright.dev/python/docs/auth .

Pagination follows visible retailer carousel / navigation Next page controls, with four actions by default (`INSTACART_PAGE_ACTION_LIMIT`, bounded 0–12), 500 cards and the request deadline. Diagnostics report actions, new-card counts and stopping reason. These bounded observations do not establish complete inventory. Product fields add `price_basis`, nullable `unit_price`, `promotion_text`, `size_status` and `variants_status`; absent variants remain not_exposed.

Storage v6 keeps normalized product, offer, context and scheduler-job tables with an additive native backup before migration. Legacy rows/snapshots are retained for rollback. API, comparisons and ingestion use the shared search resolver. Hard process cancellation is Linux-specific.


## v0.7: browsing and demand-based refresh

The existing search, selection and cart models remain compatible. An optional server-side freshness policy gives each new Instacart snapshot its own five-minute expiry; explicit retailer/ZIP invalidation still expires matching snapshots and quotes. Other providers retain their existing policy.

For a fast browsing screen, first call GET /api/v1/catalog with ZIP/query/provider and display names/images. `last_seen_price` and `price_status` describe a historical observation. Stale or context-incompatible observations must not be selectable. For an address search, a ZIP catalog preview is not proof of delivery or the selected address. Then use the normal search or cancellable job flow to obtain fresh compatible quotes. Never fall back to expired prices for cart validation.

Recent successful auto/refresh searches and current basket selections can enter a bounded demand refresh set. Capacity is computed from measured fetch time and reserves foreground/discovery work. `cache_only` performs no upstream fetch and does not schedule one. No refresh timing or availability is guaranteed across provider failures, login restrictions or overloaded capacity.

Background refresh does not extend or replace an already issued `search_id`. After expiry, call search again and use the new compatible item/search IDs for selection/cart. Existing HTTP 409 `price_expired` remains authoritative. Precise refresh contexts are held only in bounded memory for 15 minutes of inactivity and are lost on restart.

## v0.8.0 local observations and savings acceptance

`GET /api/v1/catalog?zip=63129` excludes unacknowledged or mismatched-ZIP observations by default. `allow_unverified_location=true` explicitly requests historical/discovery observations; those cannot issue local catalog quotes. `context_compatible` now requires observed ZIP acknowledgment, not merely the same requested cache key. Existing rows are retained for reference.

Every newly parsed Instacart product includes `price_basis_verified`, `price_unit`, `price_label`, and `price_unit_evidence`. `per_item` requires an explicit primary `each`/`ea` label or the same product card’s exact `Add 1 ct [product name]` quantity-control label. The latter is returned as `quantity_label` with `price_unit_evidence=quantity_control_label`; the control is read, never clicked. A secondary $/oz label does not establish the main charge basis. Missing basis stays `unspecified`; never assume “each” from a package size. Weighted observations cannot be compared using integer item counts.

Fresh search items also expose `price_fresh`, `price_expires_in`, `age_seconds`, `requested_zip`, `observed_zip`, `location_verified`, `context_id`, `local_offer_eligible`, and `discovery_only`. Old stored observations do not gain unit evidence on upgrade. ZIP acknowledgment does not verify a merchant branch or delivery serviceability.

`POST /api/v1/savings` accepts only server-issued references:

```json
{"offers":[{"search_id":"fresh-search","item_id":"merchant-a-item"},{"search_id":"fresh-search","item_id":"merchant-b-item"}],"quantity":2}
```

It recomputes merchandise subtotals and savings from server-held prices. It requires two distinct merchants, identical provider product ID/name/size, the same acknowledged opaque location context, explicit matching item charge units, USD, no unvalidated promotions, and unexpired searches. `409` errors include `price_expired`, `location_unverified`, `mixed_location_context`, `product_mismatch`, `price_unit_unverified`, `price_unit_mismatch`, `weighted_quantity_required`, and `conditional_price`. Client-supplied prices are rejected. Revalidate after input changes and at displayed expiry. Taxes, fees, checkout price and delivery coverage remain outside this calculation.

### Android acceptance still required

On the installed Android build, use the actual delivery address in 63129, run a cancellable fresh search, and retain request IDs. Find the same packaged product at two merchants with explicit primary charge-unit evidence. Call `/api/v1/savings`, compare the displayed quantity/subtotals/savings against the returned decimals, change quantity, and verify recalculation. Wait until the earliest quote expires and verify the savings claim is removed until refresh succeeds. Repeat with a mismatched-ZIP observation, unknown price unit and different package size: none may create a savings claim. Record build number, merchant/product IDs, observed ZIP, price labels, expiry, API result and screenshots. API fixture tests alone do not satisfy this device acceptance.


## v0.8.2: grocery observations and targeted restaurant menus

### Rollout and existing flows

The X-Byte grocery and restaurant capabilities use server-side rollout controls and credentials. They remain off by default. Existing providers remain available. Grocery uses the existing search, catalog, comparison, selection and basket contracts; it does not introduce a new Android provider selector. When enabled and configured, the grocery demo path requires a specific retailer slug and a structured street address in `location.address`. ZIP-only and other existing search contexts retain their established path. Do not silently substitute a different address or retailer.

Keep vendor pilot checks and the existing Android search/basket regression as separate acceptance exercises. The examples in this section use fictional fixture references and ZIP `00000`; replace them only with values from an authorized runtime search.

### Nullable decoding, source evidence and selection

`price`, `currency`, `url`, `availability`, `price_label`, `size`, `brand`, `pack_count`, `provider_product_id`, `upc` and `provider_location_id` can be null. `unit_price` is a nullable object with amount/unit and optional currency/source. Show missing prices and links explicitly. Disable selection when `selectable=false`; a successful search or HTTP 200 is not permission to calculate a basket from every returned item.

Preserve the following metadata through display, local basket references and diagnostics:

| Field | Meaning |
| --- | --- |
| `id`, `identity_kind` | Opaque observation identity; not a stable SKU/UPC or proof of equivalence |
| `provider`, `data_provider`, `platform`, `source` | Existing provider identity, supplier, marketplace and connector provenance |
| `price_source` | Demo grocery is `instacart_marketplace`; it is not a direct retailer price |
| `brand`, `size`, `pack_count`, `provider_product_id`, `upc` | Evidence needed to compare like-for-like offers; missing fields stay unknown |
| `store`, `provider_location_id`, `observed_zip`, `location_verified` | Store/location evidence; ZIP acknowledgment does not prove branch or delivery serviceability |
| `price_label`, `price_kind`, `price_basis`, `price_basis_verified`, `unit_price_label` | Original amount/qualifier, charge basis and unit-price evidence |
| `captured_at`, `source_expires_at`, `source_freshness` | Explicit source observation validity; nullable timestamps require timezone information |
| `retrieved_at`, `age_seconds`, `cache_expires_in` | Receipt/local-cache timing; separate from source validity |
| `availability`, `availability_captured_at`, `availability_verified` | Supplied stock label and its evidence, without an invented stock-freshness claim |

X-Byte `price_status` is `capture_time_unknown`, `stale_snapshot` or `snapshot`; the existing browser path retains `fresh`/`expired`. `snapshot` means the source evidence is currently valid, while cache TTL and selection requirements still apply. `price_fresh=true` requires valid source capture/expiry evidence and positive cache lifetime when present. `price_expires_in` is zero for unknown/stale source validity. Vendor request time and receipt time must not be substituted for capture time. A scheduled snapshot does not establish a refresh timezone, caching rights, refresh guarantees or live stock.

The API rejects ineligible selections and totals with typed 409 errors such as `source_freshness_unknown`, `source_stale`, `offer_unselectable`, `price_unavailable`, `price_unit_unverified`, `weighted_quantity_required` and `conditional_price`. Keep an estimated or starting-at amount visible as an observation without treating it as an exact basket price. Re-run search and require reconfirmation when a quote expires; detail retrieval does not update a quote.

For savings, X-Byte offers require a shared supplier product ID or UPC plus matching brand, package size, explicit pack count, platform and price source. A lexical match or equal display name cannot establish equivalence. Existing same-context, fresh-quote and charge-unit checks still apply. Marketplace and direct-retailer observations must stay separate.

### Product details and merchant navigation

```http
POST /api/v1/products/details
Content-Type: application/json
```

```json
{"search_id":"fixture-search-id","item_id":"fixture-product-id"}
```

The server resolves the original item and saved address context. The request accepts no arbitrary merchant URL, client price or vendor credential. An unavailable search returns `409 search_expired`. An X-Byte result without a usable vendor product URL returns:

```json
{
  "status":"product_link_unavailable",
  "error":"product_link_unavailable",
  "message":"X-Byte search did not supply a usable product URL. Product details cannot be fetched.",
  "items":[]
}
```

That response is HTTP 409. Do not invent a URL from the product name or observation ID. A successful details response returns `status=ok`, `item`, `search_item_id`, `quote_updated=false`, `cart_updated=false`, `provider_cart_updated=false` and `handoff`. The details item is an observation and remains unselectable; search again to replace the original quote. For another existing provider, `status=merchant_link_only` returns the known item and handoff without a new vendor details fetch. An upstream empty matching result can return the typed HTTP 200 `no_match` envelope.

`handoff.kind` is `merchant_link` or `unavailable`, `handoff.url` is nullable, and `handoff.cart_transfer_supported=false`. Opening a merchant link navigates to that merchant. It does not transfer the local basket, create a checkout session or place an order.

### Restaurant discovery and menu sequence

This implementation supports **Yelp only**. DoorDash, Uber Eats and other platforms return `422 unsupported_platform` until separately implemented. Do not present them as connected or merge offers across platforms.

1. Discover candidates by keyword, ZIP, platform and offset:

   ```json
   {"zip":"00000","query":"pizza","platform":"yelp","page_no":0}
   ```

   Send this to `POST /api/v1/restaurants/search`. Offsets are 0, 10, 20, and so on up to 1000. Keep the returned `discovery_id` and each returned `restaurant_url`. Discovery candidates alone do not establish a menu-item match, price or location coverage. `has_more=null` means unknown; `next_page_no` is an offset to try, not proof of another page. `status=no_match` is an empty candidate result.

2. Fetch the menu only for a relevant candidate from that discovery:

   ```json
   {
     "discovery_id":"fixture-discovery-id",
     "restaurant_url":"https://www.yelp.com/biz/fixture-pizzeria",
     "platform":"yelp",
     "terms":["pizza"],
     "budget":15,
     "budget_operator":"lt"
   }
   ```

   Send this to `POST /api/v1/restaurants/menu`. The discovery lifetime is 300 seconds and is lost on restart. A changed URL/platform returns `409 discovery_mismatch`; an expired discovery returns `409 discovery_expired`. A candidate explicitly reporting no menu returns `409 menu_unavailable`. Terms are optional, at most five distinct nonempty strings of up to 120 characters. The budget is optional, positive USD, at most 10000 with two decimal places; numeric or decimal-string input is accepted.

The menu result preserves restaurant name/location, menu item/description, listed price text, price type, platform, and validated menu link where available. Missing prices and order links remain null; missing stock and source capture evidence remain unknown. `cross_platform_matching_supported=false` and `cart_transfer_supported=false` are explicit.

### Budget and required-modifier semantics

The default `budget_operator=lt` means **strictly under** the limit; `lte` means **at most**. Use `lt` for “cheeseburger and fries under $15.” An exact $15 subtotal fails the strict-under comparison.

The API uses decimal arithmetic and identifies its selected item IDs. A multi-term assessment selects the lowest known listed base price for each requested term; this is not a full-order optimization. Words in a description may establish relevance but do not prove an included order component. A positive full-order budget claim is never produced by this pilot because taxes, fees, availability and final checkout price are unverified.

- `listed_subtotal` requires exact listed amounts for the selected components. `minimum_listed_subtotal` may describe starting-at lower bounds.
- `required_modifiers=null` and `modifiers_complete=false` mean the modifier evidence is missing. Do not equate them with no paid modifiers.
- Supplied modifiers are summed only when completeness, all prices, and verified additive semantics support that sum. A `one_of` list or unknown semantics does not establish an additive charge total.
- `modifier_component_overlaps` identifies a required modifier that may overlap a separately requested item; do not double-count it or claim a lower bound without resolving the overlap.
- `order_within_budget` remains null when uncertain, or false when an established lower bound already violates the budget. `status` is `uncertain` or `over_budget`; `listed_subtotal_within_budget=true` alone is not a completed-order claim.

Show the returned `reason_codes` near the amount. Current reasons include missing listed price, starting-at price, unmatched requested items, unknown required modifiers, unverified modifier selection, component overlap and unknown taxes/fees. With a budget but no terms, each menu item has an individual assessment and the top-level assessment is null.

### X-Byte errors, retries and acceptance

| HTTP | Code | Client behavior |
| --- | --- | --- |
| 200 | `no_match` | Show no accepted match; do not substitute unrelated recommendations or a $0 item |
| 409 | `product_link_unavailable`, `discovery_expired`, `discovery_mismatch`, `menu_unavailable` | Show the specific limitation or repeat discovery/search as appropriate |
| 422 | `no_coverage`, `location_required`, `location_unsupported`, `unsupported_platform`, `invalid_request` | Correct input or show unsupported coverage; a format failure is not proof of local stock absence |
| 429 | `provider_quota`, `rate_limited` | Stop tight retries and preserve the current observations with their age |
| 502 | `provider_auth`, `provider_error`, `provider_unavailable`, `invalid_response`, `identity_mismatch` | Show provider failure; authentication/identity failures require investigation |
| 503 | `provider_disabled`, `not_configured`, `budget_exhausted` | Capability is unavailable; client retries cannot enable it or purchase quota |
| 504 | `provider_timeout` | Show a bounded timeout; do not start overlapping requests |

The server evaluates HTTP transport status and nested response errors, applies bounded retries and returns a sanitized `X-Request-ID` header. Capture that header for support. Vendor credentials and full vendor request URLs must never be sent by Android or copied into diagnostics.

Fixture tests establish API behavior only. Before enabling this path for the installed Android client, verify nullable DTO decoding, observation/source labels, disabled selection, detail missing-link handling, reference-based basket arithmetic, expiry, and merchant navigation on that build. Run the existing Android search/basket regression separately; a fixture does not establish device acceptance or cart transfer.

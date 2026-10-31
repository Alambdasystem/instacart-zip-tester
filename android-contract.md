# InstaSearch API Contract v0.3.0

Base URL: https://alambda-grocery-api.onrender.com/
Interactive documentation: /docs
Machine-readable source of truth: /openapi.json (OpenAPI 3.1)
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
- Product.price: JSON number, USD unit price. Convert to a decimal type for money calculations; never trust client-computed totals for checkout.
- Product.price_fresh, price_expires_in, age_seconds, last_seen and selectable are catalog fields. last_seen is a UTC ISO 8601 timestamp. Live products do not include those fields; use the response expires_in and location.verified instead.
- Catalog: only selectable=true items can be submitted using the returned search_id. Stale metadata may remain visible, but stale prices aren't valid for cart validation. A catalog search_id with zero selectable items doesn't create a usable selection snapshot.
- Live response: status=ok, search_id, items, location, cache, expires_in, cart_updated=false. Catalog response: status=catalog, search_id, items, location, cache, warnings, discovery, catalog.
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

Price freshness is at most 300 seconds, non-sliding. Return cache.hit, partial_hit, hit_queries, fetched_queries, age_seconds and expires_in when present. Re-reading doesn't extend freshness. New ZIP/term payloads can enqueue collector jobs within bounded capacity; enqueue isn't a promise of fresh or geographically verified data. Background refresh only runs while this free service is awake. Catalog and snapshots aren't durable across deployment/restart on the current free setup.

POST /api/v1/cache/invalidate is an operator action, not a normal Android search step. It accepts a retailer slug or * plus optional ZIP and expires related price snapshots. GET /api/v1/ingestion/status and /api/v1/catalog/export are diagnostics.

## Authentication and provider scope

This is an unauthenticated prototype; don't embed an invented API key or send customer credentials/payment details. Production integration needs an agreed authenticated gateway and durable storage. Current deployed connector is Instacart only; no MealMe provider selector or normalized comparison endpoint is claimed by this contract. `source` identifies the existing connector, not a provider choice request.

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

Authenticated operators can recover a login-paused discovery worker with one bounded read probe. Run `python operator_recover.py --acknowledge-provider-access` inside the service shell. The command reads the server-local credential without printing it. Screening pauses remain stopped; successful public reads do not authenticate a customer or establish delivery eligibility.

# Alambda Grocery ZIP Lab

The current Docker app provides a grocery search API, an ingested catalog, a bounded refresh queue, image thumbnails, and a local app cart. The original server.py is retained as a legacy keyless link tester.

## Run

Python 3.11+; run `python server.py`, then open http://localhost:3000.

Server environment variables:
- `INSTACART_API_KEY`: approved Instacart Developer Platform key. Never put this in frontend code.
- `INSTACART_ENV`: `development` (default) or `production`. Must match key.
- `TESTER_ACCESS_TOKEN`: random private token required for API actions. Enter the same token in the browser. Not stored by the frontend.
- `PORT`: supplied by Render, defaults to 3000 locally.

## Deploy on Render

Push these files to a private GitHub repository and create a Python Web Service connected to it. Build command: `python --version`. Start command: `python server.py`. Health check: `/health`. Choose the free plan if available. Set the three variables above through Render's environment settings. No database or packages are required.

## Actual API behavior

`GET /idp/v1/retailers?postal_code=63129&country_code=US` finds nearby retailer organizations, not exact branches or product inventory. `POST /idp/v1/products/products_link` creates one shoppable list from food names. List URLs do not embed ZIP codes; users confirm delivery address and retailer on Instacart. The tester does not claim per-ZIP product availability, live prices, McDonald's support, or a prefilled cart. Website search links are experimental and use the customer's Instacart session location.

On October 30, 2026, Instacart's public developer application page said applications were closed and no waitlist was available. Existing approved keys can be configured when available.

API reference: https://docs.instacart.com/developer_platform_api/

## Middleware API and app cart (v0.1)

The Docker service runs `api_server.py`, serving the API tester at `/` and `/docs`, with a machine-readable contract at `/openapi.json`.

- `POST /api/v1/search`: `{ "zip": "63129", "queries": ["milk"], "retailer": "aldi", "allow_unverified_location": false }`. Accepts up to three queries. Returns normalized items, search ID, timestamp, location verification, and warnings. Explicitly enabling unverified location permits nonlocal results; no login or screening bypass is implemented.
- `POST /api/v1/select`: `{ "search_id": "returned ID", "item_ids": ["returned item ID"] }`. Validates selected IDs against server-held search results.
- `POST /api/v1/cart`: `{ "lines": [{ "search_id": "returned ID", "item_id": "returned item ID", "quantity": 2 }] }`. Validates quantities and prices against recent searches and calculates subtotal. Expired prices return HTTP 409.
- `GET /health`: process health.

`CONNECTOR_ENABLED=true` enables the browser connector. The official Playwright Python image and package are pinned to matching versions. One concurrent search and three searches per minute per client are allowed. Results expire after five minutes and are lost when the free instance restarts. App cart contents persist in browser localStorage; they must be refreshed and revalidated after server-side price expiry. Neither selection nor cart validation writes to Instacart's basket or places an order. Provider failures never return saved sample products as live results.

Run tests with `python -m unittest test_api -v`. Unit tests mock the provider and verify normalization, invalid input, location failure, selection integrity, quantities, subtotal, expiry, and the OpenAPI route. They do not prove that Instacart will accept the deployed browser.

### Five-minute smart cache

Successful searches are cached in server memory for at most 300 seconds. Keys include provider context (Instacart), requested ZIP, individual normalized query, retailer, and whether unverified location was explicitly allowed. Cache hits do not extend expiry. Changing a list from milk to milk plus eggs fetches only eggs; milk is reused while fresh. A store/location group's five-minute clock begins at its first fetch; newer products from that store expire with that clock. A cross-store query is also invalidated when a participating store expires. Product and cart price snapshots never outlive the source cache entry. No provider failures are cached as successful results.

Responses include `cache.hit`, `cache.age_seconds`, `cache.expires_in`, and `cache.ttl_seconds`. `POST /api/v1/cache/invalidate` accepts `{"retailer":"aldi","zip":"63129"}` to expire matching bundles and cart validation snapshots, or retailer `*` for every retailer. This prototype rate-limits invalidation to once per group per minute. The cache is bounded to 100 bundles, one provider browser operates at a time, and the search handler does not poll. The optional ingestion worker separately refreshes its queue. Memory cache is lost on service sleep or redeployment; a future multi-instance version needs a shared Redis/Valkey store. Restaurant checkout is not implemented. The separate Vercel comparison adapter can sample restaurant catalog reads when configured.

## Trial catalog ingestion loop

Set `INGEST_ENABLED=true` on the Docker API service to start one embedded background queue worker. Its curated starter terms are milk, eggs, bread, chicken, bananas, rice, cheese, coffee, apples, potatoes, pasta, and yogurt. These are common grocery categories, not a measured ranking or an exhaustive catalog. `INGEST_ZIP` defaults to 63129; the collector explicitly marks unverified location data. No additional paid service is required for this bounded trial.

Each successful job upserts normalized products in `/tmp/alambda-grocery-catalog.sqlite`, warms the shared query cache, and schedules its next attempt four minutes later. The loop waits 15 seconds between jobs, shares the single browser semaphore with foreground calls, and caps attempts at 120/hour. Failures back off up to 30 minutes. Login or screening pauses the entire worker. Queue throughput may be too low to refresh all categories within five minutes; expired prices remain visible as stale and cannot be selected.

- `GET /api/v1/ingestion/status`: active query, queue state, attempts, errors, product counts, memory high-water marks, and disk footprint.
- `GET /api/v1/catalog?queries=milk&queries=eggs&zip=63129&allow_unverified_location=true`: search stored data without opening another browser. Returns fresh/expired price flags and a selection ID for permitted fresh items.
- `GET /api/v1/catalog/export`: export up to 5,000 normalized product snapshots.

SQLite persists across process reopenings but this free instance's filesystem is ephemeral: catalog data and queue state may be lost on deploy, restart, or sleep. The worker does not keep the service artificially awake. A paid always-on worker and durable storage are a separate upgrade. No automatic paid upgrades are configured. Store-wide cache invalidation also expires stored price snapshots.

## Demand-driven ZIP and product footprint

Incoming catalog and live-search payloads register `(requested ZIP, retailer, normalized query)` jobs. A new ZIP starts with its requested terms plus milk, eggs, and bread. The starter ZIP has twelve curated common queries. Repeat requests increment demand counters without duplicating jobs. Scheduling prioritizes never-fetched and least-recently-successful work, with demand breaking ties. Trial limits: five requested ZIPs, sixty jobs total, twenty queries per ZIP/retailer, one browser, 120 attempts/hour. Capacity rejections appear in discovery responses and limits appear in ingestion status. New queries are added only to their requested context rather than multiplying across every ZIP.

The connector attempts the visible ZIP selector when possible. If a street address or login is required, unverified records remain explicitly labeled and are not proof of local inventory coverage. A larger list of requested ZIPs is a discovery footprint, not a verified store-location footprint.

Product responses now carry `image_url` when a supported HTTPS Instacart/CDN image is present. The catalog retains the URL with the product. The UI loads thumbnails lazily, uses no referrer, and shows an unavailable-image label for missing or failed images. Worker browsers skip image downloads to save resources while still capturing the image URL from rendered markup.

## InstaSearch API Contract v0.2

Interactive Swagger UI is served at `/docs` and `/swagger`. `/openapi.json` defines reusable request/response models and examples. `/android-contract.md` explains the Android integration sequence, nullable images, location verification, cache expiry, errors, retry policy and prototype boundaries. Swagger UI assets are pinned to 5.17.14 and loaded from unpkg.

Run contract checks with `pip install jsonschema openapi-spec-validator`, then `CATALOG_PATH=/tmp/instasearch-tests.sqlite python -m unittest test_api test_cache test_ingestion test_contract -v`. Use an isolated catalog path so local stale records cannot affect fixtures.

## MealMe / Satsuma comparison (v0.2)

The API tester now compares the existing Instacart browser connector with the
account-enabled MealMe API at `https://api.satsuma.ai`. This is NOT the legacy
MealMe v3 contract or the Vercel Kroger/Square proxy.

- `POST /api/v1/compare`: `{"zip":"63110","queries":["milk"],"providers":["instacart","mealme_satsuma"],"mode":"live","allow_unverified_location":true}`.
  Use `mode: "catalog"` (default) to inspect stored snapshots without provider calls.
  Each provider has its own status/error, product/image/price-field counts, freshness,
  and latency. HTTP 200 represents a completed comparison, not success of every source.
- `GET /api/v1/providers/status`: bounded, sanitized activity and local call accounting.
- `/api/v1/catalog` and `/api/v1/catalog/export` accept `provider=instacart` (default),
  `mealme_satsuma`, or `all`. Stored products are namespaced by provider and location.
- `SATSUMA_API_KEY`: secret configured on Render only; raw `Authorization` header,
  fixed HTTPS Satsuma destination, redirects disabled, no write/order endpoints.
- `SATSUMA_READ_BUDGET`: maximum product reads per UTC month in the local database,
  default 10, capped at 100. The cap is local, NOT the account's authoritative meter;
  it resets if ephemeral storage is lost. Five-minute cooldown after upstream failures.
- MealMe is on-demand only while availability remains unresolved; it is NOT added to
  the continuous Instacart ingestion loop. Successful reads populate a separate
  five-minute memory cache and the shared SQLite catalog. Errors never become empty
  successful catalog entries. Existing good records are retained and age normally.
- ZIP centroids come from the public `https://api.zippopotam.us/us/{zip}` response.
  They are approximate search coordinates, not verified delivery addresses. No account
  credential is transmitted to that service. Missing ZIP coordinates are explicit errors.
- Raw MealMe price fields/currency are retained, but units have not been verified with
  usable live products. They are not converted into checkout prices or admitted to the
  app cart. Comparison counts price fields, not matched/equivalent product prices.
- Existing SQLite rows migrate with provider `instacart`; identities are preserved.
  The existing free Render service has NO durable disk: cache, history and budget
  counters can be lost on deployment. No paid storage was provisioned by this change.

Run `python -m unittest -v` (contract tests require `jsonschema` and
`openapi-spec-validator`). Tests use simulated provider responses; they are not
proof of live MealMe products. The live portal audit on October 30 returned
`/merchant` 200 (GoPuff only), `/location` 503 and `/product` 503. Current portal
billing showed Free and 17/100 used after that three-call audit.


## Vercel proxy comparison (not Satsuma)

`vercel_kroger` and `vercel_square` are separate comparison sources at
`https://mealmeai.vercel.app`. They use Bearer authentication and **must not**
receive `SATSUMA_API_KEY`. The existing Instacart ingestion loop is unchanged.

Set `VERCEL_MEALME_API_KEY` only in the server secret environment. Set
`VERCEL_MEALME_READ_BUDGET` to an explicitly approved number of attempts (0–100;
default **0**). Both proxy sources share this budget, while Satsuma accounting
remains separate. The portal currently requires billing activation before key
creation and lists $0.10 per successful call. Do not activate billing implicitly.
The local budget is a trial guard, not a durable billing limit: SQLite is ephemeral
on the current free Render instance and counters reset if its data is lost.

`POST /api/v1/compare` accepts up to four provider names:
`instacart`, `mealme_satsuma`, `vercel_kroger`, `vercel_square`.
Example: `{"zip":"63110","queries":["milk"],"mode":"live","providers":["vercel_kroger"]}`.
Use `mode: "catalog"` for stored snapshots without upstream calls. Clear the
Instacart retailer filter when comparing other sources.

Each proxy query makes one store search, then at most two selected-store catalog
reads (maximum 3 requests/query/source, 18 for three queries across both sources).
Kroger radius is 25 miles; Square is 5 miles. This is a bounded sample, not complete
coverage, GPS, or delivery serviceability. The adapter uses the documented
`radiusInMiles`, `zipCode`, `providerId`, and returned `providerLocationId` fields.
IDs are namespaced by source/store/item. Caches are independent and expire in five
minutes. Failures trigger a shared five-minute proxy cooldown; partial failures
are not cached as successful responses. Successfully discovered stores remain
visible even if catalog reads fail. No raw provider payloads/tokens are persisted.

Store-search fields are documented; catalog mappings are provisional, covered by
synthetic fixtures, and **not yet verified against this account**. Unexpected
response shapes return an error rather than pretending there are no products.
Prices stay raw/unverified, products are comparison-only, and no carts, OAuth,
checkout, orders, or payments are called. Missing or unsafe image URLs stay absent.

Portal documentation inspected: https://mealmeai.vercel.app/docs
Local tests: `python -m unittest discover -q`.
Live activation requires the account's own key and an approved read budget.


## Current correction release: v0.4
The deployed service now uses paid 2 GB compute and a 1 GB disk at /var/data. Older sections above describe historical releases. Current seeds: 394 curated terms, five requested ZIPs and 5,000 maximum jobs. Local provider budgets persist on disk and are not increased by this release.

Search and comparison auto mode check fresh memory and exact durable snapshots, fetching only missing queries. Cache-only never calls upstream; refresh bypasses fresh caches. Stored /catalog remains discovery search with explicit age/expiry. Address/GPS input is supported by the request contract; each adapter either uses its supported mechanism or returns location_unsupported. No location is considered verified because an address was merely supplied. Instacart browser address selection requires visible acknowledgment; delivery and exact branch are separate unknowns.

Foreground browser work waits ahead of subsequent background jobs and rechecks caches after waiting. Native SQLite pre-v04 backup is created once before schema additions. This on-disk backup is not an offsite disaster recovery policy.

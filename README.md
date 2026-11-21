# Alambda Grocery ZIP Lab

The current Docker app provides a grocery search API, an ingested catalog, a bounded refresh queue, image thumbnails, and a local app cart. The original server.py is retained as a legacy keyless link tester.

## Current provider policy (v0.10.0)

MealMe has been retired from this API. The Satsuma and Vercel Kroger/Square adapters, comparison options, and live calls are removed. Catalog reads and exports exclude their historical records, including when `provider=all` is requested.

| Entry point | Current routing |
| --- | --- |
| `/api/v1/search` and `/api/v1/search/jobs` | X-Byte/FoodSpark first when the server key, retailer slug, and full `location.address` are present. Otherwise the existing Instacart browser handles discovery. An X-Byte error is returned without switching providers. |
| `/api/v1/compare` | Legacy catalog observations. Fresh fetches use the Instacart browser; cached observations retain their actual source, including X-Byte. `providers` defaults to `["instacart"]`; retired providers return HTTP 410 `provider_removed`. |
| `/api/v1/provider-tests/search` and `/api/v1/provider-tests/compare` | Explicit, isolated X-Byte and Northfork reads, plus Actowiz schema fixtures. These tests do not replace the shopping search route or create selectable quotes. Actowiz live keyword search is unsupported. |
| Background ingestion | Existing bounded Instacart browser collection. |

Use `/provider-lab` to select and compare the supported suppliers independently. `GET /api/v1/providers/status` reports normal routing; `GET /api/v1/provider-tests/status` reports test configuration and allowance. Configured access does not prove current upstream success. See [PROVIDER-LAB.md](PROVIDER-LAB.md) for the isolated test contract.

For X-Byte on the normal shopping path, configure `FOODSPARK_API_KEY` on the server and send a retailer slug with `location.postal_code`, `location.country_code`, and `location.address` containing `line1`, `city`, and `region`. ZIP-only or missing-retailer requests use browser discovery. The normal search routes retain their existing `CONNECTOR_ENABLED` gate. Never put a supplier key in Android or the browser.

`GET /api/v1/catalog` and `/api/v1/catalog/export` accept `provider=instacart` (default) or `all`, which means only the supported catalog namespace. The legacy `instacart` namespace can contain FoodSpark observations; inspect `source_provider` (or legacy `source`) and the search response’s `source_providers` rather than treating that namespace as proof of browser origin. X-Byte results may have no product image or product URL. Northfork is not an automatic failover source.

The production Docker service runs `api_server.py` and uses its existing persistent disk. The legacy instructions below describe the original link tester and earlier API versions; `/openapi.json` and the current policy above govern integrations.

## Legacy link tester

Python 3.11+; run `python server.py`, then open http://localhost:3000.

Server environment variables:
- `INSTACART_API_KEY`: approved Instacart Developer Platform key. Never put this in frontend code.
- `INSTACART_ENV`: `development` (default) or `production`. Must match key.
- `TESTER_ACCESS_TOKEN`: random private token required for API actions. Enter the same token in the browser. Not stored by the frontend.
- `PORT`: supplied by Render, defaults to 3000 locally.

## Legacy link-tester deployment

Push these files to a private GitHub repository and create a Python Web Service connected to it. Build command: `python --version`. Start command: `python server.py`. Health check: `/health`. Choose the free plan if available. Set the three variables above through Render's environment settings. No database or packages are required.

## Legacy Instacart link API behavior

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

Responses include `cache.hit`, `cache.age_seconds`, `cache.expires_in`, and `cache.ttl_seconds`. `POST /api/v1/cache/invalidate` accepts `{"retailer":"aldi","zip":"63129"}` to expire matching bundles and cart validation snapshots, or retailer `*` for every retailer. This prototype rate-limits invalidation to once per group per minute. The cache is bounded to 100 bundles, one provider browser operates at a time, and the search handler does not poll. The optional ingestion worker separately refreshes its queue. Memory cache is lost on service sleep or redeployment; a future multi-instance version needs a shared Redis/Valkey store. Restaurant checkout is not implemented. Supplier observations can be compared independently in `/provider-lab`; restaurant checkout is not supplied by these tests.

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

## Legacy catalog comparison

`POST /api/v1/compare` retains the existing observational envelope for the legacy `instacart` catalog namespace. Example: `{"zip":"63129","queries":["milk"],"providers":["instacart"],"mode":"catalog"}`. `catalog` and `cache_only` do not call upstream; `live`/`auto` reuse fresh snapshots and `refresh` requests a fresh browser observation. The shared cache can contain X-Byte results from normal shopping search. Preserve their `source_provider`; the namespace does not imply browser origin. HTTP 200 means the comparison completed; inspect `providers[].status` and `providers[].error` for the source outcome. Comparison items are not selectable shopping quotes.

Use the separate `/api/v1/provider-tests/compare` contract for explicit X-Byte/Northfork supplier comparisons. A legacy comparison request cannot request a fresh fetch from those suppliers or silently fall back to them. Requests naming retired providers return HTTP 410 `provider_removed` before upstream work; catalog and export requests use the same retirement response.

## Persistent storage baseline (v0.4)
The deployed service now uses paid 2 GB compute and a 1 GB disk at /var/data. Older sections above describe historical releases. Current seeds: 394 curated terms, five requested ZIPs and 5,000 maximum jobs. Local provider budgets persist on disk and are not increased by this release.

Search and comparison auto mode check fresh memory and exact durable snapshots, fetching only missing queries. Cache-only never calls upstream; refresh bypasses fresh caches. Stored /catalog remains discovery search with explicit age/expiry. Address/GPS input is supported by the request contract; each adapter either uses its supported mechanism or returns location_unsupported. No location is considered verified because an address was merely supplied. Instacart browser address selection requires visible acknowledgment; delivery and exact branch are separate unknowns.

Foreground browser work waits ahead of subsequent background jobs and rechecks caches after waiting. Native SQLite pre-v04 backup is created once before schema additions. This on-disk backup is not an offsite disaster recovery policy.

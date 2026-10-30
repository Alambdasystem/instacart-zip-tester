# Alambda Grocery ZIP Lab

Small, dependency-free Python web app for Render. Supports batches of up to 10 US ZIP codes, nearby-retailer comparison, food-list creation, standalone experimental product-search links, and JSON export. Mobile-friendly green/white interface.

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

Responses include `cache.hit`, `cache.age_seconds`, `cache.expires_in`, and `cache.ttl_seconds`. `POST /api/v1/cache/invalidate` accepts `{"retailer":"aldi","zip":"63129"}` to expire matching bundles and cart validation snapshots, or retailer `*` for every retailer. This prototype rate-limits invalidation to once per group per minute. The cache is bounded to 100 bundles, one provider browser operates at a time, and no background polling runs. Memory cache is lost on service sleep or redeployment; a future multi-instance version needs a shared Redis/Valkey store. Restaurant support is not implemented, but uses the same retailer/location grouping pattern.

## Trial catalog ingestion loop

Set `INGEST_ENABLED=true` on the Docker API service to start one embedded background queue worker. Its curated starter terms are milk, eggs, bread, chicken, bananas, rice, cheese, and coffee. These are common grocery categories, not a measured ranking or an exhaustive catalog. `INGEST_ZIP` defaults to 63129; the collector explicitly marks unverified location data. No additional paid service is required for this bounded trial.

Each successful job upserts normalized products in `/tmp/alambda-grocery-catalog.sqlite`, warms the shared query cache, and schedules its next attempt four minutes later. The loop waits 15 seconds between jobs, shares the single browser semaphore with foreground calls, and caps attempts at 120/hour. Failures back off up to 30 minutes. Login or screening pauses the entire worker. Queue throughput may be too low to refresh all categories within five minutes; expired prices remain visible as stale and cannot be selected.

- `GET /api/v1/ingestion/status`: active query, queue state, attempts, errors, product counts, memory high-water marks, and disk footprint.
- `GET /api/v1/catalog?queries=milk&queries=eggs&zip=63129&allow_unverified_location=true`: search stored data without opening another browser. Returns fresh/expired price flags and a selection ID for permitted fresh items.
- `GET /api/v1/catalog/export`: export up to 5,000 normalized product snapshots.

SQLite persists across process reopenings but this free instance's filesystem is ephemeral: catalog data and queue state may be lost on deploy, restart, or sleep. The worker does not keep the service artificially awake. A paid always-on worker and durable storage are a separate upgrade. No automatic paid upgrades are configured. Store-wide cache invalidation also expires stored price snapshots.

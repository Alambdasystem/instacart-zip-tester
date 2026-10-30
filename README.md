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

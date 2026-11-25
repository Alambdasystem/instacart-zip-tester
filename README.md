# Alambda Grocery ZIP Lab

The Docker application runs `api_server.py`: a grocery search API, retained catalog, bounded background queue, API workbench and local app cart. The current contract is **v0.11.0**. The original `server.py` remains a separate legacy link tester.

## Neadz integration

- [OpenAPI schema](openapi.json), also hosted at [the service's /openapi.json](https://alambda-grocery-api.onrender.com/openapi.json).
- [Android integration contract](android-contract.md), also hosted at [/android-contract.md](https://alambda-grocery-api.onrender.com/android-contract.md).
- [Neadz 0.10.1 migration handoff](neadz-migration-0.10.1.md): provider removal, precise DTO/error/polling rules, examples and the remaining live acceptance gate.
- [Interactive documentation](https://alambda-grocery-api.onrender.com/docs), [API workbench](https://alambda-grocery-api.onrender.com/) and [isolated supplier lab](https://alambda-grocery-api.onrender.com/provider-lab).

Version 0.12.0 makes Northfork checkout retailer-neutral: discover Northfork-enabled stores by ZIP, resolve against the selected retailer/store, and open that retailer’s handoff URL. The 0.10.1 release filled gaps for existing `partial`/`cache_miss` search results, nullable job results/errors, savings, readiness, routing and ingestion status. Existing wire types are retained. Small runtime fixes accept faithful normalized full-address X-Byte acknowledgments, report shared deadlines as `provider_timeout`, and require literal boolean `acknowledge:true` for operator recovery. These checks do not establish live supplier success.

## Current provider policy

MealMe, Satsuma and the Vercel Kroger/Square adapters were removed in 0.10.0. Their active calls/options are gone. Compare, catalog and export reject explicitly selected retired names with HTTP 410 `provider_removed`; catalog/export exclude historical retired records even for `provider=all`.

| Entry point | Routing |
| --- | --- |
| `/api/v1/search` and `/api/v1/search/jobs` | Fresh fetches use X-Byte/FoodSpark when the server key, retailer slug and full address are present; otherwise Instacart browser discovery. An X-Byte error does not switch providers. Fresh exact snapshots can satisfy the request first. |
| `/api/v1/compare` | Only `providers:["instacart"]` is supported/defaulted. Fresh fetches use the browser; shared cache/catalog observations retain their actual source, including X-Byte. Results are not selectable shopping quotes. |
| `/api/v1/provider-tests/search` and `/compare` | Explicit isolated X-Byte and Northfork reads, plus Actowiz fixtures. These tests do not populate shopping/cart state or become automatic fallback. Actowiz live keyword search is unsupported. |
| `/api/v1/checkout/stores`, `/resolve`, `/handoff` | Northfork checkout-store candidates, product remapping from a live X-Byte search, then a validated HTTPS handoff to the selected Northfork retailer. The API returns the provider redirect without following it or placing an order. |
| Background ingestion | Bounded Instacart browser collection. |

Configure `FOODSPARK_API_KEY` on the server and send a retailer slug plus `location.address` (`line1`, `city`, `region`, optional `line2`) with a five-digit ZIP to select X-Byte for a new normal shopping fetch. ZIP-only or missing-retailer requests use browser discovery. Both shopping routes retain the `CONNECTOR_ENABLED` gate. Supplier secrets do not belong in the APK or browser.

`GET /api/v1/providers/status` reports conditional normal routing. `GET /api/v1/provider-tests/status` reports isolated test configuration and allowance; `live_ready` does not prove current upstream availability. See [PROVIDER-LAB.md](PROVIDER-LAB.md) for that separate contract.

Catalog `provider` accepts `instacart` (default) or `all`, meaning allowed sources only. The legacy `instacart` storage namespace can contain FoodSpark observations. Preserve `item.source_provider` (`xbyte_foodspark` / `instacart_browser`) and search `source_providers`; do not use the namespace alone as a supplier label. FoodSpark may return null image, product URL or availability. Current FoodSpark products lack verified primary charge-unit/product-URL evidence and therefore do not qualify for the existing strict `/savings` comparison.

## Shopping API

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/search` | Synchronous normalized search with fresh quote references; HTTP 200 can be `ok`, `partial` or `cache_miss`. |
| `POST /api/v1/search/jobs` | Cancellable search; returns a short-lived job capability. Only `auto`, `refresh`, `cache_only` modes. |
| `GET /api/v1/search/jobs/{id}` | Poll with the returned bearer token. Failed jobs still return HTTP 200 with a nested typed error. |
| `POST /api/v1/search/jobs/{id}/cancel` | Same bearer token and `{}` body; cancellation is asynchronous. |
| `GET /api/v1/catalog` | Stored discovery preview, with freshness/context flags; no upstream request. |
| `POST /api/v1/select` | Optional selection of 1–60 IDs from one unexpired search; duplicates are deduplicated. |
| `POST /api/v1/cart` | Validate up to 60 local cart lines and integer quantities 1–99 against server-held prices. |
| `POST /api/v1/savings` | Compare exactly two qualifying server-issued offers from distinct merchants. Client prices are rejected. |

Shopping requests require `query` or `queries` (1–3 nonblank terms, at most 120 characters each) and `zip` or `location.postal_code`. If both ZIP forms are supplied they must match. `queries` takes precedence. Full location can also carry coordinates, but provider support must be established; unsupported mechanisms return `location_unsupported`.

`auto` checks fresh exact snapshots and fetches misses. `cache_only` never fetches or schedules refresh. `refresh` requests a fresh snapshot; concurrent refreshes may coalesce. On synchronous `/search`, legacy `live` and `catalog` are cache-first aliases of `auto`. On `/compare`, `catalog` means stored discovery-only. Do not interchange these endpoint-specific meanings.

The local `/api/v1/cart` remains app-owned. Northfork checkout is a separate explicit two-step flow: `/checkout/resolve` validates a live X-Byte selection and maps product names to Northfork IDs for a selected Northfork store; `/checkout/handoff` sends the shopper-selected Northfork IDs/quantities to Northfork and returns Northfork’s retailer-specific cart URL for the chosen store. The app opens that retailer’s cart so the shopper can review and finish checkout there. The browser must open that URL; the shopper signs in and reviews the transferred products. No order is placed by this API. X-Byte's current documented grocery search requires a retailer slug and full address and returns one store context; its ZIP-only grocery store-discovery endpoint is not yet documented. `/checkout/stores` therefore lists Northfork checkout candidates, not X-Byte store discovery. Selection/local-cart responses still do not mutate a provider basket. Savings requires matching product identity/name/size, acknowledged location/context, verified per-item charge basis, USD, no unvalidated promotion and unexpired prices. It covers observed merchandise prices, excluding taxes, fees, delivery and checkout verification.

## Freshness, persistence and location

Price snapshots expire within 300 seconds; reads never slide expiry. Response `expires_in` is the earliest returned snapshot expiry. A partial result identifies uncovered terms. `cache_miss` has no search ID and cannot supply cart/savings references. Existing quote IDs never gain lifetime from background refresh.

The deployed Docker service uses its configured persistent SQLite disk. The path is `CATALOG_PATH` when supplied, otherwise `alambda-grocery-catalog.sqlite` under `RENDER_DISK_MOUNT_PATH` or `/tmp`. Exact snapshots, normalized product/offer/context records and scheduler state can survive restart on durable storage, retaining their expiry. Search quote IDs, jobs/tokens and precise demand-refresh contexts remain process-local. A restart can invalidate references before their displayed expiry.

Catalog prices can remain numeric after expiry as historical observations. Inspect `price_fresh`, `price_status`, `selectable`, context and evidence flags. With a ZIP, unacknowledged/mismatched rows are withheld unless `allow_unverified_location=true` is explicitly requested. A requested ZIP or an address acknowledgment does not verify merchant branch, stock or delivery eligibility. Catalog counts and worker `running:true` are not proofs of local inventory.

## Runtime and operations

The Docker image installs the matching pinned Playwright browser/package and starts `api_server.py`. Relevant settings are `PORT`, `CONNECTOR_ENABLED`, `FOODSPARK_API_KEY`, `CATALOG_PATH`, `RENDER_DISK_MOUNT_PATH`, `INGEST_ENABLED`, `INGEST_ZIP`, `INGEST_RETAILER`, and the optional `INSTACART_FRESHNESS_V07` policy override. Supplier-test enablement/tokens/allowances are separate; see the provider-lab guide. Optional authorized browser state is configured privately with `INSTACART_STORAGE_STATE_PATH`, never through a public cookie upload.

The worker uses curated discovery terms, at most five requested ZIPs/5,000 jobs, one browser and 120 attempts/hour. These are bounded observations, not an exhaustive inventory or measured popularity ranking. Refresh tiers and admitted demand are reported in ingestion status. Login-required contexts are suspended by ZIP/retailer; screening pauses the browser worker. Foreground queue waits are bounded to 15 seconds normally or 65 seconds for coalesced refresh, within a shared 120-second request budget.

- `/health`: process liveness and loaded schema version.
- `/ready`: SQLite readiness, not supplier availability.
- `/api/v1/ingestion/status?offset=0&limit=50`: typed state, queue pagination, nullable errors/pause, resources and catalog counts. `limit` is clamped to 1–100.
- `/api/v1/catalog/export`: up to 5,000 allowed observations, including explicitly marked history.
- `/api/v1/cache/invalidate`: current public prototype maintenance endpoint; retailer slug or `*`, optional ZIP (null/omitted means all ZIPs). Rate-limited once per group/minute.
- `/api/v1/ingestion/recover`: operator bearer credential plus literal `{"acknowledge":true}`. The server-side `operator_recover.py --acknowledge-provider-access` tool reads the credential without printing it and probes the retained failed context.

Normal shopping routes and read-only status endpoints currently require no application bearer token. Job capability auth, operator recovery auth and live supplier-test auth are separate. Avoid embedding any operator or supplier credential in the app.

`/api/v1/diagnostics/http-compare` is an optional transport diagnostic. It always attempts direct anonymous HTTP even with `browser_mode:cache_only`; that setting controls only the browser branch. It cannot issue quotes or alter normal shopping routing. A screening response stops the browser branch. Its typed parity report does not by itself approve a rollout.

## Development checks

Install test dependencies with `pip install jsonschema openapi-spec-validator`. Use an isolated `CATALOG_PATH` so developer data cannot affect fixtures. Run `python -m unittest discover -v`; focused HTTP/schema coverage includes `test_contract`, `test_neadz_schema` and `test_runtime_contract`. The tests mock suppliers and exercise real local handlers, including failure variants. They do not prove upstream access or replace device acceptance on the latest APK.

## Legacy link tester

The original `server.py` is separate from the production Docker API. Run it with Python 3.11+ at `http://localhost:3000`. Its environment uses an approved `INSTACART_API_KEY`, matching `INSTACART_ENV` (`development` by default or `production`), private `TESTER_ACCESS_TOKEN`, and optional `PORT`.

That tester creates Instacart Developer Platform shoppable-list links, not local product quotes or a prefilled provider basket. The customer confirms address and retailer on the provider site. Its API reference is https://docs.instacart.com/developer_platform_api/ . Use the current Docker contract above for Neadz integration.

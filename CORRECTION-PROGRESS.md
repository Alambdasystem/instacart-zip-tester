# v0.4 correction release

Baseline: feac490. Implemented and tested in this release:
- Optional validated address/GPS location context, US ZIP compatibility, conflict and unknown-field rejection.
- HMAC-isolated context keys without raw addresses in cache keys; request-scoped propagation through comparison threads.
- Exact persistent search snapshots beneath the catalog disk, source/age/expiry metadata, provider namespaces and durable invalidation. Fresh reads never renew TTL.
- Auto/cache_only/refresh modes; legacy catalog remains discovery-only. Cached partial results retain missing-query metadata.
- Foreground-priority browser coordination and post-wait cache recheck. One browser remains the resource bound.
- Stable Instacart product identity across price changes.
- ZIP selection attempted independently of fallback setting; optional street address attempted through normal provider controls with visible active-address acknowledgment required. GPS-only Instacart and unverified address/GPS proxy contracts explicitly rejected.
- Satsuma explicit GPS discovery support; ZIP-centroid discovery remains unverified for delivery.
- Queue batch persistence, restored pause state/cursor, future due-time preservation, capacity-safe seed updates.
- Recognized upstream error-code preservation and narrower shared proxy cooldown.
- Updated mobile-compatible location inputs, selected-provider cache-first action, nullable price rendering and context-reset behavior; current deployment copy.
- Additive snapshot table; native pre-v04 SQLite backup created once on disk; bounded persistent snapshots and memory-cache bytes.

80 tests passed before final release verification. Unit tests use synthetic provider fixtures and do not prove delivery availability.

Provider docs checked directly: https://mealmeai.vercel.app/docs confirms GET /api/stores/search and GET /api/catalog/search. 404 can mean restaurant_not_found or product_not_found, not necessarily a missing route. No guessed endpoint changes made.

Remaining plan items must not be represented as complete:
- Separate normalized product/offer/job tables and versioned migration pipeline (existing product catalog and JSON queue retained with fixes).
- Full shared search-service extraction, admin-scoped invalidation, async progress/cancellation endpoints and unified foreground/background budget accounting.
- Complete relevance taxonomy/restaurant-specific discovery routing, robust store pagination and measured provider quality ranking.
- Verified local address/branch/delivery coverage depends on actual provider acknowledgment and authorized session if required. A public test address is not delivery proof.
- Live provider smoke, durable restart proof and 30-minute memory soak must be recorded separately after deployment. No new provider spend or larger budgets are implied.


Second correction pass: thread-safe expiring quote store, conservative query matching, cache key versioning to reject older recommendation snapshots, clearer catalog/stale labels, queue pagination, provider capability registry, readiness and request IDs, restart recovery for interrupted jobs and graceful server shutdown. Public-address smoke returned location_required for both 63129 and 63110; this is an explicit unresolved provider address acknowledgment, not verified local delivery. Live selection and quantity-two local cart arithmetic passed against an unverified-location snapshot. No OOM events were reported in the observed period after the 2 GB upgrade.


Final pass adds durable store epochs, GPS on explicit click, address-save/header hydration polling and a delayed-form regression. 80 deterministic tests pass. Four-source live smoke: Kroger 40205 returned 24 items / 24 images / 16 raw price fields from two stores; Square 45202 discovered two restaurants but menu reads returned 502; Satsuma 63129 returned 503. These are not equivalent-product or delivery-verification claims.


## v0.5 migration correctness release — October 31, 2026

Implemented context-specific persistent offers with additive schema version/backup, separate bounded product descriptions, fail-closed legacy quote eligibility, mixed-context cart rejection, stricter parity gates, heading/srcset and full embedded-item extraction, explicit diagnostic browser modes, computed comparison decisions, structured comparison UI, monotonic stage budgets, narrower auth dialog classification, canonical address bootstrap before visible retailer navigation, L2-to-L1 hydration without TTL extension, and authenticated operator recovery with a bounded public-read probe. Worker status exposes circuit evidence, heartbeat and measured throughput.

Direct local HTTP remains unsupported: no authorized address-context mechanism or complete matching catalog has been proven. It is discovery-only. No HTTP canary is enabled. Screening recovery remains blocked without authorized provider-session setup. Recovery helper runs in the service shell and never prints the local operator credential.

Remaining architectural work from the broader plan: fully normalized offer/job tables, a single extracted search orchestration module, staged same-response browser capture, robust pagination, hard process cancellation on client disconnect, validated HTTP context transport, repeated parity matrix and canary. The monotonic deadlines bound individual stage waits but do not claim hard operating-system cancellation of DNS or all blocking I/O.

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

Remaining architectural work from the broader plan: fully normalized offer/job tables, a single extracted search orchestration module, robust pagination, hard process cancellation on client disconnect, validated HTTP context transport, repeated parity matrix and canary. The monotonic deadlines bound individual stage waits but do not claim hard operating-system cancellation of DNS or all blocking I/O.


### v0.5 live acceptance evidence

Runtime ce61a565 (October 31, 2026): health/readiness 200; unauthenticated recovery 403. Public fixture address 5430 Telegraph Road, St. Louis, MO 63129 returned 229 products, 229 image URLs, 23 retailer listings in 43.064 s. Active address acknowledgment passed, but branch and delivery serviceability remain unverified. Immediate cache-only repeat returned the same 229 products in 2.496 s, memory_cache, zero fetched queries, TTL reduced to 297 s. Selection and quantity-two local cart passed, subtotal $7.38. Three sampled product images fetched HTTP 200 image/jpeg; UI initially loaded seven lazy images with none failed.

Same-response audit: initial HTML had 14 complete items and 909 references (895 without details), initial DOM and address-selected DOM had zero cards, subsequent shelf collection 230 cards / 229 normalized items. Results were partial with pagination available. These measurements demonstrate hydration/lazy-loading, not complete inventory coverage.

Fresh controlled pair: anonymous HTTP 14 products in 2.834 s, unknown ZIP; browser 236 products in 38.078 s with acknowledged 63129. Five overlapping identities, zero equal-price offers. Gate correctly BLOCKED / not_comparable; no canary or production transport switch. The HTTP embedded image template uses {width=} and {height=}; a follow-up normalizes these placeholders and adds a regression.

ALDI-only bootstrap failed retailer_unavailable after 17.803 s because navigation was checked before hydration (address-selected DOM was empty). Follow-up waits up to 15 seconds for the exact visible retailer link rather than scanning the first 300 early anchors. Runtime acceptance pending its deployment.

111 deterministic tests pass with the navigation/image follow-up. Operator recovery and post-deployment ALDI results are recorded in the accompanying migration plan. Full normalized offer/job schema, shared orchestration extraction, robust pagination, hard cancellation, and sustained soak remain open. These are not claimed complete.

Operator recovery succeeded at 07:11:03 UTC: public-read probe returned 218 products, cleared the legacy login circuit and resumed background discovery. This did not import an authenticated session or verify local delivery. Final restart/worker acceptance pending.

Final runtime follow-up 456bf064: ALDI navigation advanced past the missing-link race but encountered interactive login (502/login_required); no retry/bypass. Direct embedded image normalization returned 14/14 image URLs. Persistent-cache restart proof returned 236 items with no fetch, then L1 memory returned the same 236; expiry decreased 18→15 seconds. Worker resumed briefly, then a new milk job required access before location selection and paused again. The loop is NOT healthy or continuously filling: final snapshot 5,000 stored rows, zero fresh prices, 1,582 jobs, four ZIPs, 397 distinct queries.

Recovery tightened after this finding: only the retained paused job's exact ZIP/query/retailer is probed; a legacy pause without scope returns recovery_context_required instead of clearing on an unrelated milk read. New circuits persist scope. Screening still prevents any probe. 111 tests pass including exact recovery fetch arguments and legacy fail-closed behavior. Continuous ingestion now requires authorized provider-session integration; do not repeatedly invoke recovery.


## v0.5.1 discovery isolation and handoff

Login-required work now suspends its exact ZIP/retailer discovery context, including all queries there, while eligible contexts continue. Screening remains a provider-wide stop. Persisted suspensions cannot be cleared by new demand. A legacy login circuit migrates only when it has explicit scope or a unique matching failed job within 120 seconds of its pause timestamp; unscoped/ambiguous pauses stay stopped. Operator recovery probes one original failed query and clears only that suspension.

Status reports blocked contexts/jobs, eligible jobs, and explicit work_state; all-context suspension reports no_eligible_jobs, not a healthy running ingestion feed. The local basket displays validated HTTPS Instacart product links labeled Open on Instacart, with no automatic provider-cart transfer claim. Android contract updated.

120 deterministic tests cover context isolation, continued work elsewhere, persistence, demand not bypassing suspension, exact scoped recovery, unknown global pause and stale legacy evidence. Live deployment/worker results must be distinguished from these fixtures. Broader normalized offer/job storage, shared search orchestration, pagination, hard cancellation, sustained coverage/soak, verified branch/delivery, authorized restricted-provider access and equivalent HTTP transport remain pending.

Live v0.5.1 acceptance: old circuit reconstructed as Schnucks / 63129 and only that context suspended (one queued job). Other work resumed: chicken search completed with 225 products in 43.319 s; a subsequent job produced 386 total fresh discovery-price rows. Exact chicken cache-only read returned 225 items, zero fetched queries, original expiry decreasing. These discovery results observed ZIP 30043 and remain explicitly UNVERIFIED for 63129. API quantity-two local cart $11.98 passed; UI selected a Costco chicken item, validated $6.21 with unverified-location warning and displayed Open on Instacart. Expired prior demo quote returned price_expired.

Follow-up prevents new demand from immediately refetching an exact background snapshot with more than 60 seconds remaining; original TTL remains unchanged and the job is scheduled for the final-minute refresh window. Invalidation still permits fetch; cache read failure defers provider work safely. Cache deferrals do not consume upstream attempt budget.
Rollback note: older worker code ignores blocked_contexts. Preserve suspensions or stop ingestion before rolling back to a version without isolation support.

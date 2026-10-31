# InstaSearch v0.7 freshness rollout

## Scope and compatibility

Keep the existing search/select/cart contracts and the five-minute hard quote limit. Catalog names/images remain browseable after prices expire; stale previews are not selectable. Precise-address refresh demand stays in bounded memory, never the durable ZIP discovery queue. No provider keys, paid resources, hourly limits or login/screening recovery policies change.

The version-controlled `freshness-config.json` switch defaults to false for the first deployment. A subsequent tested commit enables it. `INSTACART_FRESHNESS_V07` is an optional environment override; explicit false immediately selects the legacy behavior on the next deploy. No service environment variables are rewritten. Other provider cache instances remain on the existing policy even if the flag is enabled. New cache keys use v07 and cannot hydrate older v06 snapshots. The database change is an additive `cache_fences` table; normalized offer/job schema and legacy rows remain unchanged.

## Files and functions

| File | Changes | Regression protection |
| --- | --- | --- |
| search_cache.py · key/put/get/invalidate/fence | Independent five-minute snapshot expiry; original expiry survives L2 restart; durable invalidation fence; explicit store invalidation remains | Staggered milk/eggs writes, restart, expiry boundary, unaffected retailer/provider |
| search_service.py · resolve/finish | Coalesce overlapping forced refreshes after lock; leader alone spends live-call budget; capture/check invalidation fence; register successful foreground demand | Concurrent threads share one fetch, invalidation during fetch rejects results, cache-only schedules nothing |
| refresh_planner.py · observe/admitted/pick/completed/failed/promote | Bounded 64-entry RAM demand queue, fifteen-minute inactivity eviction, basket priority, capacity based on measured service time; stagger refresh 210–225 seconds before expiry, allowing for two non-preemptible 90-second read budgets and the tick delay | Private context preserved for provider fetch but absent from durable queue/status; hard quote remains unchanged |
| ingestion.py · Catalog.ingest/search and Ingestor.tick/status | Fence catalog writes; explicit historical price fields; admitted refresh jobs share existing browser/120-attempt hourly budget, foreground lock priority and provider suspensions | Budget/pause tests; urgent refresh precedence; discovery turn when time permits |
| api_server.py · initialization/search/jobs/select/cart | Flag wiring, sanitized status, keep request context only in quote RAM; basket activity promotes exact context; precise-address baskets do not seed ZIP-only discovery | Existing endpoint/quote/expiry/context regression suite runs with flag off and on |
| middleware.html | Catalog preview during live search, last-seen labels, disabled preview selection; fresh results replace preview; hot-refresh status; weighted-price caption | JavaScript syntax plus live browser verification |
| openapi.json / android-contract.md | Additive historical-price/status models and browsing integration guidance; old search IDs still expire | Published examples and HTTP response schema validation |
| Dockerfile | Include refresh_planner.py and freshness-config.json | Verify image starts and health reports code version and flag state |
| test_freshness.py | Eighteen targeted concurrency, invalidation, expiry, private-context and scheduler tests | In addition to the existing regression suite |

## Controlled rollout

1. Compare affected source files to GitHub main; retain an isolated baseline and inspect all diffs.
2. Run the whole suite with the flag false and true, including the published OpenAPI models. Run frontend syntax and whitespace checks.
3. Publish and deploy with the flag still false. Verify health/readiness, schema compatibility, catalog and unaffected providers. Expect the existing persistent-disk deploy interruption; do not call it zero downtime.
4. Enable the non-secret version-controlled switch in a separate commit and deploy it through the existing Render connection. Environment overrides, if present, take precedence. Verify the effective health flag before issuing provider reads.
5. Verify real search, immediate exact cache reuse, stale preview disabled selection, basket validation and explicit cancellation. Admit a recently used context and observe an actual refresh before its previous snapshot expiry, within existing budgets.
6. Record provider failures or access restrictions rather than clearing suspensions. If verification fails, revert freshness-config.json to false and deploy (or use the explicit environment override). Neither rollback nor background refresh renews old client quote IDs.

## Completion limits

An admitted small set is a capacity target, not continuous freshness for all 1,584 discovery jobs. Provider failures and non-preemptible reads can still cause a gap. The remaining queue remains discovery/warm data. A catalog preview is not stock, branch or delivery verification. Restricted-provider access remains a separate authorized integration. Automatic provider basket transfer is out of scope.

## Evidence

Implementation and local test results are recorded during this rollout. Production validation is recorded separately; passing fixtures do not establish real provider coverage.


### First production timing correction

The first enabled-policy trial returned 247 products / 24 retailer listings for acknowledged ZIP 63129 and validated the local basket. It demonstrated a late refresh after a non-preemptible discovery read; the old quote correctly returned 409 price_expired. The 120–135-second lead was insufficient for two 90-second browser budgets plus the tick delay. v0.7.1 increases the lead to 210–225 seconds, conservatively admits demand and preserves discovery turns only when a refresh has enough time. The original five-minute price deadline is unchanged. A second live timing trial is recorded separately; the first trial is not claimed to have passed early refresh.

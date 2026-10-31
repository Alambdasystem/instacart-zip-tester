# Instacart ingestion fixes — v0.3.0

## Findings

The public search page can render several retailer shelves, but the previous connector stopped waiting at the first price. It then took the first twenty products, favoring the earliest shelf. Inspection showed a browser-default ZIP of 67570 alongside Dillons, ALDI, Dollar General and Costco; that is not evidence of those stores serving requested St. Louis ZIPs. Product brand and retailer slug must not be equated with a local branch.

## Implemented changes

- Wait for the active address control to appear; use its ZIP rather than any five-digit text in the header.
- Attempt the normal visible ZIP suggestion flow. A suggestion click alone never confirms location.
- In strict mode, return location_required when the provider cannot confirm the requested ZIP. Never manufacture a street address or silently enable unverified mode.
- In explicit unverified mode, preserve the actual observed provider ZIP, and identify requested ZIPs as catalog partitions rather than coverage.
- Wait at least eight seconds for all-retailer searches (four for explicit retailer searches) and require four stable snapshots. Bound collection to 22 seconds and report unsettled/partial results.
- Recheck active ZIP after every query. Do not inherit verification from a prior query.
- Discover retailer sections from their visible store links. Return retailer ids, names, bounded product counts and branch_verified=false.
- Replace a first-twenty slice with round-robin collection, at most eight products per retailer and eighty per query.
- Accept safe retailer slugs in search/cache invalidation and ingestion queue, so discovered retailers can be selected.
- Filter explicit-retailer results to matching product-link retailer slugs.
- Preserve retailer discovery and per-query diagnostics through live responses, cache hits and provider comparison.
- Populate tester retailer choices from returned discovery metadata and explain geographic scope visibly.
- Add OpenAPI models and integration documentation for diagnostics, retailer discovery and geographic verification scope.

## Remaining boundaries

ZIP confirmation is not proof of a street address, delivery serviceability, an exact store branch, or inventory availability. Those are explicitly false/unverified. Retailer discovery is limited to the sections visible in the current provider context; this is not an exhaustive retailer directory. Collection remains bounded and the service respects sign-in and screening walls. Existing ephemeral storage and five-minute freshness limits remain.

## Validation

Regression tests cover later-loading retailers, fair allocation, cross-retailer identity, empty/incomplete rendering, screening, unrelated-header ZIPs, preservation of a mismatched ZIP, safe retailer slugs and cached diagnostics/geo disagreement. Existing API, cart, cache, ingestion, provider isolation and OpenAPI contract tests run with the new suite. Live rollout checks are recorded separately after deployment.

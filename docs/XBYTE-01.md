# Opt-in FoodSpark adapter

This change hardens the existing address-scoped grocery adapter and adds
separate restaurant discovery and menu reads. All committed fixture data is
synthetic. This repository does not contain live vendor results, private
correspondence, credentials, or internal deployment inventories.

## Rollout

The checked-in `xbyte-config.json` defaults both flags to false. Server-only
overrides are `FOODSPARK_ENABLED` and `FOODSPARK_RESTAURANTS_ENABLED`.
`FOODSPARK_API_KEY` is a server credential. `FOODSPARK_READ_BUDGET` defaults
to zero and is capped at 100 total attempts recorded in the budget database.
Failures and retries count against this application limit. It does not define
the vendor's billing policy. No scheduled collection is enabled.

The existing `CONNECTOR_ENABLED` switch still gates grocery search. When
FoodSpark is not selected, the existing browser search stays available. Once
selected, provider errors are surfaced without silently substituting a source.
The existing Satsuma and Vercel adapters remain available.

## Grocery contract

Grocery search keeps the existing search, selection and local-cart references.
It preserves price/currency, brand, size, pack count, unit price, store identity,
product identity and links when supplied. Observation IDs are not SKUs or UPCs.
The adapter explicitly labels the Instacart marketplace price source.

Provider capture time, validity, availability evidence and local cache lifetime
are distinct. A new retrieval cannot make an old or undated provider snapshot
fresh. Missing prices remain null; estimated and starting-at prices stay
explicit. Local basket validation rejects unsupported or ineligible prices.
Equivalent-offer savings requires actual identity and matching brand, size,
pack count, charge basis, location context, platform and price source.

`POST /api/v1/products/details` accepts only server-held `search_id` and
`item_id`. It requires the product URL returned by search. A missing link
produces `product_link_unavailable` without a provider call. A detail response
does not silently replace a quote or borrow an unverified store branch.
Opening a merchant link never transfers the local cart.

## Restaurant contract

`POST /api/v1/restaurants/search` accepts keyword, ZIP, platform and page
offset. `POST /api/v1/restaurants/menu` accepts a returned URL/platform plus
its bounded, expiring discovery reference. Only the selected restaurant is
fetched. The available adapter contract is Yelp; other platforms explicitly
return `unsupported_platform` until their contracts are implemented.

Menu items preserve listed prices, qualifiers, supplied location, platform,
links and required-modifier evidence. Budget arithmetic uses Decimal. For
example, `terms=["cheeseburger","fries"]`, `budget=15` and
`budget_operator="lt"` means under $15; `lte` means at most $15.
Description-only mentions cannot establish an included component. Ambiguous
modifier choices, overlapping components, absent prices and unknown fees
remain explicit uncertainty. No cross-platform item matching is claimed.

## Transport and validation

Only the four adapter read operations are allowed. Requests reject redirects,
bound response size and retries, and record sanitized timings/request IDs.
Both HTTP status and body-level failures are evaluated, including auth/errors
within HTTP 200. No-match, coverage, stale-source, authentication, quota,
timeout and provider-error outcomes remain distinct.

Cache keys separate browser and FoodSpark observations and credential
rotations. Catalog, comparison and basket reads recheck source evidence.
The public API contract describes nullable observations and eligibility;
clients must honor those fields before pilot enablement.

Install `requirements-dev.txt`, then run:

```bash
python -m unittest test_xbyte_grocery test_xbyte_restaurants test_xbyte_transport test_xbyte_savings -q
python -m unittest test_contract -q
python -m unittest discover -q
```

The fixture suite covers search/details/selection/cart, strict identity,
unknown and stale capture, missing prices, transport/body errors, retry/budget
bounds, query relevance, restaurant pagination and budget uncertainty. It
does not establish live provider coverage or installed-mobile acceptance.

Source contracts, source timestamps/charge units, client compatibility and
commercial terms must be verified before any production enablement.

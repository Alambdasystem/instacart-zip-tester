# Alambda local offer correctness — v0.8.1

## Reproduced before change, November 4, 2026

- Live release was 0.7.1, commit 03f3693. Catalog: 5,000 offers, 10,000 descriptions, 919 unexpired price observations, zero delivery-verified offers, zero acknowledged ZIPs; all observed ZIPs were 30043. Jobs covered 63017, 63110, 63119 and 63129, 399 queries and 1,586 jobs.
- Catalog for requested 63129/milk returned observations from 30043. Ingestion calls use allow_unverified=True for discovery; the provider default can remain unchanged when ZIP suggestions do not acknowledge a delivery location. Stored rows are indexed by requested ZIP but retain the distinct observed ZIP. Same requested key was incorrectly reported context-compatible even for a mismatched observation. This is not evidence of stores or delivery in 63129.
- A cancellable fresh address search failed `busy` after 15 seconds while ingestion held the single browser slot. Measured ingestion mean was 62.95 seconds, so foreground priority applied too late.
- Primary price units were usually unspecified. Previous normalization could assign per_lb from any text, including a secondary comparison rate. Package size does not establish the charge unit.

## Changes

- Local catalog and catalog-comparison defaults withhold unacknowledged/mismatched observations. Explicit unverified discovery remains available, with incompatible/nonselectable flags; rows are retained.
- Catalog context compatibility requires acknowledged observed ZIP and request context. New counts separate fresh ZIP-acknowledged offers from fresh unverified observations.
- Price evidence distinguishes the primary amount/charge unit from secondary unit-price labels. No package-size-to-each inference. New fields are additive; older rows remain unknown until a successful refresh.
- Ingestion opts into cooperative yielding. Foreground contention signals its existing cancellation path, stops/reaps the isolated provider process, releases the browser slot and reschedules the job without increasing its failure count or clearing any provider suspension. No new browser concurrency or budget increase.
- First matching-context page is retained before optional pagination. Optional traversal has an eight-second budget and reserves shared deadline time. Previously collected results survive a traversal deadline; partial coverage remains explicit.
- POST /api/v1/savings computes server-held Decimal merchandise savings only for two unexpired, matching product/name/package/unit/USD offers at distinct merchants in one acknowledged location context. Unknown units, promotions, weighted quantities, mismatches and client-supplied money are rejected. Branch/delivery and checkout remain separate.

## Validation and rollout

- 17 targeted regression tests cover location/reference gates, explicit units, Decimal savings, expiry, product/size/context/merchant/promotion conflicts, background yielding and preservation of the first page after pagination timeout.
- Full regression suite: 170 tests passed for v0.8.0; v0.8.1 adds two targeted cases (172 total). Final result is recorded with the live release evidence after execution; never treat a mocked fixture as a live offer pair.
- Baseline run initially lacked development-only jsonschema dependencies and encountered an intermittent descendant-reaping assertion; these were recorded before editing that path. Subsequent full run with development validators passed. No production dependency added.
- Existing endpoints and five-minute quote TTL retained. Additive storage/metadata only. No source-provider budget, key, session state or resource-plan change.
- Rollback: redeploy previous commit 03f3693. Database rows are not deleted. Note this restores the older reference-return behavior, so consumers must continue treating unverified observations as references.

## Outstanding acceptance

A real provider search must acknowledge 63129 and expose two identical packaged products at different merchants with explicit each/ea or product-scoped Add 1 ct quantity labels before a positive savings demonstration is valid. The Android build must then consume the response, display/recalculate it and remove the claim on expiry. There is no adb, emulator or connected Android device in this execution environment. API fixture success does not close Android acceptance.

## v0.8.0 live retest and v0.8.1 refinement

- v0.8.0 commit 30fa816b2df58be53d448408189d4f03d507d543; Render deploy dep-dbleik59el5s73cqokog live November 4 at 08:14:41 UTC. Health 200/version 0.8.0.
- Strict catalog 63129/milk returned zero wrong-location rows, with 100 excluded observations. Explicit unverified discovery returned 100 reference observations from 30043. Request IDs: 9e4495e8ba464053bb376dfff82324bd and baaedb3acf034cb2a220c5f0477c63f7.
- Fresh 63129 address job reached provider selection and failed location_required at address_input. It did not prove location resolution or a fresh matching pair.
- Public page inspection showed both Enter your address and Street address names on input id streetAddress, and exact product-scoped Add 1 ct controls. v0.8.1 accepts both observed field labels, waits for hydration, reclassifies an auth wall appearing after a click, and captures quantity-control labels without clicking them. Unknown units remain unknown. Conditional promotional labels are excluded from savings.
- Browser inspection is separate from the service browser and is not proof of a successful API or Android comparison.

## Final v0.8.1 live result

Commit 5416d39bddc01a1c8067888f9f0fa5315e9561cb; deployment dep-dblem6gn74is739u6jb0 live at 2026-11-04T08:22:30.273483Z. All 172 tests passed (36.918 seconds). All published files matched tested source hashes.

During an active background job, Render logged ingestion_yielded at 08:23:06.584614127 UTC. The foreground fresh address job succeeded in 49.083 seconds. Requested and observed ZIP both 63129, active address control acknowledged. 208 fresh products across 24 retailer listings; all 208 carried explicit quantity-control unit evidence. Branch and delivery were not independently verified.

At 08:24:04 UTC, the same provider product 35023 (Coca-Cola Original Taste Soda Fridge Pack, 12 x 12 fl oz) was observed at Schnucks for $9.79 and Walgreens for $11.99. Both labels read Add 1 ct Coca-Cola Original Taste Soda Fridge Pack. POST /api/v1/savings returned 200: two packs cost $23.98 versus $19.58, merchandise savings $4.40 (18.35%), with 287 seconds remaining at comparison. Independent Decimal checks matched. Quantity one returned $2.20 savings; quantity three returned $6.60. A different product/package returned 409 product_mismatch. These are timestamped observations, not a promise the prices remain current.

See LIVE-OFFER-VALIDATION.json for sanitized evidence. The proof is API-side. Native Android display/recalculation/expiry acceptance remains open: no connected Android device, emulator or native app workspace was available. No APK was modified. Use the Android acceptance sequence in android-contract.md against two freshly obtained offers; do not reuse the expired search IDs from this run.

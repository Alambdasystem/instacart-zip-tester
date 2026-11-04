# Alambda local offer correctness — v0.8.0

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

- 15 targeted regression tests cover location/reference gates, explicit units, Decimal savings, expiry, product/size/context/merchant/promotion conflicts, background yielding and preservation of the first page after pagination timeout.
- Full regression suite: 170 tests. Final result is recorded with the live release evidence after execution; never treat a mocked fixture as a live offer pair.
- Baseline run initially lacked development-only jsonschema dependencies and encountered an intermittent descendant-reaping assertion; these were recorded before editing that path. Subsequent full run with development validators passed. No production dependency added.
- Existing endpoints and five-minute quote TTL retained. Additive storage/metadata only. No source-provider budget, key, session state or resource-plan change.
- Rollback: redeploy previous commit 03f3693. Database rows are not deleted. Note this restores the older reference-return behavior, so consumers must continue treating unverified observations as references.

## Outstanding acceptance

A real provider search must acknowledge 63129 and expose two identical packaged products at different merchants with explicit each/ea price labels before a positive savings demonstration is valid. The Android build must then consume the response, display/recalculate it and remove the claim on expiry. There is no adb, emulator or connected Android device in this execution environment. API fixture success does not close Android acceptance.

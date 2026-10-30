# MealMe/Satsuma comparison rollout status

October 30, 2026. Integration is committed locally, not deployed.

- Created separate key neadz-test-2026-10-30 with user authorization. Existing keys unchanged.
- Portal merchant read: HTTP 200, one merchant (GoPuff), Inventory/Ordering capabilities and Delivery fulfillment declared.
- Initial St. Louis downtown location and milk product reads: both HTTP 503.
- Additional milk product read at ZIP 63110 centroid (38.6185, -90.2564): HTTP 503.
- Additional milk product read at ZIP 63104 centroid (38.6128, -90.2185): HTTP 503.
- Both additional responses: Required service is currently unavailable; data null.
- These failures do not establish absent retail coverage. They establish unavailable API responses at these coordinates.
- Five authenticated read calls in this session. Portal showed 17/100 after the first three; last two have not been meter-rechecked.
- Billing inspected in this session showed Free, 100 monthly requests, next billing $0. Earlier Plus reports are inconsistent with this observed session.
- SATSUMA_API_KEY saved via Save only on Render service srv-dbigfhmk1f9s73d4de20, Jim's workspace. Key value is not present in source or evidence.
- 35 automated tests passed after merging latest Swagger and Android contract from origin/main at 6df78ca.
- No new app deploy triggered. GitHub tree write rejected with HTTP 403 Resource not accessible by integration. CLI push also lacked authentication. Local branch feat/mealme-provider-comparison is not published.
- No carts, orders, charges, subscription changes or storage upgrades performed.

The app's current free-instance SQLite database is ephemeral. New successful provider results will be separately cached and stored after rollout; no live MealMe products have been observed or inserted yet.

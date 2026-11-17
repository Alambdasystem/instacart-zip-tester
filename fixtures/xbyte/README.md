# Synthetic X-Byte adapter fixtures

Every JSON file in this directory is wholly synthetic. Names, product/store
identifiers, addresses, prices, timings and URLs are fictional test values.
These files do not contain live vendor responses or private correspondence.

The tests exercise missing product identity/link/capture evidence, exact versus
estimated/starting-at prices, source freshness, safe handoffs, pagination and
restaurant menu/budget uncertainty. Synthetic offers with explicit validity
windows do not establish that a provider supplies or guarantees those fields.

The restaurant listing and menu examples are intentionally separate. A test
that connects them creates an explicit synthetic membership bridge. Every
network request is mocked; fixture URLs must never be used for live reads.

ZIP 63131 is the synthetic vendor-pilot test scope. Existing ZIP 63129 backend
search/basket tests remain separate. No installed Android acceptance claim is
made by this suite.

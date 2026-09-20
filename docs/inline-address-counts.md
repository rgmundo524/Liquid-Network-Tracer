# Transaction counts inside address circles

The count is now part of the address circle's native text content, on a final
row below its **Explorer** link:

```text
[Address and attribution labels]
Explorer
TX: 1,234
```

There is one Miro shape for the address and count, with one object ID. Moving
or resizing that shape moves its contents together. No extra label, group,
frame, image, connector, or CSV row is created for the count. With no Explorer
link, the count follows the address labels. Known zero is `TX: 0`; missing
statistics remain `TX: ??`. Automatic statistics fetching, caching, and
the confirmed-plus-mempool calculation are unchanged.

ELK and basic SVG render the count inside the circle. Mermaid source now puts
it directly in the native node label, so standalone renderers display it too;
the old external-label SVG postprocessing is removed. Miro and linked SVG
previews place the count below Explorer. Standalone Mermaid retains its existing
plain node-label format without adding an Explorer link.

## Grouping removed

The grouping implementation, group response parsing, readback/recovery calls,
and grouping progress phases have been deleted. Normal sync and snapshot
publication make no requests to any `/groups` endpoint. The old failed grouping
entries in a local mapping are retained as inactive historical data; they do not
cause retries or require `miro-resolve`. Uncertain ordinary shape/connector
creations are still protected by their existing reconciliation rules.

## Existing boards

Pull the development branch, restart the application, then use normal **Sync to
Miro** with the existing mapping. The address circles and connectors retain their
IDs. Sync updates circle content and removes the old floating count shapes only
when their saved creation proofs and current managed content are intact. Lost
DELETE responses are journaled and recovered on retry. Deletions target the old
count shape IDs, never an address, connector, or group ID. Other analyst objects
are not selected for removal.

Manual circle content/style/position changes retain ordinary conflict protection.
A manually edited old count label or a connector attached to one is protected;
sync reports the conflict instead of deleting analyst work. Existing remote group
relationships are not read or modified through the grouping API. Keep the mapping
and its journal; do not delete them to retry.

Previously generated SVGs and immutable reviewed previews are not rewritten.
A saved publication plan that still contains external count shapes is rejected
before any board writes and asks for a fresh preview. Regenerate old starter-
connection or compact previews before publishing. Graph presentation version 18
prevents reusing old-version ELK previews as if they had the new display. No new
blockchain trace or frontend rebuild is needed for this Python-rendering change.
The automatic renderer heap policy uses 90% of currently available memory.
Colors, borders, and transaction CSV columns are unchanged by inline counts.

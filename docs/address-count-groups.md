# Group address circles with their transaction counts

Normal **Sync to Miro** now creates a native Miro group for each address circle
and its generated transaction-count label. Dragging the group in Miro moves
both items together immediately; no sync is needed to make the label follow.
The count stays above the circle and remains a separately editable item within
the group. Known counts, zero and `??` labels are all grouped the same way.

Grouping uses the existing mapped circle and count IDs. Existing boards are
upgraded on their next normal sync, without replacing circles, duplicating
labels, changing the trace, or reconnecting arrows. Only the circle and its own
count belong to each pair. Transaction boxes, connectors and other addresses
are not added. Miro group relationships do not create graph nodes or CSV rows.
No tracing, address-statistics lookup or ELK calculation is introduced by the
grouping step itself; the existing normal workflow still prepares its graph.

Snapshot publication (including starter connections) groups the same pairs.
Repeating a publication or sync checks live membership and does not create a
second group for a pair that is already grouped. Count updates keep the group
and the existing circle/label IDs. Pure CSV exports, saved SVGs, rendering and
local-only dry runs do not contact Miro or modify any group.

## Manual edits and interrupted requests

The complete group inventory is paginated and checked before ordinary sync writes. An existing exact two-item pair can be reused. Larger user-created
groups, conflicting memberships and manual changes to previously acknowledged
groups are preserved and reported, not ungrouped or overwritten. If you manually
ungroup a pair that the program had already grouped, later syncs preserve that
choice. The program never calls the group-delete or ungroup endpoints.

Each new grouping request has a durable intent in the board's existing local
Miro mapping/journal. An uncertain response is not blindly replayed. On retry,
an exact group found in the live inventory is acknowledged without another
POST. If it is not found, the operation stops for explicit reconciliation using
`miro-resolve --state MAPPING_PATH --key GROUP_KEY --item-id GROUP_ID`, or
`--absent` only after inspecting the board and confirming no group was created.
Keep the mapping and its journal. Do not delete them to restart publication.

The progress display includes **Grouping addresses with their transaction
counts**. Results include `address_groups` with created, reused, preserved and
conflict counts/details. `max-new-items` continues to limit new individual
shapes/connectors/frames; group relationships are reported separately and their
API requests use the Miro rate-limit coordinator. `boards:read` and
`boards:write` scopes are required.

Update the development branch, restart the application, then use normal sync.
No special grouping command or frontend rebuild is required for this change.

REST references:
- https://developers.miro.com/reference/creategroup
- https://developers.miro.com/reference/get-all-groups
- https://developers.miro.com/reference/getitemsbygroupid

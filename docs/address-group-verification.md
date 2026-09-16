# Verify native address/count groups

This corrects false-positive grouping acknowledgements. A group is a native Miro
selection/movement unit containing the existing circle and the separate editable
count above it. It is not a flattened picture, frame or replacement circle.

The earlier grouping code reused an inventory taken before shape/parent/frame
updates, accepted a successful POST without reading membership, and treated any
previous local group record as a reason not to repair missing grouping. Those
behaviors could report grouped items when the pair was absent from the board.

The revised implementation:

* Reads the live group inventory again after graph mutations and before grouping.
* Reads all pages of `GET /groups/items?group_item_id=...` after creating each
  group. Only an exact circle/count pair becomes an acknowledged creation.
* Keeps an unverified POST response ID in the existing durable pending journal.
  Uncertain outcomes are checked on retry, never blindly reposted.
* Repairs missing pairs from older, unverified records when both items are free.
  It also repairs a previously existing pair lost during the current sync.
* Preserves larger/different user groups. Deliberately ungrouping a pair that this
  version previously verified is still respected. Such conflicts are reported
  as incomplete grouping, not as verified pairs.

The `address_groups` result includes `verified`, `total`, `repaired`, and `status`
(`complete` or `incomplete`) alongside created/reused/preserved/conflict counts.
These counts reflect the observed board membership, not merely POST attempts.
Normal sync does not emit its generic complete progress event when grouping is
incomplete. API/verification errors stop success reporting and preserve progress.

Update the development branch and restart, then run normal Sync to Miro. Existing
circles, count labels and connectors retain their IDs. No special grouping command,
new tracing run, mapping deletion, or frontend rebuild is required. The grouping
pass runs after the graph items have been written. Normal layout and statistics
preparation are unchanged. Snapshot publication also verifies newly created groups.

Tests simulate successful responses with missing or wrong server membership,
groups lost during updates, legacy journal repair, pagination, uncertain POST
recovery, manual groups, repeat sync and immutable snapshot publication. These
protocol tests are not live validation of any investigator's Miro board.

Official REST references:
https://developers.miro.com/reference/creategroup
https://developers.miro.com/reference/getitemsbygroupid
https://developers.miro.com/reference/get-all-groups

# Miro group response parsing

The error after `Grouping addresses with their transaction counts (0/25)` can
occur while reading membership after the first successful group POST. This is
an API-envelope parsing bug, not an ELK or Node heap issue.

The old shared pagination helper required a top-level `data` array for both
endpoints. Miro's Platform groups OpenAPI documents different response shapes:

- `GET /groups`: `data` is an array of group summaries.
- `GET /groups/items?group_item_id=...`: `data` is a group object with its `id`,
  `type`, and nested `data`. The schema places `ItemPagedResponse` records inside
  that nested array; each member page contains a `data` array of item objects.
- Group summaries can wrap member IDs at `data.data.items` because
  `GroupResponseShort.data` references `Group`, which has its own `data` wrapper.

The parser now handles these endpoint-specific envelopes, direct member arrays
inside a group wrapper, and the legacy flat-list form. It validates a wrapped
group's identity, reads continuation cursors at supported envelope levels, and
rejects mismatched groups, duplicate members, cursor loops, conflicting metadata,
and truncated membership pages. It never follows response URLs or treats an
unrecognized response as an empty group. Errors do not expose board contents.

The shared simulated Miro service now returns the documented nested envelopes.
Regression tests cover the original failure, all 25 address/count pairs, normal
and snapshot publication, multiple pages, and resuming after a group was created
but its membership response could not be parsed.

After updating the development branch and restarting, normal **Sync to Miro**
uses the existing mapping and checks the pending pair against live membership
before continuing. It does not recreate an already verified group or replace the
address/count shapes and connectors. Keep the Miro mapping and its journal;
manual grouping, deleting the mapping, or retracing is not required for this fix.
An actually uncertain or conflicting remote outcome still stops or reports
incomplete grouping rather than claiming success.

This changes grouping response handling only. Automatic address counts, graph
layout, CSV columns, and the 75% automatic renderer heap policy are unchanged.
No frontend rebuild is needed. Tests use synthetic IDs, not a live case board.

Official response schema references:
https://developers.miro.com/reference/get-all-groups.md
https://developers.miro.com/reference/getitemsbygroupid.md
https://developers.miro.com/reference/creategroup.md

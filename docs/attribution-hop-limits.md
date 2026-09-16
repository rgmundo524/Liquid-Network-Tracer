# Attribution hop limits and address presentation

The attribution **import** accepts an optional `hop_limit`. This is separate from
`transactions.csv`, whose transaction input/output column order is unchanged.

```csv
Address,Name,confidence,stop_tracing,hop_limit,source,notes
REPLACE_WITH_DEPOSIT_ADDRESS,Example Exchange,suspected,false,1,Investigator research,Include consolidation then stop
```

Replace the placeholder before importing. The terminal and browser import review
show the limit. Address review can edit or clear it individually. Existing import
files without the column remain valid. Values must be blank or nonnegative whole
numbers; `0` is not the same as blank.

## Counting additional hops

An arrival at the annotated output consumes no local hop. One hop is its spend by
the next transaction. Thus `hop_limit=1` includes the consolidation transaction
and its outputs, then stops further expansion along that path. A value of 2 allows
one more transaction spend. The overall run hop/request/time limits still apply.

* `stop_tracing=true`: stop at the annotated address, regardless of `hop_limit`.
* `stop_tracing=false`, blank `hop_limit`: normal tracing, without a local cap.
* `stop_tracing=false`, `hop_limit=0`: stop at this output.
* `stop_tracing=false`, positive `hop_limit`: allow that many additional spends.

Every inherited budget decreases along the path. Reusing the address, reaching
another annotation, or continuing/restarting the investigation does not refill
it. A stricter downstream rule can shorten it. Independent selected outputs or
other permitted paths keep their own allowance. A short exhausted path cannot
lend its global hop depth to a longer open path.

The limit applies to fresh traces and continuations. Adding it to an already
expanded run holds frontier reachable only beyond the new boundary. It does not
delete old transactions, links, raw responses, or existing board objects. The
full graph remains the saved evidence view; the starter-connections view applies
the current cap separately to each source starter. It does not claim common
ownership or carry a service's attribution onto all descendants.

The CLI accepts `service-set --hop-limit 1`; `--hop-limit ''` clears it. Omitting
the option when changing another assessment field preserves an existing limit.

## Address labels

Graph addresses now show their first six characters, three dots, and last four
characters, for example `abcdef...wxyz`. Transaction-hash shortening is unchanged.
Addresses are never merged based on that display string. Full addresses remain
in transaction CSVs, saved evidence, metadata, and explorer links.

The visible label and color-menu category are **Unspent**, without “endpoint”.
Internal saved role names remain compatible with older investigations. The label
still means a tracked output was unspent at its last observation, not that an
entire address is dormant or contains only unspent funds.

## The number above each address circle

The number is the address's **confirmed plus mempool transaction count at the last
statistics lookup**, not the number of chart arrows, UTXOs, or visible transactions.
Unknown counts show `??`; an observed zero displays `0`. Counts include their source
and observation time in local graph details. They are not ownership evidence.

Normal traces, chart generation and ordinary live Miro sync now fetch missing
counts automatically. **Fetch address transaction counts** remains an optional
separate lookup/refresh. The lookup uses: one statistics endpoint per uncached Liquid
address, with the normal request/time bounds. It does not enumerate history, trace
more funds, run ELK, or contact Miro. Each response is saved so another invocation
fetches only remaining missing counts. Existing verified address-review statistics
are reused. API authentication/retries can also consume the request budget.

New local previews and normal **Sync to Miro** obtain and display missing counts
without a separate fetch step. Existing rendered files are not rewritten. No retracing is needed for display-only changes. Normal Miro sync
adds one managed text shape per address circle and follows the circle's current
position, including manual moves; those shapes count against the new-item budget.
No additional transaction CSV rows are created. Old run archives and immutable
connection/compact previews are not rewritten. Generate a fresh connection/compact
preview to include updated counts.

ELK/basic SVG and Miro place counts above circles. The local Mermaid SVG adds them
after rendering, using actual circle positions. Standalone Mermaid source carries
counts as comments because Mermaid has no equivalent external-node-label syntax;
an unrelated Mermaid renderer will not show the external text automatically.
Bitcoin peg-in context circles currently show `??`: a Liquid lookup is not used to
invent a Bitcoin address-history count.

```sh
liquid-trace address-counts --case /path/to/investigation --run latest
liquid-trace address-counts --case /path/to/investigation --run latest --refresh
```

The second command refreshes counts that are already cached. Pull the development
branch and restart the application; rebuild a compiled browser frontend. Display
changes may require a fresh ELK preview because old labels no longer match.

See [Automatic address transaction counts](automatic-address-counts.md) for authentication, budgets and failure reporting.

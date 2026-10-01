# Attribution hop limits and address presentation

The attribution **import** accepts an optional `hop_limit`. This is separate from
`transactions.csv`, whose transaction input/output column order is unchanged.

```csv
Address,Name,confidence,stop_tracing,hop_limit,source,notes
REPLACE_WITH_DEPOSIT_ADDRESS,Example Exchange,suspected,false,1,Investigator research,Show one consolidation hop in Full trace
```

Replace the placeholder before importing. The terminal and browser import review
show the limit. Address review can edit or clear it individually. Existing import
files without the column remain valid. Values must be blank or nonnegative whole
numbers; `0` is not the same as blank.

## Which phases use the rules

Active `stop_tracing` rules apply to collection and every plotting goal. The
attribution `hop_limit` is a local display/traversal cap for **Full trace** and
**Starter connections** only:

| Phase or goal | `stop_tracing=true` | Attribution `hop_limit`, including `0` |
| --- | --- | --- |
| Collect transaction data | Stops that path | Ignored |
| Paths to peg-outs, including standalone searches | Stops that path | Ignored |
| Full trace | Stops that path | Applied |
| Starter connections | Stops that path | Applied |

Collection still obeys the run's overall hop ceiling, confirmation policy, and
transaction, output, API-request, and time budgets. Peg-out tracing still obeys
its selected global hop range. Ignoring an address's local cap does not remove
these limits or change the selected seeds. To stop collection or a peg-out path
at an address, use `stop_tracing=true`; `hop_limit=0` alone does not stop them.

## Counting additional hops in Full trace and Starter connections

An arrival at the annotated output consumes no local hop. One hop is its spend by
the next transaction. Thus `hop_limit=1` includes the consolidation transaction
and its outputs in these views, then stops further expansion along that path.
A value of 2 allows one more transaction spend. The selected global hop ceiling
and available saved evidence still bound the plotted activity.

* `stop_tracing=true`: stop at the annotated address, regardless of `hop_limit`.
* `stop_tracing=false`, blank `hop_limit`: no local cap.
* `stop_tracing=false`, `hop_limit=0`: stop this plotted path at the output.
* `stop_tracing=false`, positive `hop_limit`: display that many additional spends.

Every inherited local budget decreases along the path. Reusing the address or
reaching another annotation does not refill it. A stricter downstream rule can
shorten it. Independent selected outputs or other permitted paths keep their
own allowance. A short exhausted path cannot lend its global hop depth to a
longer open path. Named-group hop resets do not replenish these local budgets.

Changing a saved limit affects the next generated Full trace or Starter
connections plot. Tightening the limit removes paths from those new views;
loosening it restores only activity already collected. It does not delete saved
transactions, raw responses, or historical plots, and does not establish common
ownership or carry a service's attribution onto descendants. Use **Replace**
when importing a changed CSV; **Keep existing** retains the previous limit.

## Existing investigations collected with address caps

Older collection runs may lack transactions because an attribution `hop_limit`
previously stopped their branches. Updating the application does not fill those
gaps automatically. **Continue collection with 0 additional hops** to revisit
eligible branches within the saved global ceiling, or add hops to increase that
ceiling. Current `stop_tracing` rules and resource budgets still apply. For
example, a branch previously capped at hop 3 can now be collected toward the
saved overall ceiling of 10, unless an explicit address stop intervenes.

Generate new plots from the resulting run. New peg-out plots ignore address
hop caps; Full trace and Starter connections still apply them. Existing saved
reports, endpoint tables, and plots retain their recorded rules and results.

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

## The transaction count inside each address circle

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

New local previews and normal **Sync to Miro** obtain missing counts automatically.
In Miro, `TX: 1,234` is the final text row inside the address circle, below
Explorer. It is part of the same shape, not a separate label or group. ELK/basic
SVG counts are also inside the circle; standalone Mermaid includes the count
directly in its node label and needs no SVG postprocessing.

Normal sync safely retires old generated external labels using their saved
creation proofs. Failed grouping journals no longer trigger requests or block
this change. Keep the existing Miro mapping. No extra transaction CSV rows are
created, and full addresses and transaction-count observations stay unchanged.
Old run archives and immutable connection/compact previews are not rewritten;
regenerate a preview with external labels before publishing it. Bitcoin peg-in
context circles still have unknown counts rather than invented Liquid statistics.

```sh
liquid-trace address-counts --case /path/to/investigation --run latest
liquid-trace address-counts --case /path/to/investigation --run latest --refresh
```

The second command refreshes counts that are already cached. Pull the development
branch and restart the application; rebuild a compiled browser frontend. Display
changes may require a fresh ELK preview because old labels no longer match.

See [Automatic address transaction counts](automatic-address-counts.md) for authentication, budgets and failure reporting.

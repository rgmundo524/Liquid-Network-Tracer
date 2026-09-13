# Chronological starting-transaction numbers

Starting transactions have one global, 1-based display number for the saved graph.
The earliest recorded confirmed `status.block_time` is number 1. Full transaction
keys break timestamp ties deterministically; this does not claim a within-block
transaction order. Unconfirmed transactions and transactions without a usable
confirmation timestamp come last, sorted by full key. Input-list order, hop
number, address reuse, and the number of selected outputs do not determine the
number. Multiple selected outputs of one transaction share one number.

The transaction box displays `Starting TX 1`, keeping its existing hash, date and
hop information. Activity frame titles list the actual numbers, not their count:

```text
Activity 1 · Starting transactions 1, 4, 7
Activity 2 · Starting transaction 2
Activity 3 · Starting transactions 3, 5, 6
```

Numbers refer to all starting transactions present in the graph, not a numbering
that restarts within each activity. A component without a starting transaction is
labeled `No starting transactions`. The complete key/index/timestamp mapping is
saved in `activity_frames.starting_transactions` in graph and Miro-plan JSON.
Graph connectivity is a display grouping, not common ownership or allocation of
stolen value. No tracing links, fees, service rules or archived evidence change.

## Existing investigations

After updating the development branch and restarting the application, select
**Sync to Miro** to refresh generated frame titles and starting-transaction labels.
No new trace or address-merge migration is required for this title change. Existing
frame keys and remote IDs are retained. Ordinary sync preserves manual title and
node-content edits; it does not force-reset them. **Sync and reorganize Miro graph**
is only needed when you also want new automatic positions.

Regenerated ELK, Mermaid and compact previews use the new numbers. Already saved
previews retain their reviewed contents, so regenerate them to see new labels.
Historical schema-1 frame plans with count-only titles remain readable and valid.
The new catalog is schema 2 and is validated together with the exact graph
partition and generated titles.

The numbering is deterministic for an unchanged set of starting transactions and
recorded confirmation times. If a previously unavailable starting transaction is
loaded, a timestamp becomes known, or the starting set changes, chronological
numbers may change on regeneration. These are display indexes, not permanent
evidence identifiers; full transaction hashes remain authoritative.

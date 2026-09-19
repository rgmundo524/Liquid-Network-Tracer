# Designate transaction change

Open **Change outputs** in an investigation to optionally identify one change
output per transaction. This is your designation: the application does not
infer change or ownership from amounts, addresses, or transaction structure.
It guides presentation without changing which outputs are traced.

## Select an output

1. Enter the full transaction hash and load its outputs.
2. Select the change `vout`, add optional notes, and save.
3. Regenerate the **ELK layout preview** to inspect the arrangement. Use
   **Sync and reorganize Miro graph** to apply it to existing Miro objects.

Output indexes start at zero. Fees, pegouts, and unspendable outputs cannot be
selected. Choose **None** to clear a designation and restore normal ELK rules
for that transaction. Saved selections can also be searched and edited.

Lookup uses verified saved transaction data first. If the transaction is absent,
it uses the investigation's configured source for a bounded lookup. A live lookup
may require your Blockstream credentials. It does not start a trace or add the
transaction to the saved graph. Confidential amounts remain unknown.

## Import a CSV

Choose **Import change outputs**, upload a UTF-8 file or paste its contents, then
preview and apply the reviewed changes. The required columns are `Txid` and
`ChangeVout`; `Notes` is optional. Use the
[CSV template](../examples/change-outputs-template.csv), replacing its example hash.

```csv
Txid,ChangeVout,Notes
0000000000000000000000000000000000000000000000000000000000000000,0,Replace with your transaction hash
```

Existing selections are kept by default. Choose replacement to change an existing
selection or clear it with a blank `ChangeVout`. JSON arrays with the same fields
are also accepted; use an integer index or `null` to clear. Headers are
case-insensitive and transaction hashes are normalized to lowercase.

Imports are offline. Unknown transaction hashes may be saved before a trace
reaches them. Outputs already in saved evidence are checked for existence and
spendability. An invalid row blocks the whole import. Identical duplicates are
combined; conflicting rows for the same transaction are rejected. Files are
limited to 512 KiB and 5,000 rows per batch.

Changing the text, options, or investigation settings invalidates the review.
Applying a valid batch makes one atomic settings update with revision history.

## Layout behavior

| Selection | ELK presentation |
| --- | --- |
| Valid change output | Its address sits on the transaction's horizontal row. When compatible, its sole forward spending transaction joins that row. |
| Other outputs of that transaction | Their objects sit below the change row. Fees retain their separate chronological row. |
| No change selected | Normal ELK placement rules apply. Nearby objects may still move to make room for a designated branch. |
| Transaction absent from the graph | Selection is retained for a later graph and reported as skipped in this preview. |
| Shared address or conflicting rows | The preview reports why alignment was skipped. The address remains one shared object. |

The designation belongs to an exact `txid:vout`, not to every output using that
address. The connection caption includes **Change**. ELK previews report applied
and skipped selections. Compaction preserves the enforced rows. Mermaid uses
its own placement engine, so its diagram includes the designation but does not
enforce these ELK rows.

Normal **Sync to Miro** updates generated content and preserves existing manual
positions. Choose **Sync and reorganize Miro graph** to apply the new positions,
or review and apply a new compact layout. Saving a designation alone does not
change a live board. The usual checks for manual edits still apply.

Selections are saved per investigation in `services.json`. Current previews and
sync use current selections; archived runs retain their original snapshots.
Changing a selection invalidates older reviewed compact layouts, so regenerate
the compact preview before applying it.

## Command line

```bash
liquid-trace change-output-lookup --case cases/my-case --txid TRANSACTION_HASH
liquid-trace change-output-set --case cases/my-case --txid TRANSACTION_HASH --vout 0
liquid-trace change-output-list --case cases/my-case
liquid-trace change-output-set --case cases/my-case --txid TRANSACTION_HASH --clear

liquid-trace change-output-import --case cases/my-case --file change.csv --dry-run
liquid-trace change-output-import --case cases/my-case --file change.csv \
  --approve-plan REVIEWED_APPROVAL_SHA256
```

Use the `approval_sha256` from the preview you reviewed. Add `--on-conflict replace`
to both import commands to replace or clear existing selections. Use
`--format auto|csv|json` to select the parser. Single-output saves accept `--notes`
and an optional `--expected-revision` to reject a stale edit.

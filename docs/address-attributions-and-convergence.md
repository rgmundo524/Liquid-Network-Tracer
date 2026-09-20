# Explicit address attributions and convergence highlighting

## Six import fields

```csv
Address,Name,confidence,stop_tracing,source,notes
REPLACE_WITH_PUBLIC_LIQUID_ADDRESS,Example Exchange,suspected,true,Investigator research,Explain the evidence
```

`Address` is the full public Liquid address from the trace/explorer. `Name`,
`source`, and `notes` are free text. Confidence accepts only `suspected` or
`confirmed`. Stop tracing accepts `true` or `false` (CSV also accepts yes/no and
1/0). Column names are case-insensitive; `rationale` remains an alias for `notes`.
Classification is no longer an input field or a prerequisite for stopping.
An old Classification column is ignored in CSV files. Old candidate/corroborated
confidence values are not accepted in new uploads: choose suspected or confirmed
explicitly.

CSV columns can appear in any order. Unrelated columns, such as a spreadsheet
`Duplicate count`, are ignored and are not saved as attribution data. Supported
columns still use their normal validation, including the legacy aliases and
optional `kind=address` and `network=liquid` compatibility fields. Each recognized
column must appear only once. JSON imports still reject unsupported fields.

Only Address is required. Omitted confidence defaults to suspected; omitted
stop_tracing defaults to true. An address-only list therefore creates suspected,
unnamed address assessments with stopping enabled. Set Name to a useful entity
name or alias, and set stop_tracing=false explicitly for annotation without a
stop. Optional enabled and observed_at fields remain available for disabling an
assessment and dating supporting evidence. Imports are case-local, reviewed,
atomic, limited to 5,000 rows / 512 KiB, and make no blockchain or Miro calls.

| Confidence | Stop tracing | Visible name | Tracing |
| --- | --- | --- | --- |
| suspected | true | Suspected Example Exchange | Stop at the address |
| confirmed | true | Example Exchange | Stop at the address |
| suspected | false | Suspected Example Exchange | This assessment does not stop traversal |
| confirmed | false | Example Exchange | This assessment does not stop traversal |

No `(confirmed)`, `(candidate)`, `Service`, or `Label` suffix is automatically
added. Confidence is the investigator's assertion, not a verification performed
by the tool. It controls only the name prefix, never the color. Use **Assign
name colors** after import to choose a color for each case-insensitive name.
Selected seed outputs remain red even when attributed or unspent. The independent
**STOP TRACING** line and all attribution names and notes remain present. See
[name colors and seed priority](name-colors.md) for the full precedence rules.

Stops apply to the first run and continuations, including selected seed outputs.
There is no seed exemption. All address stop flags use the same reachability
boundary logic, regardless of confidence or historic classification. This also
means an address stop imported through the legacy `--labels` path now holds
exclusive descendants on continuation, rather than only stopping new encounters.
The old internal `suspected_service_stop` / `held_behind_service` output status
names remain for archive compatibility; they are not confidence claims.

## Attribution information without Miro cards

New Miro plans no longer create attribution register cards or separate stars.
Names, suspected prefixes, STOP TRACING indicators, and assigned name colors stay
on address circles. The A- reference is removed from the Miro circle's visible
label because there is no longer a matching on-board card. The reference and all
source/notes data remain in local HTML, graph JSON, and node CSV exports and
Address review. Local HTML's expandable attribution register remains available.
Removing cards does not delete or change any assessment or evidence archive.

No register column is generated or included in new full-board compaction metrics.
Longer notes therefore do not increase the Miro graph's item count or its bounds.

## Two independent branch-interaction signals

New presentations use **red (#ff0000), 12-pixel borders** for two distinct
relationships. Labels identify which relationship caused the highlight. The
border does not change node fill, confidence, names, stop flags, topology or
tracing. Starting transaction boxes remain purple, subsequent transactions remain
blue, and selected seed address circles retain highest-priority red fill. No
separate star or attribution-card objects are generated.

### INPUT MERGE: transaction inputs

`transaction_convergences()` in `liquid_tracer/convergence.py` retains the existing
rule. At least two different nonempty starting-origin sets must meet at a
transaction through saved, validated UTXO spending links. Their union must contain
at least two starting transactions. An explicitly selected starting transaction
also contributes its own origin, so it can qualify when another origin arrives.
Multiple selected outputs from one starting transaction share one origin.

Only `state["links"]` contributes to this propagation. The exact saved funding
outpoint must match the recorded spending input. Unselected starting outputs and
untraced context inputs never acquire an origin just because they are visible.
Active stops block further propagation through that output. A transaction with
one already-merged input, or multiple inputs carrying the identical merged origin
set, is not highlighted again. Different overlapping sets, such as {1, 2} and
{2}, still qualify. An eligible box displays **INPUT MERGE**.

### SHARED ADDRESS: receipts from different branches

After propagating UTXO origins, `branch_interactions()` scans **all recorded,
tracked receiving outputs** in the saved graph. It groups spendable outputs by
the **exact full Liquid address**, not by names, shortened labels, case-folded
addresses, timestamps or activity-frame membership. An address qualifies when
its receipts contain at least two different nonempty origin sets whose union
contains at least two starting origins. No later joint-spend transaction is
required. Selected initial outputs are included even in a zero-hop graph.

A qualifying address circle gets a thick red border and **SHARED ADDRESS** label.
Every transaction with a qualifying receipt to that address gets the same label
and border, including the **earlier sender**, not just the later sender. This is
a sender/destination interaction, not an assertion that the sender merged input
UTXOs. A transaction can have both **INPUT MERGE** and **SHARED ADDRESS** labels.
Only participating senders are marked; all their ancestors, unrelated outputs,
and co-inputs are not automatically highlighted.

For example, if Starting TX 1 pays address X and a later transaction in starting
branch 2 also pays X, the regenerated graph marks **both senders and X**. It does
not wait until X's two outputs are spent together. The address need not have any
saved spending transaction. Receipts at an active stop address can qualify:
arrival is visible, but the stop continues to prevent further traversal.

This is a graph-wide post-pass, not a first-seen/second-seen flag. Transaction or
API result order cannot hide the earlier participant. Continuing a run recomputes
the signals from all saved evidence and can update earlier existing objects.
An already saved preview/archive is not rewritten: regenerate a presentation and
sync it. Legacy occurrence exports mark all occurrences of that same address;
the default graph still has only one circle per full address per network.

**Address grouping never feeds its origin union back into UTXO propagation.**
For example, X can receive an output from branch 1 and another from branch 2,
while a later transaction spends only branch 1's output. That later transaction
does not inherit branch 2 merely because its input uses address X. Neither signal
proves common ownership, value allocation or identical confidential asset types.

| Situation | Input merge | Shared-address interaction |
| --- | --- | --- |
| A transaction spends inputs carrying {1} and {2} | Yes, at that transaction | Only if a receiving address also meets its separate rule |
| Starting TX 2 receives an allowed verified input from branch 1 | Yes, at Starting TX 2 | Evaluated independently |
| Two different starting transactions pay X | No joint spend required | X and both senders, retroactively |
| A single starting transaction pays X twice | No | No, just one origin |
| One already-merged output carrying {1, 2} pays X | Not a new input merge | Not by itself, only one receipt origin set |
| X receives several outputs all carrying the identical {1, 2} set | Not a new input merge by itself | No, repetition of one already-merged lineage |
| X receives {1, 2} and separately {2} | Evaluated independently | Yes, different overlapping sets |
| The second apparent branch is context-only or behind an upstream stop | Does not contribute | Does not contribute unless independently seeded/reached |

Both detectors are offline and limited to saved trace scope. Missing receipts or
links beyond the run's bounds are not inferred and require further collection.

## Inspecting the evidence and missing highlights

In a newly regenerated `graph.json`:

- A transaction's `convergence` record identifies **input merges**, with exact
  contributing input outpoints and chronological Starting TX numbers.
- An address's `address_convergence` summary identifies its **shared receipts**.
  The full receiving-output table is stored once in top-level
  `address_convergences`, keyed by network and full address. Each receipt includes
  its exact outpoint, sending transaction, output index, and starting-origin
  numbers. The same table is retained in the Miro plan JSON, not drawn as cards.
- Each participating sender's `address_interactions` lists the shared addresses
  and only its own contributing outputs/origin numbers. This avoids copying the
  whole address receipt history into every sender. `interaction_types` lists the
  independent reasons. Node CSV exports include these fields as well.

A marked Miro shape should have `style.borderColor="#ff0000"` and
`style.borderWidth="12"`. **SHARED ADDRESS** on a sending transaction does not mean
its `convergence` record should be populated. Either independent signal can
cause its border. This separates a detection/scope issue from a render/sync issue.
Normal sync updates generated borders on existing objects while preserving their
IDs and positions. Manually edited Miro borders or labels remain protected by the
usual conflict handling and may prevent a generated change from being visible.

## Updating existing boards safely

Pull the development branch, restart the application, and choose **Sync to
Miro**. New plans deliberately include an empty presentation-item catalog. This
allows existing sync to retire previously generated cards and stars using their
saved creation proofs. Transaction/address/connector identities, positions, labels,
name colors, and evidence are not recreated or retraced. Generated complete-frame
bounds are refitted without the old register. Reorganization is optional.

Only proven managed annotations are eligible for deletion. Edited card/star
content or styles, or connectors attached to retiring annotations, block cleanup
before writes. Copy important manual notes into Address review or another medium
first. Preserve comments separately: the REST item snapshot does not back them
up. Do not remove Miro mapping/checkpoint files or edit the board during sync.
Interrupted cleanup uses the existing journals; retry the same intended sync.
This is not an atomic cross-item transaction or an automatic undo operation.

Already saved explicit plans/previews remain historical artifacts. Their legacy
annotation proofs and placement are still accepted for recovery and validation;
regenerate a new presentation for the new behavior. Finish or reconcile an
interrupted old sync before switching presentations. No archived `runs/` files
or live investigation data are changed by the code update itself.

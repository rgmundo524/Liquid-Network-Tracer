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
Remove that column from old CSV templates. Old candidate/corroborated values are
not accepted in new uploads: choose suspected or confirmed explicitly.

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

## Thick red borders: where starting lineages meet

Eligible transactions now have a native **red (#ff0000), 12-pixel border**, six
times the ordinary 2-pixel border. The detector is unchanged. A starting
transaction can qualify and keeps its purple fill; subsequent transactions keep
their blue fill. Selected seed address circles still have highest-priority red
fill. The transaction border does not use attribution confidence or name colors.
There is no extra marker shape to hide behind another object or drift away when
the transaction is moved. Miro, basic SVG, ELK/compact SVG, and Mermaid use the same
border policy. The old corner/inline star is no longer generated.

The exact rule is in `transaction_convergences()` in `liquid_tracer/convergence.py`:

1. At least two distinct starting transactions must be present in the graph's
   chronological starting catalog. Multiple selected outputs of one transaction
   share one origin.
2. Only `state["links"]` spending links contribute. The exact funding output must
   be tracked and the recorded spending input must match that outpoint. Merely
   showing an input as untraced context, sharing an address, sharing a name,
   being in the same activity frame, or being close in time never creates a link.
3. Origin sets propagate forward through those saved links. A spendable output
   at an active stop address does not contribute. Own starting-origin propagation
   begins only at selected outputs, not unselected sibling outputs.
4. At a transaction, gather the nonempty incoming origin sets and its own origin
   when it is itself a starting transaction. Highlight only when there are at
   least two *different* sets and at least two distinct starting origins in their
   union: `len(groups) > 1 and combined.bit_count() > 1`.

| Situation at a transaction | Highlight? |
| --- | --- |
| Inputs carrying {1} and {2} | Yes |
| Starting TX 2 receives an allowed verified input from Starting TX 1 | Yes |
| Inputs carrying {1} and {1} | No |
| One input carrying an already merged {1, 2} | No |
| Two inputs both carrying the same already merged {1, 2} | No |
| Inputs carrying {1, 2} and {2}, or {1, 2} and {3} | Yes |
| Two starting transactions only pay the same shared address circle | No |
| An apparent second branch is untraced context or blocked by an active stop | No, unless other qualifying branches remain |

This is convergence of saved UTXO paths, **not common ownership, allocation of
stolen value, or proof that confidential inputs/outputs have the same asset**.
The graph JSON and node CSV `convergence` record keep the exact input outpoints
and chronological Starting TX numbers. No API requests are made by the detector.
A bounded run cannot highlight a merge whose spending transaction/link has not
yet been collected.

## Diagnosing missing highlights

Check a newly regenerated `graph.json`, not an old reviewed preview. An eligible
transaction node has a nonempty `convergence` object. The generated Miro plan's
matching transaction shape should have `style.borderColor="#ff0000"` and
`style.borderWidth="12"`. These distinguish a detection/scope problem from a
render/sync issue. Changing the marker from a star to a border does not make a
transaction qualify when it has no convergence record.

The old star was a separate 24-by-24 transparent shape containing a glyph. A
successful synthetic detector test does not establish why a particular live
board did not display that item. Without the affected saved graph/plan and board
state, that specific failure remains unverified. Manually edited Miro borders
remain protected by the usual field-conflict logic and are reported as conflicts.

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

# Explicit address attributions and convergence stars

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
by the tool. It controls the name prefix and color: cyan for suspected, green for
confirmed. The independent **STOP TRACING** line appears on active stop nodes.
Multiple assessments keep their individual names and notes; a confirmed
assessment has color priority, without removing a competing suspected name.

Stops apply to the first run and continuations, including selected seed outputs.
There is no seed exemption. All address stop flags use the same reachability
boundary logic, regardless of confidence or historic classification. This also
means an address stop imported through the legacy `--labels` path now holds
exclusive descendants on continuation, rather than only stopping new encounters.
The old internal `suspected_service_stop` / `held_behind_service` output status
names remain for archive compatibility; they are not confidence claims.

## Evidence in the graph output

Each attributed address has a stable `A-...` reference. On Miro, generated
register cards in a separate column contain that reference, full address, name,
confidence, stop flag, source, notes, and observation date. Long entries are
wrapped and paginated. The cards are native editable Miro objects, not blockchain
nodes. They have no UTXO edges and never join activity components. The Complete
graph frame includes them. New cards count against the normal Miro new-item
budget. Main-graph compaction measurements exclude them; full-board measurements
include their estimated footprint.

Local ELK, Mermaid and compact HTML previews provide an expandable attribution
register. The basic HTML inspection export also exposes the node's evidence
record. JSON and CSV retain the full fields; nodes.csv includes name, confidence,
source, notes, stop_tracing and the reference. Legacy service_* export aliases
remain readable for existing consumers, but classification is no longer a
standalone node CSV column. Individual old raw evidence records are not rewritten.

Normal sync refreshes generated card content and refits the register beside the
current managed graph; manually edited content/style is preserved as a conflict.
Card dimensions may grow to fit longer generated content. Disabling/removing the
last assessment retires its generated cards. Modified cards or board connectors
attached to retiring annotations block removal before writes. Preserve Miro
comments separately before retiring cards: the REST item snapshot does not expose
comments. Do not edit the board concurrently with sync.

## Corner stars: where starting lineages meet

A small gold star appears inside the upper-right corner of a transaction when
different starting-transaction lineages meet there. A starting transaction is
eligible: its own explicitly selected starting origin can meet an earlier origin
arriving through a verified spend. Origin numbers use the existing global,
chronological Starting TX indexes, not numbering per activity.

The detector follows only saved, validated UTXO spending links. It checks the
exact funding output against the spending input. It never follows an edge merely
because an address is shared, two addresses have the same name, transactions are
in the same frame, timestamps are close, or inputs are shown as untraced context.
Multiple selected outputs from one starting transaction count as one origin.
Active address stops prevent propagation through the blocked path, while
independent permitted paths can still contribute.

A transaction is marked when at least two different nonempty origin sets meet,
counting its own starting origin where applicable. A simple downstream spend
carrying an already merged lineage is not marked again. Two inputs carrying the
same previously merged origin set are also not a new convergence. A different
incoming lineage set meeting that merged set is a new interaction and is marked.
This definition highlights convergence points, not every multi-input transaction.

The graph JSON and node CSV convergence record list the contributing Starting TX
numbers and exact input outpoints. A star indicates structural connectivity,
**not common ownership, stolen-value allocation, or proof that confidential
inputs/outputs contain the same asset**. Star detection makes no API requests.

Miro stars are small transparent shape objects containing a star glyph. Their
identities are derived from the transaction, not its index or coordinates. They
are excluded from ELK topology and re-anchor to the actual transaction corner on
sync, including resized/rotated boxes; they do not follow a manual drag until the
next sync. Very small transaction boxes are rejected rather than drawing a badge
outside them. Local SVG shows a polygon star in the same corner. Mermaid's own
layout uses an inline star in the transaction heading. Stars have no connectors.

## Existing investigations and safety

Pull the development branch and restart the application, then use **Sync to
Miro** to refresh names, stop indicators, register cards and eligible stars.
Retracing and address-merge migration are not required. Normal sync retains
transaction positions and IDs and preserves manual labels. Reorganization is
needed only when new automatic graph positions are desired. Regenerate already
saved previews: an approved compact preview remains a frozen artifact.

Historical rules are read without rewriting their files. Candidate and
corroborated confidence are conservatively interpreted as suspected; confirmed
stays confirmed. Existing explicit stop flags are preserved. Missing historical
flags use the historical default once during normalization. New rules have no
classification field. A later edit records the original previous value in the
audit history, including old fields. Evidence archives are never migrated in
place.

Generated annotation creations, updates and removals use the existing durable
sync journals. An interrupted operation must be retried with the same intended
presentation. Unknown outcomes are reconciled rather than blindly repeated.
Only dedicated, proven annotation identities can be retired; ordinary graph
objects cannot be reclassified as disposable notes or stars. This is not an
atomic cross-item Miro transaction or an automatic undo mechanism. No live board
was modified as part of development; tests use synthetic traces and transports.

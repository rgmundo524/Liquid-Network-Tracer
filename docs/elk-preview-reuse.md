# Reuse an existing ELK preview during Miro sync

Ordinary **Sync to Miro** now checks completed full-graph ELK previews under the
investigation's `previews/` directory before launching another layout calculation.
A matching preview reports **Reusing the completed ELK layout; no recalculation**.
Previews made before the current search revision 3, including the failed-attempt
outcome metadata, are no longer reused. The earlier connector-label spacing,
input ordering, horizontal spacing, change-output and small-jog corrections remain included.
The current branch-organization revision 2 is also required, including the new branch-boundary candidate ordering and quality measurements.
Horizontal-spacing revision 2 reduces the default gap between object columns from 200 to 150 units. Older spacing previews must be refreshed. Regenerate older **Compact graph** previews too, because their saved clearance rules describe the previous spacing.
Choose **Refresh layout preview** to save a compatible replacement.
Keep the entire preview directory, not just its SVG: graph.json,
layout-report.json, graph.svg and the completed graph.html are required.

The saved run, case/network namespace, node/edge identities, labels, dimensions,
colors, service settings, fee visibility, connector style, label-layout and
input-order and horizontal-spacing revisions, search version, attempt count and deterministic seed sequence must match. The current graph is rebuilt from verified archived
evidence before comparison. Sequential and parallel searches with the same candidate settings are compatible; changing worker count or available RAM does not invalidate a completed preview.
Incomplete, stale, malformed or symlinked previews are ignored. An unmatched
request still uses ELK, without imposing new time/size limits or a fallback engine.
Custom external `--out` directories are not searched automatically.

A completed search can contain failed worker attempts. ELK still tries the full
requested seed sequence and saves the best verified candidate from successful
attempts, with visible success/failure counts. That completed preview is reusable
when its outcome counts and failed-seed records are consistent and its selected
seed succeeded, as well as matching the usual graph and display settings. Reuse
keeps the warning; it does not silently rerun failed seeds. A failed seed never
removes objects or connections from the graph. If every attempt fails, no new
preview is produced. Setup errors, invalid input, malformed results and
cancellation remain fatal and cannot produce a partial search preview.

One seed attempt can compare two layouts: ELK's initial geometry and an optional
rerun that places traced inputs first. If the rerun cannot preserve the requested
attachment order, the initial geometry remains eligible after full validation.
The rejected rerun is also validated, so an ordering mismatch cannot hide malformed
output. This recovery works with **one layout attempt** and does not require another
seed. Progress reports the rejected ordering; a selected initial layout records a
notice in its preview. Dense graphs can still have coincident attachment positions
or crossing connectors. An ordering rejection is not a reported memory failure.

This reuses layout geometry, not previously published Miro objects. Normal sync
still validates the destination mapping and preserves manual positions and edits.
It does not upload the SVG as one image or rewrite tracing archives. A full-graph
preview is not a starter-connections preview or a compact-layout approval.

For a **Starter connections** chart, use **Publish starter connections to Miro**
(or the browser's publication controls inside that panel). That action already
publishes its reviewed snapshot without rerunning ELK. Ordinary **Sync to Miro**
always targets the full trace, not a filtered connection-only chart.

Progress now distinguishes preparing ELK input, measuring the input layout,
running the ELK worker, validating its returned coordinates, measuring the output,
and building the Miro publication plan. During the worker calculation it reports
object/connection counts, per-worker and shared Node heap budgets, active worker count, elapsed time, and the current layout attempt out of the configured total. Parallel attempts can report progress in a different order, while final scoring remains in seed order. These are activity
indicators, not an estimated completion percentage. No credentials or arbitrary
worker/API text are copied into progress messages. A failed attempt reports a
fixed warning and continues; completion reports how many attempts succeeded and
failed. Saved failure records contain only attempt indexes, seeds and fixed
error codes. Unknown engine failures remain undiagnosed rather than being
reported as confirmed memory errors.

# Reuse an existing ELK preview during Miro sync

Ordinary **Sync to Miro** now checks completed full-graph ELK previews under the
investigation's `previews/` directory before launching another layout calculation.
A matching preview reports **Reusing the completed ELK layout; no recalculation**.
Previews made before connector-label spacing, input ordering or horizontal-spacing
correction, or the configurable seed search, are no longer reused.
Choose **Refresh layout preview** to save a compatible replacement.
Keep the entire preview directory, not just its SVG: graph.json,
layout-report.json, graph.svg and the completed graph.html are required.

The saved run, case/network namespace, node/edge identities, labels, dimensions,
colors, service settings, fee visibility, connector style, label-layout and
input-order and horizontal-spacing revisions, search version, attempt count and deterministic seed sequence must match. The current graph is rebuilt from verified archived
evidence before comparison.
Incomplete, stale, malformed or symlinked previews are ignored. An unmatched
request still uses ELK, without imposing new time/size limits or a fallback engine.
Custom external `--out` directories are not searched automatically.

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
object/connection counts, Node heap budget, elapsed time, and the current layout attempt out of the configured total. These are activity
indicators, not an estimated completion percentage. No credentials or arbitrary
worker/API text are copied into progress messages.

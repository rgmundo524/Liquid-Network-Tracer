# Per-name colors and selected-seed priority

After importing attributions, choose **Assign name colors**. This button is in
both the terminal import screen and the browser import panel. It is also
available in the terminal investigation menu and Address review. In the browser,
open **Import attributions** or **Address review**, then **Assign name colors**.
No saved run, credentials, or blockchain lookup is required.

The menu groups existing attribution names case-insensitively. For example,
`BTSE`, `btse`, and `BtSe` share one assignment, while their original spelling is
preserved in graph labels and evidence. Unicode casefold is used consistently
for searching, grouping, saving, and rendering. The automatically generated
`Suspected ` prefix is not part of the color key. `Perp` and `Perpetrator` are
different names, not automatic aliases; assign each the same color if desired.
Addresses are never lowercased or merged because their names match.

Select a name and choose its color. The browser provides a color picker and hex
field; the terminal offers a palette and hex field. Save with **Save color**.
Use **Clear assignment** to restore the usual tracing-role colors. Custom colors
use `#RRGGBB`; text is light or dark as needed for readability. Assignments are
case-local and persist across application restarts and later imports. Newly
imported addresses with an already configured name inherit its color. Disabled
assessments are counted separately and do not color the displayed graph.

The six-column attribution import format is unchanged:

```csv
Address,Name,confidence,stop_tracing,source,notes
```

No special color is automatically assigned for a perpetrator, a service, or a
confidence level. **Suspected** still appears before a suspected name; confirmed
names have no prefix. Confidence, stop flags, source, notes, and the starting
transaction convergence detection keep their existing meanings. Convergence now
uses a thick red transaction border rather than a separate star.

## Color priority

1. **Selected seed outputs: red**, even when named, attributed, observed unspent,
   or configured as a tracing stop. A shared-address node remains red whenever
   any of its Liquid output occurrences is an explicitly selected seed.
2. **Assigned name color**, regardless of confidence.
3. **Unspent endpoint: orange**, when backed by the existing observation rules.
4. **Reachable candidate: yellow**.
5. **Context address: light gray**.

Starting transaction boxes remain purple. This is a rule for address circles,
not transaction colors. Fee and event colors and connector colors are unchanged.
The graph's `role` retains its tracing meaning; `color_source` identifies whether
the displayed color came from that role or a name assignment. Exact UTXO records,
address identities, and edges are never merged or expanded by coloring a name.

Attribution names, STOP TRACING indicators, unspent evidence labels, sources,
notes and local-register references stay present when seed red wins. Miro no
longer creates register cards or shows their A- references. A red seed still
obeys its active tracing-stop rule: color priority is not a seed-stop override.

If independent assessments on one displayed address have conflicting assigned
colors, no name is selected by confidence or input order. The normal trace-role
color remains and the local attribution register reports the conflict. Several names
with the same assigned color are compatible. Graph JSON and node CSV include the
assigned name colors, conflict flag, displayed color, and color source.

## Existing investigations

Pull the development branch, restart the application, and assign the colors.
Then choose **Sync to Miro** or regenerate a preview. No new trace or reorganization
is required. Existing node identities and positions are preserved on normal sync.
Miro colors manually edited outside the tool remain protected by its usual
manual-edit conflict rules. A saved compact preview is a frozen artifact; changing
colors invalidates its approval, so regenerate it before applying.

The name-color map is saved as `name_colors` in the case's `services.json`, with
an audit entry for each changed batch. It is separate from the per-address rules:
saving a color does not change their confidence, name spelling, stop flags,
observation dates or timestamps. Existing run archives are not rewritten.
Changes use the existing locks and revision checks. Importing assessments or
saving colors in another window invalidates a stale color editor; refresh it
before saving. Identical assignments are no-ops. Removing the last assessment
using a name does not silently delete its saved palette assignment; it can be
cleared explicitly or reused by a later import.

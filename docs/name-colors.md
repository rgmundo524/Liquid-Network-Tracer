# Graph-role and imported-name colors

Open **Assign name colors** from the terminal investigation menu, Import
attributions, or Address review. The editor now contains both graph-role colors
and imported-name colors. No run, import, credentials, or blockchain lookup is
required to configure graph roles.

In the **browser**, use the **Graph role colors** table: choose a color using the
picker or enter `#RRGGBB`, then select **Save color**. **Reset to default** clears
only that role's override. The **Imported name colors** table remains separate.

In the **terminal**, use **Or choose a graph role**, select the category, then use
the existing palette or hex field and **Save color**. **Clear / reset to default**
restores its default. Selecting a name in the table returns the editor to name
assignments. These settings are per investigation, not global.

| Graph role | Default fill | Stored role key |
| --- | --- | --- |
| Seed addresses | Red, `#f16c7f` | `seed` |
| Seed / starting transactions | Purple, `#c4b5fd` | `starting_transaction` |
| Child / downstream transactions | Blue, `#a6ccf5` | `transaction` |
| Context addresses | Light gray, `#f5f6f8` | `address` |
| Child / reachable addresses | Yellow, `#fff9b1` | `candidate` |
| Unspent | Orange, `#fdba74` | `unspent_endpoint` |
| Events, fees and unspendable outputs | Pink, `#ea94bb` | `event` |

These palettes configure node fills. The optional **Color arrows by attribution**
setting also uses imported-name colors for arrows, as described below. Border colors,
border widths, node shapes, and convergence detection are unchanged. Shared-address highlights
remain border-only; the removed SHARED ADDRESS node text is not restored.

## Color arrows by attribution

In **Investigation settings**, enable **Color arrows by attribution** and save.
This option is off by default and is saved separately for each investigation.
Assign the desired name colors using **Assign name colors**, then regenerate a
preview or use normal **Sync to Miro**. No new tracing run or graph reorganization
is required.

Both arrows entering a named Liquid address and arrows leaving it use that
address's assigned name color. For example, a transaction receiving from a blue
service and paying a green service has a blue input arrow and a green output
arrow. Colors apply to the adjacent address connections; they do not spread
through subsequent transactions or imply an allocation of funds.

Selected seed circles keep their seed color, while their arrows can show the
assigned attribution color. Arrows without an assigned name color, or with
conflicting name colors, retain their normal traced/context color. Traced and
context arrows retain their existing widths. Turning the option off restores
the normal arrow colors on the next preview or sync. Manually edited Miro
connector styles remain protected by the usual sync conflict checks.

The setting applies to Miro, Mermaid, basic SVG, and ELK previews. Current previews
and normal sync use the current setting; archived runs retain their saved
presentation. Changing it requires a new compact preview before applying a
reviewed layout. The saved setting key is `run_defaults.color_attribution_arrows`.

## Priority and imported names

A selected seed address always uses the configured seed-address color, red by
default, even when attributed, observed unspent, or marked STOP TRACING. A merged
address remains a seed whenever any displayed Liquid occurrence is a selected
seed. Starting-transaction role similarly takes priority over downstream role.

For other Liquid addresses, an assigned name color overrides the role fill.
Without an applicable name assignment, precedence remains unspent,
reachable child/candidate, then context, using each role's configured color.
Colors never assign ownership, allocate value, change confidence, or override a
tracing stop. Text switches between light and dark for readability.

Name matching uses Unicode casefold, preserving original spelling and exact
addresses. `BTSE`, `btse`, and `BtSe` share an assignment. The generated Suspected
prefix is not part of the key. `Perp` and `Perpetrator` remain different names.
New imports inherit existing name assignments. Disabled assessments do not color
the graph. A name such as "Seed addresses" is a normal imported name, not a role
setting, and cannot collide with the separate role palette.

Conflicting assigned colors on independent assessments retain the applicable
role color and report the conflict in the local attribution register. Several
names with the same assigned color are compatible. Name and role assignments
are independent of confidence, sources, notes, observation dates and stop flags.
The attribution format includes an optional hop limit:

```csv
Address,Name,confidence,stop_tracing,hop_limit,source,notes
```

## Import name-group colors

Choose **Import CSV files** and select a UTF-8 name-color CSV. You can include
the attribution and change-output CSVs in the same batch; new attribution names
are processed before colors automatically. JSON and pasted contents remain
available through the advanced name-color importer. This assigns a color to each name group, including all capitalization
variants, without editing the address attributions themselves.

```csv
Name,Color
Perp,#f0abfc
Example Exchange,#93c5fd
Client wallet,#bbf7d0
```

The [CSV template](../examples/name-colors-template.csv) and
[JSON template](../examples/name-colors-template.json) are examples; replace the
names with those in your investigation. JSON uses an array of objects with
`name` and `color` fields. Column/field names and attribution-name matching are
case-insensitive. Colors must be six-digit hex values including `#`; short hex,
color names, and CSS expressions are rejected.

1. Preview the selected files to review each name's current color, requested color,
   and proposed action.
2. Existing assignments are kept by default. Choose the replacement policy
   and preview again to overwrite them. A blank CSV color or JSON `null` clears
   an existing assignment only with this replacement policy.
3. Review the preview, check the review checkbox, and apply the import.
4. Regenerate a preview or choose **Sync to Miro** to update the graph.

Names must exist in the attribution list, saved color assignments, or the
attribution CSV being applied in the same batch.
Unknown names are reported as errors so a typo cannot silently create an unused
group. Include the attribution CSV if the name is new. Identical duplicate
rows are combined; different colors for the same case-insensitive name are an
error. Any invalid row blocks the entire import. There is no partial save.

CSV imports identify `Name` and `Color` by their headers in any column order.
Unrelated columns, such as a spreadsheet `Duplicate count`, are ignored and are
not saved. Each required header must appear only once; names and colors retain
their normal validation. Duplicate rows are compared using the name and color,
so different helper values do not create a conflict. JSON imports still accept
only `Name` and `Color` fields. Graph-role colors, selected-seed priority,
confidence, stop flags, and saved trace evidence remain unchanged.
Imported name colors also apply to future addresses using the same name.

Choose **Export saved CSV** to download all saved name-color assignments in this
same format, including assignments outside the current search or page. Larger
exports contain numbered CSV parts in a ZIP. See [CSV input exports](input-csv-exports.md).

The CLI uses the same preview and apply flow:

```bash
liquid-trace name-color-import --case cases/my-case --file colors.csv --dry-run

# Use the approval_sha256 returned by the preview you reviewed.
liquid-trace name-color-import --case cases/my-case --file colors.csv \
  --approve-plan REVIEWED_APPROVAL_SHA256
```

To replace or clear existing assignments, add `--on-conflict replace` to both
commands. `--format auto|csv|json` selects the parser, defaulting to auto.
Files are limited to 512 KiB and 5,000 rows. Changing the file, import options,
or investigation settings invalidates the prior review. Apply rechecks the
whole batch under the existing case/trace locks and makes one atomic settings
write with an audit-history entry. Preview does not write, and apply never
starts a trace, reads credentials, or publishes to Miro.

## Persistence and existing diagrams

Role overrides are saved as `role_colors` beside `name_colors` in the case's
`services.json`. Both editors share the existing case/trace locks, revision
checks and audit history. Identical assignments are no-ops. An import or a save
in another editor makes an older editor stale; refresh before saving again.
Unused name assignments remain available for later imports until cleared.

After pulling the development branch, restart the application. Browser builds
must include the updated frontend. Save colors, then use normal **Sync to Miro**
or regenerate a preview. No retracing or reorganization is required. Normal sync
retains existing identities and positions and protects manually edited Miro
styles through the usual conflict checks. Changing colors does not directly
edit a live board.

Run archives retain their own presentation snapshot. Current previews and normal
sync use current case settings. Palette changes invalidate a reviewed compact
preview, so regenerate it before applying. Miro, Mermaid, basic SVG, and ELK SVG
share the resolved node fills. Legends show a colored circle, a short label, and
a brief definition for each graph role and each assigned attribution name used
in the displayed graph. The circles use the actual configured colors. Longer
evidence notes are separate from the color key in local previews.

Normal **Sync to Miro** updates the existing generated legend. If it needs more
space, it grows upward with clearance from the graph. Manually edited legend
content, font size, or dimensions are preserved. Large color keys use additional
legend pages; obsolete generated pages are removed on a later sync. These pages
do not change tracing or activity groups.

Graph JSON retains the settings snapshot, and node CSV includes displayed
color and `color_source` (`role_palette` for a role override, `name` for a name
assignment). Existing tracing roles and evidence remain unchanged.

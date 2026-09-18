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

Only fills are configured here. Connector colors, border colors, border widths,
node shapes, and convergence detection are unchanged. Shared-address highlights
remain border-only; the removed SHARED ADDRESS node text is not restored.

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
share the resolved node fills; legends show configured hex values for custom
roles. Graph JSON retains the settings snapshot, and node CSV includes displayed
color and `color_source` (`role_palette` for a role override, `name` for a name
assignment). Existing tracing roles and evidence remain unchanged.

# Center a named address group

Open **Investigation settings**, enter an attribution name in **Center named group**, and save. The browser suggests matching active names from the investigation as you type. Name matching ignores capitalization. The same field is available in terminal settings. Workspace settings set the default for new investigations only.

Generate an **ELK layout preview** to inspect the arrangement, then use **Sync and reorganize** to apply it to an existing Miro board. Leave the field blank to return to the normal layout. Changing this setting makes previous ELK and compact previews stale, so regenerate them before applying a layout.

This option favors a central arrangement of the selected group's individual addresses and transactions between them, with other activity branching around that structure. Transaction direction, address identities, connections, tracing limits, and fetched evidence remain unchanged. It does not start a trace or fetch additional transactions. Mermaid uses its own arrangement.

Addresses explicitly listed in **Separate branch hubs** retain their hub placement, and explicit change-output rows take priority where alignment conflicts. If the current graph contains no matching addresses, it keeps its normal layout. The preview reports how many address objects and connecting transactions matched. Dense graphs, shared addresses, and disconnected activity can prevent a perfectly straight central tree; the layout still preserves every existing connection.

# Export saved CSV inputs

Choose **Export input CSVs** in an investigation to download all three input
tables together as `input-csvs.zip`. This is available before the first trace
and after saved runs. Each corresponding import/editor screen also has an
**Export saved CSV** button for its own table.

Exports contain the current saved entries across every page. Save any form edits
first if you want them included. Searching or filtering an editor does not
restrict the export. Exporting does not start a trace, look up blockchain data,
or change the investigation or Miro board.

| File | Contents |
| --- | --- |
| `attributions.csv` | Address, name, confidence, stop flag, hop limit, source, notes, observation date, and enabled state. Disabled assessments are included. |
| `name-colors.csv` | Each saved name-color assignment, once per case-insensitive name group. |
| `change-outputs.csv` | Transaction hash, designated change vout, and notes. |

The attribution export includes `observed_at` and `enabled` columns in addition
to the basic import template, so editing and reimporting does not silently enable
a disabled assessment or lose its observation date. Blank hop limits stay blank;
zero remains zero. Name-color exports contain assigned colors, including saved
assignments whose names are currently unused. Graph-role colors have no CSV
import format and are not part of these input tables.

A retained color for a name with no remaining attribution can be reimported into
the same investigation. When transferring to another investigation, that name
must already be known there or the color importer will reject its row.

Individual exports are CSV files when they fit the import limits. Larger tables
are split into numbered CSV parts inside a ZIP, with each part limited to
5,000 rows and 512 KiB including its header. Every saved row is included. An
empty table exports its headers so you can fill it in.

To edit and import again:

1. Download the table or ZIP. Extract ZIP files before editing or uploading.
2. Edit the CSV while preserving its headers and full addresses or transaction
   hashes. Import text columns as **Text** in your spreadsheet so their contents
   stay literal. Text containing commas, quotes, or line breaks is CSV-quoted.
3. Open the matching importer and preview the edited CSV. Import split parts
   individually. When transferring tables, import address attributions before
   name colors; the color importer requires names already known to the
   investigation.
4. Choose replacement if you intend to change existing entries, review the
   proposed changes, and apply.

Import remains an add/update operation. Removing a CSV row does not delete a
saved entry. Use the existing disable/clear controls or the importer's explicit
clear fields when you intend to remove a designation. An empty header-only file
has no rows to import until you add some.

The browser downloads directly. The terminal saves a new file under the
investigation's `exports/inputs-<id>/` directory and displays its full path.
Repeated exports create new directories, preserving previous exports.

## Command line

```bash
liquid-trace input-export --case cases/my-case
liquid-trace input-export --case cases/my-case --kind attributions
liquid-trace input-export --case cases/my-case --kind name-colors
liquid-trace input-export --case cases/my-case --kind change-outputs
```

The default kind is `all`. Add `--out NEW_DIRECTORY` to choose a new destination
outside the archived runs. The command reports the saved path, counts, and the
settings revision used for the export.

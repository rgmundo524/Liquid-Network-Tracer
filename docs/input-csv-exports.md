# Import and export CSV inputs

## Import CSV files together

Open an investigation and choose **Import CSV files**. In the browser, select
the attribution, name-color, and change-output CSVs in one file picker. In the
terminal, paste their full paths into the same box, one path per line. You can
import just one file or up to three, with one file per input type.

The importer detects each file from its column headers, so filenames and
selection order do not matter. Newly imported attribution names are available
to the color file in the same batch. Both interfaces offer optional type
selectors if a file's headers match more than one format.

1. Select the files. Keep existing entries is the default conflict policy.
2. Choose **Replace** for the entries you intend to update. This is necessary
   when changing an existing address's `hop_limit` or `stop_tracing` value.
   The browser has a policy per file; the terminal and CLI apply one policy
   to all selected files.
3. Preview the batch and review its file summaries and individual changes.
4. Check the review box and apply all files once.

In the browser, selecting the same queued filename again keeps its chosen type
and conflict policy while loading the new contents. Review and approval are
required again. New filenames and other investigations still default to **Keep
existing**.

All files save together. An invalid row in any file blocks the whole batch,
and changing a file, import options, saved settings, or relevant saved evidence
requires a new preview. Imports do not start a trace or sync the Miro board.
Continue the investigation to extend tracing after changing tracing rules;
regenerate a preview or sync Miro to apply display changes.

Existing limits apply to each file: UTF-8 CSV, 512 KiB, and 5,000 rows. Extract
ZIP exports first, and import numbered parts in separate batches. Pasted text,
plain address lists, and JSON remain available through the advanced importers.

## Export saved inputs

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
must already be known there or be added by the attribution CSV in the same
batch; otherwise, the color importer will reject its row.

Individual exports are CSV files when they fit the import limits. Larger tables
are split into numbered CSV parts inside a ZIP, with each part limited to
5,000 rows and 512 KiB including its header. Every saved row is included. An
empty table exports its headers so you can fill it in.

To edit and import again:

1. Download the table or ZIP. Extract ZIP files before editing or uploading.
2. Edit the CSV while preserving the supported headers and full addresses or
   transaction hashes. Columns can be reordered, and unrelated helper columns
   such as `Duplicate count` can remain in the file. Import text columns as
   **Text** in your spreadsheet so their contents stay literal. Text containing
   commas, quotes, or line breaks is CSV-quoted.
3. Choose **Import CSV files** and select the edited tables together. Import
   split parts in separate batches. Attribution names are processed before
   colors automatically, regardless of file order.
4. Choose replacement if you intend to change existing entries, review the
   proposed changes, and apply.

All three CSV importers match supported columns by header and ignore unrelated
columns. Helper values are not saved or used to compare duplicate rows. Required
columns, recognized-header uniqueness, field validation, and malformed-row
checks still apply. JSON imports continue to reject unsupported fields. The
review binds the whole uploaded file, so editing even a helper value after the
preview requires previewing again before applying.

Import remains an add/update operation. Removing a CSV row does not delete a
saved entry. Use the existing disable/clear controls or the importer's explicit
clear fields when you intend to remove a designation. An empty header-only file
has no rows to import until you add some.

The browser downloads directly. The terminal saves a new file under the
investigation's `exports/inputs-<id>/` directory and displays its full path.
Repeated exports create new directories, preserving previous exports.

## Command line

```bash
liquid-trace input-import --case cases/my-case \
  --file attributions.csv --file name-colors.csv --file change-outputs.csv
liquid-trace input-import --case cases/my-case \
  --file attributions.csv --file name-colors.csv --file change-outputs.csv \
  --approve-plan APPROVAL_SHA256

liquid-trace input-export --case cases/my-case
liquid-trace input-export --case cases/my-case --kind attributions
liquid-trace input-export --case cases/my-case --kind name-colors
liquid-trace input-export --case cases/my-case --kind change-outputs
```

Imports preview by default. Add `--on-conflict replace` to both import commands
to update existing entries, and use the approval hash from that exact preview.
The existing single-type import commands also remain available. Use those
commands if CSV headers match multiple types and require an explicit choice.

The default export kind is `all`. Add `--out NEW_DIRECTORY` to choose a new destination
outside the archived runs. The command reports the saved path, counts, and the
settings revision used for the export.

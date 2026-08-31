---
name: pbi-extract
description: Open a Power BI file. Use when the user has a .pbix or .pbip (or a PBIP folder) and wants the data, the semantic model, or the report definition out of it - to query it, to rebuild the report elsewhere, to audit it, or to hand it to another agent. Produces parquet tables, a DuckDB database, model.json and report-spec.json.
---

# Get a Power BI file open

A `.pbix` is a zip. The part that matters is `DataModel`: an XPress9-compressed
Analysis Services backup holding the VertiPaq column store. A `.pbip` (File >
Save as > Power BI project) is the same model written as text - TMDL for the
semantic model, PBIR JSON for the report - and it usually keeps the identical
binary at `*.SemanticModel/.pbi/cache.abf`.

So there are two things to pull, and they come from different places:

| what | best source | fallback |
|---|---|---|
| row data | `DataModel` in the .pbix, or `cache.abf` in the .pbip | none - without one of these there is no data |
| tables, measures, hierarchies, sort-by columns | TMDL in the .pbip | metadata inside the binary |
| pages, visuals, filters, theme | PBIR in the .pbip, or `Report/definition` or `Report/Layout` in the .pbix | none |

## Do this

```bash
python3 tools/pbi_extract.py "<file.pbix | file.pbip | PBIP folder>" -o build/model
python3 tools/pbir_parse.py  "<same source>" -o build/model/report-spec.json
```

Then confirm what you got before building anything on it:

```bash
python3 tools/pbi_chat.py schema | head -60
python3 tools/pbi_chat.py measures
```

## What to check, and why

**Where the data actually lives.** The Power Query M in a partition usually
points at a path that does not exist on your machine - here, an Excel file on
another person's OneDrive. Do not try to refresh it. The rows you want are the
ones already imported into the model file.

**Auto date tables.** Power BI generates a hidden `LocalDateTable_*` per date
column plus a `DateTableTemplate_*`. The extractor drops them. If a count of
tables looks higher than the report suggests, that is why.

**Measures that will not translate.** `tools/pbi_chat.py measures` prints the
SQL for each DAX measure or the reason there is none. A missing translation is
a fact to report, not a thing to paper over: write the SQL by hand and say so.

**A model with no cached data.** Some .pbip saves omit `cache.abf`, and a
live-connection model has no embedded data at all. Both fail loudly. The fix is
to open the report in Power BI Desktop and re-save it.

## What you end up with

```
build/model/
  tables/<Table>.parquet   one per user table
  model.duckdb             every table, ready to query
  model.json               columns, types, relationships, hierarchies,
                           sort-by columns, measures with their DAX,
                           partition M, row counts
  report-spec.json         pages, visuals, positions, fields, filters,
                           saved slicer state, theme colours resolved to hex
```

From here: `pbi-replicate` rebuilds the report, `pbi-redesign` builds something
better, `pbi-chat` answers questions against it.

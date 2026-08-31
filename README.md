# Power BI to AI visuals

A working pipeline that takes a Power BI file and gives you back four things:
the data as an open queryable model, the report rebuilt faithfully in HTML, a
strategic dashboard an agent designed from what the data actually says, and a
command line an agent uses to answer questions about any of it.

Nothing here needs Power BI Desktop, a Fabric capacity, a gateway, or a
licence. It runs on a laptop against the file you already have.

```
.pbix / .pbip
      |
      |  1  extract          parquet + DuckDB + model.json
      |  2  report spec      report-spec.json
      |
      +--> 3  replica        build/replica/index.html      the same report, in a browser
      +--> 4  analysis       build/analysis/findings.md    what the data says
      |         + narrative  build/analysis/narrative.json what an agent makes of it
      |     5  strategic     build/strategic/index.html    the report they should have had
      |
      +--> 6  chat           tools/pbi_chat.py             ask it anything
```

## Run it

```bash
./setup.sh                    # once, vendors pbixray into ./vendor
./run_all.sh path/to/your.pbix
```

Then open `build/replica/index.html` and `build/strategic/index.html`, and try:

```bash
python3 tools/pbi_chat.py schema
python3 tools/pbi_chat.py query --dims "Product.Category" \
  --measures "Sum(Sales.Sales Amount)" --filters "Date.Fiscal Year in FY2019"
```

`run_all.sh` accepts a `.pbix`, a `.pbip`, or a PBIP folder. Given a `.pbix`
with a PBIP saved beside it, it takes data from the binary and metadata from
the TMDL, which is the best of both.

`build/` is generated and is not committed, so a fresh clone has no model in it
until you run the pipeline once. Every `build/...` path named below appears
after that first run.

**No Power BI file of your own?** The worked example throughout this README is
Microsoft's AdventureWorks Sales sample, published on the
[Power BI sample datasets](https://learn.microsoft.com/power-bi/create-reports/sample-datasets)
page. Download the `.pbix` from Microsoft and point `run_all.sh` at it; the
numbers quoted here should reproduce to the cent. The sample is Microsoft's and
is deliberately not redistributed here.

## What each stage does

**1. Extract** - `tools/pbi_extract.py`

A `.pbix` is a zip whose `DataModel` part is an XPress9-compressed Analysis
Services backup of the VertiPaq store. A `.pbip` keeps the same bytes at
`*.SemanticModel/.pbi/cache.abf`, and adds TMDL. Both paths are read; you get a
parquet file per table, a DuckDB database, and `model.json` carrying columns,
types, relationships, hierarchies, sort-by columns, measures with their DAX and
the original Power Query M.

On the sample: 8 tables, 263,566 rows, in about two seconds.

**2. Report spec** - `tools/pbir_parse.py`

Reads PBIR (`.pbip`, and `.pbix` files saved by recent Desktop builds) or the
legacy `Report/Layout` blob, and flattens both into one `report-spec.json`:
pages, visual positions, field bindings, sort definitions, visual-level filters,
the saved slicer selection, and theme colours resolved from palette indexes to
hex.

**3. Replica** - `tools/replicate_html.py`

Compiles that spec plus the data into a single self-contained HTML page. Not a
screenshot: the slicer works, because each visual ships a small pre-aggregated
cube and re-aggregates in the browser.

Every checked number matches the original render to the cent - see
[`docs/fidelity-report.md`](docs/fidelity-report.md), and re-run the check with
`python3 tools/verify_fidelity.py`.

**4. Analysis** - `tools/analyze_model.py`

Computes evidence and refuses to editorialise: period coverage, growth, margin,
mix, concentration, year-on-year movers, referential-integrity gaps, placeholder
members, partial periods, and which tables the report never touches. Business
roles (revenue column, date table, channel, and so on) are detected by name and
shape, and every guess is printed with its evidence so you can override it.

Output: `build/analysis/findings.json` and `findings.md`.

**5. Strategic dashboard** - `tools/strategic_html.py`

Merges that evidence with `narrative.json`, which an agent writes after reading
`findings.md`. The deliberate split: **numbers are computed, judgment is
written**, and the page prints the SQL behind itself in the footer.

The workflow that produces the narrative - analyst, strategist, designer,
builder, critic - is [`skills/pbi-redesign`](skills/pbi-redesign/SKILL.md).

**6. Chat** - `tools/pbi_chat.py`

A read-only CLI over the semantic layer: `schema`, `measures`, `roles`,
`sample`, `distinct`, `query`, `sql`. `query` resolves joins from the model's
own relationships, so an agent asks for fields instead of writing joins it can
get wrong. There is no language model inside it - the agent reading the question
is the language model.

## The skills

Four skills, in `skills/`. Copy them into `~/.claude/skills/` (available
everywhere) or `.claude/skills/` in a project:

| skill | when |
|---|---|
| [`pbi-extract`](skills/pbi-extract/SKILL.md) | get a `.pbix` or `.pbip` open |
| [`pbi-replicate`](skills/pbi-replicate/SKILL.md) | rebuild the report so people trust the extraction |
| [`pbi-redesign`](skills/pbi-redesign/SKILL.md) | design the dashboard they should have had |
| [`pbi-chat`](skills/pbi-chat/SKILL.md) | answer questions against the model |

They carry the traps, not just the commands - which is most of their value. See
[`skills/README.md`](skills/README.md).

## What the worked example found

The sample is Microsoft's AdventureWorks Sales report: three pages, six visuals,
a matrix, an area chart, a bar chart and a year slicer. Rebuilding it is the
easy half. Reading it turned up things the report cannot show:

- **73% of revenue passes through the reseller channel at roughly zero gross
  profit** at standard cost, while the online store runs at a steady 41% margin
  and carries the entire company margin. The report never uses the cost column
  or the channel column, both of which sit in the model it already loads.
- **Average order value fell from $7,461 to $2,206** - which is good news read
  correctly. Orders went from 3,198 to 23,519 because the online store expanded
  from bikes only to accessories and clothing. Internet accessory units went
  from 718 to 35,374 in one year.
- **The newest fiscal year stops on 15 June**, two weeks short. Any comparison
  that ignores that understates it.
- **The `(Blank)` in the year slicer is a data-quality signal.** 2,113 sales
  lines carry a ship date the calendar does not contain, through an inactive
  relationship nothing on the page uses.
- **Half the model is unread.** Customer, Sales Order, Sales Territory and Table
  are never referenced by any visual, and one of them holds the channel column
  that changes the whole story.

The full read lands in `build/analysis/findings.md` and the dashboard built
from it in `build/strategic/index.html`, both after your first run.

## Layout

```
tools/
  pbi_common.py         paths, vendor bootstrap, small helpers
  tmdl.py               a TMDL reader (indentation-structured, fenced expressions)
  pbi_extract.py        1  binary + TMDL -> parquet, duckdb, model.json
  pbir_parse.py         2  PBIR or Layout -> report-spec.json, theme resolved
  dax_sql.py               a small DAX -> SQL translator that refuses loudly
  semantic.py              joins, hierarchies, sort-by columns, measures, query()
  replicate_html.py     3  spec + data -> the replica page
  analyze_model.py      4  the evidence pass
  strategic_html.py     5  evidence + narrative -> the executive page
  pbi_chat.py           6  the read-only CLI an agent drives
  verify_fidelity.py       the regression test against the original render
  templates/            replica.css/js, strategic.css/js
skills/                 four Claude Code skills
docs/fidelity-report.md what matched, what did not, and why
build/                  everything the pipeline produces
vendor/                 pbixray + native codecs (created by setup.sh)
```

## Requirements and limits

Python 3.10+, with `pandas`, `duckdb` and `pyarrow` on the system interpreter.
`setup.sh` vendors `pbixray` and its native codecs into `vendor/` so nothing
else is installed globally. The vendored wheels are platform-specific: re-run
`setup.sh` on a different OS or architecture.

Known limits, all of them deliberate:

- **DAX translation covers a subset.** Aggregates, arithmetic, `DIVIDE`,
  `CALCULATE` with a simple filter or `USERELATIONSHIP`. Anything else is
  reported as untranslatable with its reason rather than guessed at. Write the
  SQL by hand and say so.
- **Client-side filtering needs additive measures.** The replica marks a visual
  holding an average or a distinct count as not following the slicer, and
  renders it at the report's saved state.
- **No cross-highlighting, drill-down or bookmarks** in the replica.
- **A model with no cached data cannot be extracted.** Live connections and
  DirectQuery hold no rows locally; re-save from Desktop with import mode.
- **Role detection is heuristic.** It reads column names. Check
  `python3 tools/pbi_chat.py roles` before trusting anything downstream of it.

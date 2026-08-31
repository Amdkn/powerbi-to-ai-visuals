# Power BI to AI visuals (agent orientation)

Once a Power BI file has been extracted here, you can answer questions about it
directly: there is a real data model on disk, so never estimate a number you
could query.

**Check first.** `build/` is generated and is not committed, so in a fresh clone
it is empty and there is nothing to query yet. If `build/model/model.json` is
missing, the answer is `./setup.sh` then `./run_all.sh <file.pbix>`, not a
guessed number.

## What is on disk

| Path | What |
|---|---|
| `build/model/` | the extracted model: a parquet per table, `model.duckdb`, `model.json` |
| `build/analysis/findings.md` | the evidence pass: growth, margin, mix, concentration, data quality |
| `build/replica/index.html` | the original report rebuilt, verified to match to the cent |
| `build/strategic/index.html` | the dashboard designed from the evidence |
| `build/model/report-spec.json` | pages, visuals, field bindings, filters, theme |
| `docs/pipeline-notes.md` | the traps, written up. Read it before trusting a surprising number |

Run `python3 tools/pbi_chat.py schema` to see what is actually loaded rather
than assuming. The worked example in the docs is Microsoft's AdventureWorks
sample: 8 tables, 263,566 rows, fiscal years FY2018 to FY2020. Every number
quoted below comes from that model, so if a different file is loaded, treat
them as illustrations of the traps and not as facts about the data in front
of you.

## Answering a question about the data

`tools/pbi_chat.py` is the interface. It is read-only: the connection is opened
read-only, statements are checked against a deny list, results are capped. You
cannot damage the model through it.

```bash
python3 tools/pbi_chat.py schema                    # tables, columns, relationships
python3 tools/pbi_chat.py measures                  # each DAX measure and its SQL
python3 tools/pbi_chat.py roles                     # revenue / cost / date / channel columns
python3 tools/pbi_chat.py distinct "Product.Category"
python3 tools/pbi_chat.py query \
  --dims "Product.Category" \
  --measures "Sum(Sales.Sales Amount)" \
  --filters "Date.Fiscal Year in FY2019" --format md
python3 tools/pbi_chat.py sql "SELECT ..." --format md   # escape hatch, SELECT only
```

`query` builds the joins from the model's own relationships, so ask for fields
and let it find the path. Reach for `sql` only when the semantic layer cannot
express the question.

Add `--model <dir>` to point at a different extraction. One copy of `tools/`
can talk to any number of extracted models.

## Four ways to be confidently wrong here

These are not hypothetical. Each one produces a plausible number rather than an
error, and each one has bitten this model.

1. **A visual-level filter.** One visual in the source report excludes
   `[Not Applicable]` reseller business types, and its subtotals are 27% lower
   than the same query without it. If the user quotes a number off the report
   and yours differs, look in `report-spec.json` before explaining the gap.
2. **Placeholder members.** `[Not Applicable]` on Customer carries 73% of
   revenue and the same member on Reseller carries the other 27%. They are the
   two sales channels, not dirty data. Any ranking of a dimension has to drop
   its placeholder first, and say so. "Top 10 customers are 73% of revenue" is
   a real query result and complete nonsense.
3. **Sort-by columns.** `Product[Category]` sorts by `Product[Sorting]`, which
   is why the report reads Bikes, Components, Clothing, Accessories and not
   alphabetically. `query` carries the sort key. Raw SQL does not.
4. **The partial period.** FY2020 stops on 15 June. Raw year-on-year
   understates growth by about three points. Compare only the months both
   years contain, and say that you did.

## House rules

- **Reconcile before explaining.** If the user has a number from their report,
  reproduce it first. A difference is nearly always a filter you have not
  applied yet.
- **Quote the query.** Give the number, then the SQL that produced it, via
  `--show-sql`. That is the difference between an answer and a claim.
- **Never hand-edit anything under `build/`.** It is generated. Re-run
  `./run_all.sh <file>` instead.
- **`tools/verify_fidelity.py` is the regression test.** Run it after touching
  anything in `tools/`. All 22 checked values must still match.

---
name: pbi-chat
description: Answer questions against an extracted Power BI model. Use after pbi-extract when someone asks anything about the numbers in their Power BI file - totals, breakdowns, trends, "why is this different from the report" - and you need to query the real data rather than guess. Wraps a semantic layer that builds the joins from the model's own relationships.
---

# Talk to the model

`tools/pbi_chat.py` is the whole interface. There is no language model inside
it: you are the language model, and it gives you a safe way to reach the data.
Every path is read-only.

## Start by looking

```bash
python3 tools/pbi_chat.py schema        # tables, columns, hierarchies, relationships
python3 tools/pbi_chat.py measures      # each DAX measure and its SQL, or why there is none
python3 tools/pbi_chat.py roles         # revenue / cost / date / channel columns, if analysed
python3 tools/pbi_chat.py distinct "Sales Order.Channel"
```

## Then ask

```bash
python3 tools/pbi_chat.py query \
  --dims "Product.Category,Reseller.Business Type" \
  --measures "Sum(Sales.Sales Amount)" \
  --filters "Date.Fiscal Year in FY2019" \
  --filters "Reseller.Business Type not in [Not Applicable]" \
  --show-sql --format md
```

`query` resolves joins from the model's relationships, so ask for fields and
let it find the path. Field references:

- `Table.Column`
- `Table.Hierarchy.Level` - e.g. `Date.Fiscal.Month`
- `Agg(Table.Column)` - `Sum`, `Avg`, `Min`, `Max`, `Count`, `DistinctCount`
- a measure by name - e.g. `Sales.Sales Amount by Due Date`

Anything the semantic layer cannot express goes through raw SQL, which is
capped and rejects everything but `SELECT` / `WITH` / `DESCRIBE` / `SHOW`:

```bash
python3 tools/pbi_chat.py sql "SELECT ... FROM Sales JOIN Date ON ..." --format md
```

## Answering well

**Reconcile before you explain.** If the user has a number from the report,
reproduce it first. A difference is nearly always a filter you have not applied
yet - visual-level filters live in `report-spec.json` and are easy to miss.

**Respect sort-by columns.** `Product[Category]` sorts by `Product[Sorting]`,
which is why Power BI shows Bikes, Components, Clothing, Accessories rather
than alphabetical order. `query` carries the sort key automatically; if you
write raw SQL, order by it yourself.

**Watch for role-playing dates.** A measure wrapped in `USERELATIONSHIP` joins
the date table down a different key. `measures` prints which one. Two measures
on one axis can legitimately mean two different join paths.

**Exclude placeholder members before ranking.** `[Not Applicable]`, `NA`,
`Unknown` and a `-1` key are how a star schema says "no member". In this model
one `[Not Applicable]` customer carries every reseller sale, so any top-customer
list is meaningless until it is dropped. Say that you dropped it.

**Quote the SQL.** Answer with the number, then the query that produced it.
Anyone can then check you, which is the difference between an answer and a
claim.

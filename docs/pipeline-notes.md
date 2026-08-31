# Notes from building this

Findings that cost time to discover and are not written down anywhere obvious.

## The data is in the file, but not where the file says

Every table's Power Query M in the sample points at
`C:\Users\<redacted>\OneDrive - Microsoft\...\AdventureWorks Sales.xlsx`, a path
on the authoring machine of whoever built the sample, back in 2020. (The real
username is in the file; there is no reason to reprint it here.) It cannot be refreshed and does not need
to be: the imported rows are inside the model, in the `DataModel` part of the
`.pbix` (and in `cache.abf` inside a saved `.pbip`).

Both are XPress9-compressed Analysis Services backups. `pbixray` decodes them
with no Analysis Services engine, no Power BI Desktop and no Windows - which is
what makes this pipeline runnable on any machine. A `.pbip` with no `cache.abf`
is a genuine dead end; re-save it from Desktop.

## ThemeDataColor is an index into a palette that starts two slots late

A visual stores `{"ThemeDataColor": {"ColorId": 6, "Percent": 0}}`, and
`dataColors[6]` is the wrong colour. Slot 0 is the theme background and slot 1
the foreground, so:

```
hex = dataColors[ColorId - 2]
```

Confirmed twice against the rendered report: `ColorId 6` gives `#6B91C9` (the
header bar) and `ColorId 5` gives `#DD6B7F` (the due-date series). `Percent`
tints negative and shades positive.

## The three things that silently change a number

**A visual-level filter.** In the sample, one of three visuals excludes
`[Not Applicable]` reseller business types. Its subtotals are 27% lower than the
same query without it, and nothing on the page says so.

**A sort-by column.** `Product[Category]` sorts by `Product[Sorting]`, a
calculated column of `RELATED('Table'[Sorting])` pointing at a four-row helper
table. It is the only reason the order is Bikes, Components, Clothing,
Accessories. Any rebuild that sorts alphabetically is visibly wrong to whoever
uses the report daily.

**The saved slicer state.** A report opens filtered. Rebuild it unfiltered and
every number differs from the screenshot the user is holding.

## USERELATIONSHIP is a different join, not a different filter

`CALCULATE(SUM(Sales[Sales Amount]), USERELATIONSHIP(Sales[DueDateKey], 'Date'[DateKey]))`
re-routes the date join for that measure only. Both the axis and any slicer
filter travel down the re-routed path.

The clean way to express it in SQL is one aggregate per join path, joined on the
dimensions:

```sql
WITH q0 AS (SELECT Date.Month, SUM(...) FROM Sales JOIN Date ON Sales.OrderDateKey = Date.DateKey ...),
     q1 AS (SELECT Date.Month, SUM(...) FROM Sales JOIN Date ON Sales.DueDateKey   = Date.DateKey ...)
SELECT COALESCE(q0.Month, q1.Month), q0.v, q1.v
FROM q0 FULL OUTER JOIN q1 ON q0.Month IS NOT DISTINCT FROM q1.Month
```

which is what `semantic.py` generates when a query mixes measures with different
relationship overrides.

## A blank member in a slicer is a bug report

Power BI adds a blank row to a dimension when a fact table holds keys the
dimension does not have - and inactive relationships count. In the sample, 2,113
`Sales` rows carry a `ShipDateKey` with no matching `Date[DateKey]`, through a
relationship no visual uses. The only visible symptom is `(Blank)` at the top of
the year slicer.

Anyone who has ever wondered why a slicer has a blank entry has been looking at
a referential-integrity gap without knowing it.

## Placeholder members are the shape of the business

`[Not Applicable]` on Customer carries 73.26% of revenue and `[Not Applicable]`
on Reseller carries 26.74%. They are not dirty data. They are the two channels:
a reseller sale has no customer, an internet sale has no reseller.

The consequence is sharp - "top 10 customers = 73.4% of revenue" is a real
output of a real query and complete nonsense. Any ranking of a dimension has to
exclude its placeholder first, and say that it did.

## Re-aggregating in the browser is only safe for additive measures

Shipping a pre-grouped cube and summing it client-side gives a live report with
no server. It is exactly wrong for an average or a distinct count. Two rules
kept it honest here:

- the replica marks non-additive visuals and renders them at the report's saved
  filter state rather than following the slicer
- the strategic dashboard limits filters to fiscal year and channel, both
  order-level attributes, so distinct order counts stay exact under every
  combination - checked, not assumed: no order in this model spans two months or
  two channels

A filter that silently breaks a KPI is worse than a filter that is not offered.

## Comparing to a partial period

The sample's latest fiscal year stops on 15 June, two weeks short. Raw
year-on-year understates it by about three points (+52.3% against +55.1%
excluding June from both sides). The dashboard numbers each month inside its
fiscal year and compares only the months both years contain, and says so on the
page. Getting this wrong in the flattering direction is the more common failure.

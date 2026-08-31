---
name: pbi-replicate
description: Rebuild a Power BI report as a self-contained HTML page that matches the original. Use when someone wants their report out of Power BI and running in a browser, or wants to prove an extraction is faithful before building anything new on it. Compiles from the report definition, so layout, filters, sort order, theme colours and the saved slicer state all carry over.
---

# Rebuild the report, faithfully

The point of this step is trust. Before anyone accepts a redesign, they need to
see the same report, with the same numbers, outside Power BI. So the page is
*compiled from the report definition* - never eyeballed from a screenshot.

```bash
python3 tools/replicate_html.py --model build/model --out build/replica
```

Requires `build/model/model.json` and `build/model/report-spec.json` from
`pbi-extract`.

## What the compiler carries across, and why each one bites

**Visual-level filters.** In the sample report the matrix filters out
`[Not Applicable]` business types and the two charts beside it do not. Miss it
and every subtotal in that visual is wrong while the rest of the page looks
right. Filters live per visual in `report-spec.json`.

**The saved slicer selection.** A report ships filtered. This one opens on
FY2019. Rebuild it unfiltered and every number differs from the screenshot the
user is holding.

**Sort-by columns.** `Product[Category]` sorts by a `Sorting` column pulled
from another table with `RELATED`. That is the only reason the order reads
Bikes, Components, Clothing, Accessories. Alphabetical order is the tell that a
rebuild was done by eye.

**Theme colour indexes.** A visual stores `ThemeDataColor: {ColorId, Percent}`,
not a hex value. `ColorId` indexes a palette whose slot 0 is the theme
background and slot 1 the foreground, so the data colours start at slot 2:
`dataColors[ColorId - 2]`. Verified on this report - the header shape asks for
`ColorId 6` and renders `#6B91C9`, the fifth data colour.

**Explicit column widths.** Power BI saves them. The matrix's value column is
893 px wide, which is what puts the data bars where the original has them.

**Role-playing dates.** Two series on one axis can join the date table down
different keys. `Sales Amount` uses the active `OrderDateKey`; `Sales Amount by
Due Date` re-routes to `DueDateKey` through `USERELATIONSHIP`. The builder runs
one aggregate per join path and joins them on the axis.

## Live, not a picture

Each visual ships a small cube - its own fields, grouped, plus the column the
page's slicer filters on - so clicking the slicer re-aggregates in the browser.

That is only sound for additive measures. A visual holding an average or a
distinct count is marked `followsSlicer: false` and renders at the report's
saved filter state instead of quietly returning a wrong number. If you extend
the renderers, keep that rule.

## Check it before you show it

Reproduce a few cells against the source with `pbi-chat` and compare to the
original report or a screenshot of it. Then write down what did not match:
`docs/fidelity-report.md` in this project is the worked example, including the
one thing that does not match and why.

## Extending

Renderers live in `tools/templates/replica.js`, keyed by Power BI's
`visualType`: `areaChart`, `barChart`, `columnChart`, `pivotTable`, `tableEx`,
`slicer`, `textbox`, `basicShape`. Anything else renders as a labelled
placeholder rather than silently disappearing - so an unsupported visual is
visible in the output, which is the behaviour you want when a stakeholder is
looking for their chart.

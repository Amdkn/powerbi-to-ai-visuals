# Fidelity report

How close the rebuilt report is to the Power BI original, what was checked, and
the one thing that does not match.

Reproduce it any time:

```bash
python3 tools/verify_fidelity.py --model build/model
```

Ground truth is the report as Power BI Desktop rendered it, page 1, with the
slicer where the report was saved (FY2019). Every expected value below is read
off that render and lives in `tools/verify_fidelity.py`, so this doubles as a
regression test: exit code 1 on any mismatch.

## Numbers: 22 of 22 match to the cent

**Matrix, Sales Amount by Category and Reseller Business Type, FY2019**

| Category | Business type | Power BI | Rebuilt |
|---|---|---:|---:|
| Bikes | Specialty Bike Shop | 1,687,480.26 | 1,687,480.26 |
| Bikes | Value Added Reseller | 11,311,980.26 | 11,311,980.26 |
| Bikes | Warehouse | 9,417,959.18 | 9,417,959.18 |
| Components | Specialty Bike Shop | 224,145.70 | 224,145.70 |
| Components | Value Added Reseller | 1,435,322.10 | 1,435,322.10 |
| Components | Warehouse | 2,969,633.35 | 2,969,633.35 |
| Clothing | Specialty Bike Shop | 89,368.09 | 89,368.09 |
| Clothing | Value Added Reseller | 237,391.49 | 237,391.49 |
| Clothing | Warehouse | 423,956.75 | 423,956.75 |
| Accessories | Specialty Bike Shop | 8,406.43 | 8,406.43 |
| Accessories | Value Added Reseller | 40,366.23 | 40,366.23 |
| Accessories | Warehouse | 75,660.69 | 75,660.69 |
| **Bikes** | subtotal | **22,417,419.69** | **22,417,419.69** |
| **Components** | subtotal | **4,629,101.14** | **4,629,101.14** |
| **Clothing** | subtotal | **750,716.33** | **750,716.33** |
| **Accessories** | subtotal | **124,433.35** | **124,433.35** |
| **Total** | | **27,921,670.52** | **27,921,670.52** |

**Order and structure**

| Check | Result |
|---|---|
| Category row order | Bikes > Components > Clothing > Accessories |
| Bar chart order | United States > Canada > [Not Applicable] > France > United Kingdom |
| Area chart axis | 12 months, Jul 2018 to Jun 2019 |
| Two series, two join paths | largest month gap 694,334 between order-date and due-date |

## The four things that made those numbers right

**A visual-level filter on one visual only.** The matrix excludes
`[Not Applicable]` business types; the bar chart beside it does not, which is
why `[Not Applicable]` appears third in the bar chart and nowhere in the matrix.
Without that filter the Bikes subtotal reads 28,544,881.62 instead of
22,417,419.69 - a 27% overstatement on a page that otherwise looks correct.

**A sort-by column reached through another table.** `Product[Category]` sorts by
`Product[Sorting]`, itself a calculated column of `RELATED('Table'[Sorting])`.
Bikes=1, Components=2, Clothing=3, Accessories=4. Sorting alphabetically puts
Accessories first, which is the usual sign a rebuild was done by eye.

**The saved slicer selection.** The report ships filtered to FY2019. Rebuilt
without that selection the same matrix totals 80,450,596.98 - every year at once,
which is what page 2 of the report shows and what page 1 must not.

**USERELATIONSHIP.** `Sales Amount by Due Date` re-routes the date join from
`OrderDateKey` to `DueDateKey`. The rebuild runs one aggregate per join path and
joins them on the axis, which is why the two areas separate month by month
rather than lying on top of each other.

## Layout

| Element | How it is carried |
|---|---|
| Canvas | 1280 x 720, absolute positions from `position` in each visual.json, scaled to fit the viewport |
| Header bar colour | `ThemeDataColor ColorId 6` resolved against the NewExecutive theme to `#6B91C9` |
| Due-date series colour | `ThemeDataColor ColorId 5` to `#DD6B7F` |
| Data bars | literal `#c4d3e9`, scaled to the largest leaf value, on leaf rows only |
| Value column width | 893.52 px, saved in the report |
| Title text and sizes | from each visual's `visualContainerObjects.title` |
| Drop shadows | from `dropShadow` on the same object |

The palette mapping is worth stating because it is not documented anywhere
obvious: `ColorId` indexes a palette whose slot 0 is the theme background and
slot 1 the foreground, so data colours start at slot 2 and
`hex = dataColors[ColorId - 2]`. Confirmed twice on this report against the
rendered output.

## What does not match

**The `(Blank)` member in the year slicer.** Power BI shows it; the rebuild
shows it too, but only because it is synthesised deliberately - and it is worth
understanding rather than copying.

Power BI adds a blank row to a dimension when a fact table holds keys the
dimension does not contain, *including through inactive relationships*. Here
2,113 rows of `Sales` carry a `ShipDateKey` with no matching `Date[DateKey]`.
The relationship is inactive, so nothing on the page uses it, but the blank
member appears in every slicer built on that table anyway.

The rebuild detects the same condition and renders the same item. Selecting it
returns nothing, which is what Power BI does. If you port this to another model,
that check lives in `ReplicaBuilder._blank_member`.

**Font rendering.** The page asks for Segoe UI. On Windows it matches; on Linux
or macOS the fallback is wider, so the header text and axis labels sit a few
pixels differently. Nothing about the numbers changes.

## Not covered

The replica renders `areaChart`, `barChart`, `columnChart`, `pivotTable`,
`tableEx`, `slicer`, `textbox` and `basicShape` - every visual type this report
uses. Anything else renders as a labelled placeholder rather than disappearing,
so a missing chart is visible in the output instead of silently absent.

Also unmodelled: cross-highlighting between visuals (clicking a bar to filter
the rest of the page), drill-down through hierarchies, tooltips carrying extra
fields, and bookmarks. All are in the report definition and none were needed
here.

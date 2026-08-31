#!/usr/bin/env python3
"""Check the rebuilt numbers against the original Power BI render.

    python3 tools/verify_fidelity.py --model build/model

The expected values below were read off a screenshot of the source report as
Power BI Desktop rendered it, page 1, with the slicer on FY2019 exactly as the
report was saved. They are the ground truth for this project: if the extraction,
the join paths, the visual-level filter or the sort order regress, one of these
rows stops matching.

Exit code is 1 on any mismatch, so this works as a test.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic import SemanticModel  # noqa: E402

FY2019 = {"table": "Date", "column": "Fiscal Year", "op": "in", "values": ["FY2019"]}
NOT_NA = {"table": "Reseller", "column": "Business Type", "op": "not_in",
          "values": ["[Not Applicable]"]}

# Page 1 matrix, "Sales Amount by Category and Reseller Business Type".
MATRIX = [
    ("Bikes", "Specialty Bike Shop", 1687480.26),
    ("Bikes", "Value Added Reseller", 11311980.26),
    ("Bikes", "Warehouse", 9417959.18),
    ("Components", "Specialty Bike Shop", 224145.70),
    ("Components", "Value Added Reseller", 1435322.10),
    ("Components", "Warehouse", 2969633.35),
    ("Clothing", "Specialty Bike Shop", 89368.09),
    ("Clothing", "Value Added Reseller", 237391.49),
    ("Clothing", "Warehouse", 423956.75),
    ("Accessories", "Specialty Bike Shop", 8406.43),
    ("Accessories", "Value Added Reseller", 40366.23),
    ("Accessories", "Warehouse", 75660.69),
]
SUBTOTALS = {"Bikes": 22417419.69, "Components": 4629101.14,
             "Clothing": 750716.33, "Accessories": 124433.35}
GRAND_TOTAL = 27921670.52
CATEGORY_ORDER = ["Bikes", "Components", "Clothing", "Accessories"]
# Page 1 bar chart, "Order Quantity by Reseller Country", sorted descending.
BAR_ORDER = ["United States", "Canada", "[Not Applicable]", "France", "United Kingdom"]
TOLERANCE = 0.01


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="build/model")
    a = ap.parse_args()
    sm = SemanticModel(Path(a.model))
    fails = []

    _, df = sm.query(dims=["Product.Category", "Reseller.Business Type"],
                     measures=["Sum(Sales.Sales Amount)"], filters=[FY2019, NOT_NA])
    got = {(r["Product.Category"], r["Reseller.Business Type"]): r["Sum(Sales.Sales Amount)"]
           for _, r in df.iterrows()}

    print("matrix cells (category x business type, FY2019, [Not Applicable] filtered out)")
    for cat, bt, expected in MATRIX:
        actual = got.get((cat, bt))
        ok = actual is not None and abs(actual - expected) <= TOLERANCE
        print(f"  {'ok ' if ok else 'FAIL'} {cat:<12} {bt:<22} "
              f"expected {expected:>15,.2f}  got {actual if actual is None else round(actual, 2):>15,.2f}")
        if not ok:
            fails.append(f"{cat}/{bt}")

    print("\nsubtotals and total")
    for cat, expected in SUBTOTALS.items():
        actual = sum(v for (c, _b), v in got.items() if c == cat)
        ok = abs(actual - expected) <= TOLERANCE
        print(f"  {'ok ' if ok else 'FAIL'} {cat:<12} expected {expected:>15,.2f}  got {actual:>15,.2f}")
        if not ok:
            fails.append(f"subtotal {cat}")
    total = sum(got.values())
    ok = abs(total - GRAND_TOTAL) <= TOLERANCE
    print(f"  {'ok ' if ok else 'FAIL'} {'Total':<12} expected {GRAND_TOTAL:>15,.2f}  got {total:>15,.2f}")
    if not ok:
        fails.append("grand total")

    print("\nrow order (comes from Product[Category] sortByColumn [Sorting], not the alphabet)")
    order = list(dict.fromkeys(df["Product.Category"].tolist()))
    ok = order == CATEGORY_ORDER
    print(f"  {'ok ' if ok else 'FAIL'} {' > '.join(order)}")
    if not ok:
        fails.append("category order")

    print("\nbar chart order (Order Quantity by reseller country, FY2019, descending)")
    _, bar = sm.query(dims=["Reseller.Country-Region"],
                      measures=["Sum(Sales.Order Quantity)"], filters=[FY2019],
                      order=[{"field": "Sum(Sales.Order Quantity)", "direction": "Descending"}])
    got_order = bar["Reseller.Country-Region"].tolist()
    ok = got_order == BAR_ORDER
    print(f"  {'ok ' if ok else 'FAIL'} {' > '.join(got_order)}")
    if not ok:
        fails.append("bar chart order")

    print("\ntwo series, two join paths (area chart, FY2019)")
    _, area = sm.query(dims=["Date.Fiscal.Month"],
                       measures=["Sum(Sales.Sales Amount)", "Sales.Sales Amount by Due Date"],
                       filters=[FY2019])
    same = (area["Sum(Sales.Sales Amount)"] - area["Sales.Sales Amount by Due Date"]).abs().max()
    ok = len(area) == 12 and same > 1000
    print(f"  {'ok ' if ok else 'FAIL'} {len(area)} months; largest gap between order-date and "
          f"due-date series {same:,.0f} (a gap proves USERELATIONSHIP re-routed the join)")
    if not ok:
        fails.append("role-playing date")

    print()
    if fails:
        print(f"{len(fails)} mismatch(es): " + ", ".join(fails))
        sys.exit(1)
    print("every checked value matches the original render.")


if __name__ == "__main__":
    main()

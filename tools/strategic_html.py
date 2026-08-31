#!/usr/bin/env python3
"""Stage 4b: build the strategic dashboard from evidence plus an agent's read.

    python3 tools/strategic_html.py --model build/model \
        --findings build/analysis/findings.json \
        --narrative build/analysis/narrative.json \
        --out build/strategic

The replica answers "what did the report say". This answers "what should
somebody do on Monday" - and it is deliberately a different shape:

* a like-for-like year-over-year on every KPI, comparing only the fiscal months
  present in both years, because the newest year in this model stops mid-June
  and a raw comparison would flatter it
* margin, which the source report never shows even though cost sits in the
  same fact table
* channel, customer and territory, which the source report never touches
* the data-quality findings that change how every other number should be read

Filters stay at fiscal year and channel. Both are order-level attributes here,
so order counts and average order value remain exact under any combination;
adding a product filter would silently break them, so it is offered as a
breakdown instead.

Numbers come from cubes queried out of DuckDB. Judgment - the headline, the
"so what" under each card - comes from narrative.json, written by the agent
that read findings.md. Nothing in the page invents either.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_common import PROJECT_ROOT, log, read_json  # noqa: E402
from semantic import SemanticModel                   # noqa: E402

TEMPLATES = Path(__file__).resolve().parent / "templates"
TOP_PRODUCTS = 60


class StrategicBuilder:
    def __init__(self, model_dir: Path, findings: dict, narrative: dict):
        self.sm = SemanticModel(model_dir)
        self.f = findings
        self.n = narrative or {}
        self.roles = findings["roles"]

    # ------------------------------------------------------------- plumbing
    def col(self, role):
        v = self.roles.get(role)
        if not v:
            return None, None
        t, _, c = v.rpartition(".")
        return t, c

    def qual(self, role):
        t, c = self.col(role)
        return f'"{t}"."{c}"' if t else None

    def joins(self, tables):
        fact = self.roles["fact_table"]
        out, seen = [], {fact}
        for t in tables:
            if not t or t in seen:
                continue
            try:
                path = self.sm.join_path(fact, t)
            except KeyError:
                continue
            for _a, b, rel, _f in path:
                if b in seen:
                    continue
                out.append(f'LEFT JOIN "{b}" ON "{rel["fromTable"]}"."{rel["fromColumn"]}"'
                           f' = "{rel["toTable"]}"."{rel["toColumn"]}"')
                seen.add(b)
        return f'FROM "{fact}"\n' + "\n".join(out)

    def q(self, sql):
        return self.sm.con.execute(sql).fetchdf()

    def measures(self):
        rev, cost, qty, oid = (self.qual("revenue"), self.qual("cost"),
                               self.qual("quantity"), self.qual("order_id"))
        parts = []
        if rev:
            parts.append(f"SUM({rev}) AS rev")
        if cost:
            parts.append(f"SUM({cost}) AS cost")
        if qty:
            parts.append(f"SUM({qty}) AS qty")
        if oid:
            parts.append(f"COUNT(DISTINCT {oid}) AS orders")
        parts.append("COUNT(*) AS lines")
        return ", ".join(parts)

    # ---------------------------------------------------------------- cubes
    def fiscal_index(self) -> str:
        """A CTE numbering the months inside each fiscal year, so year over year
        can be compared like for like when the latest year is incomplete."""
        dt, _ = self.col("date")
        fy = self.qual("fiscal_year")
        mt, mc = self.col("month")
        return (f'fidx AS (SELECT DISTINCT {fy} AS fy, "{mt}"."{mc}" AS m FROM "{dt}"), '
                f"fnum AS (SELECT fy, m, ROW_NUMBER() OVER (PARTITION BY fy ORDER BY m) AS fm "
                f"FROM fidx)")

    def cube_months(self):
        mt, mc = self.col("month")
        fy = self.qual("fiscal_year")
        ch = self.qual("channel")
        ch_t, _ = self.col("channel")
        sel = f'"{mt}"."{mc}" AS m, {fy} AS fy' + (f", {ch} AS ch" if ch else "")
        grp = "1, 2" + (", 3" if ch else "")
        sql = (f"WITH {self.fiscal_index()}\n"
               f"SELECT b.*, fnum.fm FROM (\n"
               f"SELECT {sel}, {self.measures()} {self.joins([mt, ch_t])}\n"
               f"GROUP BY {grp}) b JOIN fnum ON fnum.fy = b.fy AND fnum.m = b.m\n"
               f"ORDER BY 1")
        df = self.q(sql)
        return self._rows(df, dates=["m"]), sql

    def cube_category(self):
        fy = self.qual("fiscal_year")
        fy_t, _ = self.col("fiscal_year")
        ch = self.qual("channel")
        ch_t, _ = self.col("channel")
        cat_t, cat_c = self.col("category")
        sub_t, sub_c = self.col("subcategory")
        sel = [f"{fy} AS fy"]
        if ch:
            sel.append(f"{ch} AS ch")
        if cat_t:
            sel.append(f'"{cat_t}"."{cat_c}" AS cat')
        if sub_t:
            sel.append(f'"{sub_t}"."{sub_c}" AS sub')
        grp = ", ".join(str(i + 1) for i in range(len(sel)))
        sql = (f'SELECT {", ".join(sel)}, {self.measures()} '
               f"{self.joins([fy_t, ch_t, cat_t, sub_t])}\nGROUP BY {grp}")
        return self._rows(self.q(sql)), sql

    def cube_geo(self):
        t, c = self.col("country")
        if not t:
            return [], ""
        fy = self.qual("fiscal_year")
        fy_t, _ = self.col("fiscal_year")
        ch = self.qual("channel")
        ch_t, _ = self.col("channel")
        sel = [f"{fy} AS fy"] + ([f"{ch} AS ch"] if ch else []) + [f'"{t}"."{c}" AS member']
        grp = ", ".join(str(i + 1) for i in range(len(sel)))
        sql = (f'SELECT {", ".join(sel)}, {self.measures()} '
               f"{self.joins([fy_t, ch_t, t])}\nGROUP BY {grp}")
        return self._rows(self.q(sql)), sql

    def cube_pareto(self):
        """Top products by revenue, with the tail folded into one row so the
        cumulative curve stays honest without shipping every SKU."""
        t, c = self.col("product")
        rev = self.qual("revenue")
        if not (t and rev):
            return [], ""
        fy = self.qual("fiscal_year")
        fy_t, _ = self.col("fiscal_year")
        ch = self.qual("channel")
        ch_t, _ = self.col("channel")
        sel = [f"{fy} AS fy"] + ([f"{ch} AS ch"] if ch else []) + [f'"{t}"."{c}" AS member']
        grp = ", ".join(str(i + 1) for i in range(len(sel)))
        sql = f'''
WITH ranked AS (
  SELECT "{t}"."{c}" AS member, SUM({rev}) AS total {self.joins([t])}
  GROUP BY 1 ORDER BY 2 DESC LIMIT {TOP_PRODUCTS}
), detail AS (
  SELECT {", ".join(sel)}, SUM({rev}) AS rev {self.joins([fy_t, ch_t, t])}
  GROUP BY {grp}
)
SELECT CASE WHEN r.member IS NULL THEN '(all other products)' ELSE d.member END AS member,
       d.fy, {"d.ch," if ch else ""} SUM(d.rev) AS rev
FROM detail d LEFT JOIN ranked r ON r.member = d.member
GROUP BY 1, 2{", 3" if ch else ""}
ORDER BY 4 DESC'''
        return self._rows(self.q(sql)), sql

    def _rows(self, df, dates=()):
        out = []
        for rec in df.to_dict("records"):
            row = {}
            for k, v in rec.items():
                if v is None or (isinstance(v, float) and v != v):
                    row[k] = None
                elif k in dates:
                    row[k] = str(v)[:10]
                else:
                    # pandas hands back native Python scalars for most dtypes and
                    # numpy scalars for the rest; normalise both before JSON.
                    v = v.item() if hasattr(v, "item") else v
                    if isinstance(v, bool):
                        row[k] = v
                    elif isinstance(v, float):
                        row[k] = round(v, 4)
                    elif isinstance(v, int):
                        row[k] = v
                    else:
                        row[k] = str(v)
            out.append(row)
        return out

    # ---------------------------------------------------------------- build
    def build(self) -> dict:
        months, sql_months = self.cube_months()
        category, sql_cat = self.cube_category()
        geo, sql_geo = self.cube_geo()
        pareto, sql_par = self.cube_pareto()

        years = sorted({r["fy"] for r in months if r.get("fy")})
        channels = sorted({r.get("ch") for r in months if r.get("ch")})
        quality = self.f.get("quality") or []

        return {
            "model": self.f["model"],
            "roles": self.roles,
            "generated": {
                "source": self.sm.model.get("sourceFile"),
                "rows": self.sm.model.get("totalRows"),
                "queries": {"months": sql_months, "category": sql_cat,
                            "geo": sql_geo, "pareto": sql_par},
            },
            "capabilities": {
                "margin": bool(self.roles.get("cost")),
                "orders": bool(self.roles.get("order_id")),
                "channel": bool(self.roles.get("channel")),
                "category": bool(self.roles.get("category")),
                "geo": bool(geo),
                "pareto": bool(pareto),
            },
            "years": years,
            "channels": channels,
            "defaultYears": years,
            "cubes": {"months": months, "category": category, "geo": geo, "pareto": pareto},
            "quality": quality,
            "period": self.f.get("period"),
            "movers": self.f.get("movers"),
            "concentration": self.f.get("concentration"),
            "unused": self.f.get("unused"),
            "narrative": self.n,
        }


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<div id="app"></div>
<script>window.DASH = {payload};</script>
<script>
{js}
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=str(PROJECT_ROOT / "build" / "model"))
    ap.add_argument("--findings", default=str(PROJECT_ROOT / "build" / "analysis" / "findings.json"))
    ap.add_argument("--narrative", default=str(PROJECT_ROOT / "build" / "analysis" / "narrative.json"))
    ap.add_argument("--out", default=str(PROJECT_ROOT / "build" / "strategic"))
    a = ap.parse_args()

    findings = read_json(Path(a.findings))
    narrative = read_json(Path(a.narrative)) if Path(a.narrative).exists() else {}
    if not narrative:
        log("  note: no narrative.json - the page will render evidence with no read on it")

    b = StrategicBuilder(Path(a.model), findings, narrative)
    payload = b.build()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "dashboard-data.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    html = HTML.format(
        title=narrative.get("title") or f"{findings['model']['name']} - executive review",
        css=(TEMPLATES / "strategic.css").read_text(encoding="utf-8"),
        js=(TEMPLATES / "strategic.js").read_text(encoding="utf-8"),
        payload=json.dumps(payload, separators=(",", ":")),
    )
    (out / "index.html").write_text(html, encoding="utf-8")
    for k, v in payload["cubes"].items():
        log(f"  cube {k:<10} {len(v):>5} rows")
    log(f"\n{out / 'index.html'}  ({(out / 'index.html').stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

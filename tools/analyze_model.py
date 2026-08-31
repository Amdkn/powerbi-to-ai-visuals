#!/usr/bin/env python3
"""Stage 4a: read the model and produce evidence, not opinions.

    python3 tools/analyze_model.py --model build/model -o build/analysis

Writes ``findings.json`` (machine-readable evidence) and ``findings.md`` (the
same thing for a human or an agent to read). The strategic dashboard is built
from this file, so anything it claims can be traced back to a query.

The split matters. This tool computes *what is true*: growth, margin, mix,
concentration, coverage gaps. It never decides what matters or how to say it -
that judgment belongs to the agent that writes narrative.json, and keeping the
two apart is what stops a dashboard from asserting things the data does not
support.

Role detection is heuristic - it looks for a revenue column, a cost column, a
date table and so on by name and shape - and every guess is recorded under
``roles`` with the evidence for it. Pass ``--roles roles.json`` to override any
of them when the guess is wrong.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_common import PROJECT_ROOT, log, read_json, write_json  # noqa: E402
from semantic import SemanticModel                               # noqa: E402

PATTERNS = {
    "revenue": [r"^sales amount$", r"^net revenue$", r"^revenue$", r"^net amount$",
                r"^extended amount$", r"amount$"],
    "cost": [r"^total product cost$", r"^total cost$", r"^cogs$", r"cost$"],
    "quantity": [r"^order quantity$", r"^quantity$", r"^units$", r"^qty$"],
    "order_id": [r"^sales order$", r"^order number$", r"^order id$", r"^invoice"],
    "channel": [r"^channel$", r"^sales channel$"],
    "category": [r"^category$", r"^product category$"],
    "subcategory": [r"^subcategory$", r"^product subcategory$"],
    "product": [r"^product$", r"^product name$", r"^sku$"],
    "country": [r"country", r"^region$"],
    "customer": [r"^customer$", r"^customer name$"],
    "fiscal_year": [r"^fiscal year$", r"^year$"],
    "month": [r"^month$"],
    "date": [r"^date$", r"^full date$"],
}


PLACEHOLDER_RE = re.compile(
    r"^\s*(\[.*\]|n/?a|unknown|none|not applicable|undefined|-1|)\s*$", re.I)


def is_placeholder(v) -> bool:
    """A dimension member that stands in for 'no member'. These absorb whole
    segments: in this model one [Not Applicable] customer carries every
    reseller sale, so any 'top customers' cut is wrong until it is excluded."""
    return v is None or bool(PLACEHOLDER_RE.match(str(v)))


def first_match(names, patterns):
    for p in patterns:
        for n in names:
            if re.search(p, n, re.I):
                return n
    return None


class Analyzer:
    def __init__(self, sm: SemanticModel, overrides: dict | None = None):
        self.sm = sm
        self.overrides = overrides or {}
        self.roles = {}
        self.evidence = {}
        self.skipped = []

    # ------------------------------------------------------------- discovery
    def detect(self) -> dict:
        tables = self.sm.model["tables"]
        by_rows = sorted(tables, key=lambda t: -t.get("rowCount", 0))

        rel_count = {}
        for r in self.sm.relationships:
            rel_count[r["fromTable"]] = rel_count.get(r["fromTable"], 0) + 1
        fact = next((t["name"] for t in by_rows if rel_count.get(t["name"], 0) >= 2),
                    by_rows[0]["name"])
        self._set("fact_table", fact, f"largest table with {rel_count.get(fact, 0)} outgoing relationships")

        date_tbl = None
        for t in tables:
            cols = [c for c in t["columns"] if "date" in (c["dataType"] or "").lower()]
            if cols and (t.get("hierarchies") or re.search(r"date|calendar", t["name"], re.I)):
                date_tbl = t["name"]
                break
        self._set("date_table", date_tbl, "table holding date columns and a hierarchy")

        numeric = [c["name"] for c in self.sm.tables[fact]["columns"]
                   if (c["dataType"] or "") in ("double", "int64")]
        for role in ("revenue", "cost", "quantity"):
            hit = first_match(numeric, PATTERNS[role])
            self._set(role, f"{fact}.{hit}" if hit else None,
                      f"numeric column in [{fact}] matching {PATTERNS[role][0]}")

        # A conformed geography (a territory table) beats the same column name
        # sitting on Customer or Reseller, which each cover one channel only.
        prefer = {"country": r"territory|geograph"}
        for role in ("order_id", "channel", "category", "subcategory", "product",
                     "country", "customer"):
            found = None
            ordered = tables
            if role in prefer:
                ordered = ([t for t in tables if re.search(prefer[role], t["name"], re.I)]
                           + [t for t in tables if not re.search(prefer[role], t["name"], re.I)])
            for t in ordered:
                if t["name"] == self.roles.get("date_table"):
                    continue
                hit = first_match([c["name"] for c in t["columns"] if not c.get("isHidden")],
                                  PATTERNS[role])
                if hit:
                    found = f"{t['name']}.{hit}"
                    break
            self._set(role, found, f"first non-hidden column matching {PATTERNS[role][0]}")

        if date_tbl:
            dcols = [c["name"] for c in self.sm.tables[date_tbl]["columns"]]
            for role in ("fiscal_year", "month", "date"):
                hit = first_match(dcols, PATTERNS[role])
                self._set(role, f"{date_tbl}.{hit}" if hit else None,
                          f"column in [{date_tbl}] matching {PATTERNS[role][0]}")
        return self.roles

    def _set(self, role, value, why):
        if role in self.overrides:
            self.roles[role] = self.overrides[role]
            self.evidence[role] = "overridden by --roles"
            return
        self.roles[role] = value
        self.evidence[role] = why if value else "not found"

    # ----------------------------------------------------------- primitives
    def col(self, role):
        v = self.roles.get(role)
        if not v:
            return None, None
        t, _, c = v.rpartition(".")
        return t, c

    def q(self, sql):
        return self.sm.con.execute(sql).fetchdf()

    def _qual(self, role):
        t, c = self.col(role)
        return f'"{t}"."{c}"' if t else None

    def _fact_join(self, tables_needed):
        fact = self.roles["fact_table"]
        # The order id usually lives on an order-header table, so every
        # aggregate that counts orders needs it joined in.
        order_t, _ = self.col("order_id")
        joins, seen = [], {fact}
        for t in list(tables_needed) + [order_t]:
            if not t or t in seen:
                continue
            try:
                path = self.sm.join_path(fact, t)
            except KeyError:
                continue
            for _a, b, rel, _f in path:
                if b in seen:
                    continue
                joins.append(f'LEFT JOIN "{b}" ON "{rel["fromTable"]}"."{rel["fromColumn"]}" '
                             f'= "{rel["toTable"]}"."{rel["toColumn"]}"')
                seen.add(b)
        return f'FROM "{fact}"\n' + "\n".join(joins)

    def measures_sql(self):
        parts = []
        rev, cost, qty = self._qual("revenue"), self._qual("cost"), self._qual("quantity")
        if rev:
            parts.append(f"SUM({rev}) AS revenue")
        if cost:
            parts.append(f"SUM({cost}) AS cost")
        if rev and cost:
            parts.append(f"SUM({rev}) - SUM({cost}) AS gross_profit")
        if qty:
            parts.append(f"SUM({qty}) AS quantity")
        oid = self._qual("order_id")
        if oid:
            parts.append(f"COUNT(DISTINCT {oid}) AS orders")
        parts.append("COUNT(*) AS lines")
        return ", ".join(parts)

    # -------------------------------------------------------------- findings
    def run(self) -> dict:
        self.detect()
        out = {
            "model": {"name": self.sm.model["name"], "tables": len(self.sm.tables),
                      "rows": self.sm.model["totalRows"],
                      "measures": [m["name"] for m in self.sm.model["measures"]]},
            "roles": self.roles, "roleEvidence": self.evidence,
        }
        for name, fn in (("period", self.period), ("totals", self.totals),
                         ("by_year", self.by_year), ("by_month", self.by_month),
                         ("mix", self.mix), ("concentration", self.concentration),
                         ("movers", self.movers), ("quality", self.quality),
                         ("unused", self.unused)):
            try:
                out[name] = fn()
            except Exception as e:                              # noqa: BLE001
                out[name] = None
                self.skipped.append(f"{name}: {type(e).__name__}: {e}")
        out["skipped"] = self.skipped
        return out

    def period(self):
        dt, dc = self.col("date")
        fy = self._qual("fiscal_year")
        if not dt:
            return None
        joins = self._fact_join([dt])
        df = self.q(f'SELECT MIN("{dt}"."{dc}") AS first_date, MAX("{dt}"."{dc}") AS last_date, '
                    f'{fy} AS fiscal_year, COUNT(*) AS lines {joins} GROUP BY {fy} ORDER BY 1')
        periods = [{"fiscal_year": r["fiscal_year"],
                    "first": str(r["first_date"])[:10], "last": str(r["last_date"])[:10],
                    "lines": int(r["lines"])} for _, r in df.iterrows()]
        cal = self.q(f'SELECT {fy} AS fiscal_year, MIN("{dc}") AS first_date, '
                     f'MAX("{dc}") AS last_date FROM "{dt}" GROUP BY 1 ORDER BY 2')
        calendar = {r["fiscal_year"]: {"first": str(r["first_date"])[:10],
                                       "last": str(r["last_date"])[:10]}
                    for _, r in cal.iterrows()}
        for p in periods:
            c = calendar.get(p["fiscal_year"])
            p["calendar_last"] = c["last"] if c else None
            p["partial"] = bool(c and p["last"] < c["last"])
        empty = [k for k in calendar if k not in {p["fiscal_year"] for p in periods}]
        return {"periods": periods, "calendar": calendar, "empty_periods": empty}

    def totals(self):
        joins = self._fact_join([])
        df = self.q(f"SELECT {self.measures_sql()} {joins}")
        return _row(df)

    def by_year(self):
        fy = self._qual("fiscal_year")
        dt, _ = self.col("fiscal_year")
        joins = self._fact_join([dt])
        df = self.q(f"SELECT {fy} AS fiscal_year, {self.measures_sql()} {joins} "
                    f"GROUP BY 1 ORDER BY 1")
        rows = [_row(df.iloc[[i]]) for i in range(len(df))]
        for i, r in enumerate(rows):
            prev = rows[i - 1] if i else None
            r["revenue_yoy_pct"] = _pct(r.get("revenue"), prev and prev.get("revenue"))
            r["margin_pct"] = _safe_pct(r.get("gross_profit"), r.get("revenue"))
            r["aov"] = (r["revenue"] / r["orders"]) if r.get("orders") else None
        return rows

    def by_month(self):
        mt, mc = self.col("month")
        fy = self._qual("fiscal_year")
        ch = self._qual("channel")
        chan_t, _ = self.col("channel")
        joins = self._fact_join([mt, chan_t])
        sel = f'"{mt}"."{mc}" AS month, {fy} AS fiscal_year'
        grp = "1, 2"
        if ch:
            sel += f", {ch} AS channel"
            grp += ", 3"
        df = self.q(f"SELECT {sel}, {self.measures_sql()} {joins} GROUP BY {grp} ORDER BY 1")
        return [{**_row(df.iloc[[i]]), "month": str(df.iloc[i]["month"])[:10]}
                for i in range(len(df))]

    def mix(self):
        out = {}
        for role in ("channel", "category", "country", "customer"):
            t, c = self.col(role)
            if not t:
                continue
            if role == "customer":
                continue
            fy = self._qual("fiscal_year")
            fy_t, _ = self.col("fiscal_year")
            joins = self._fact_join([t, fy_t])
            df = self.q(f'SELECT {fy} AS fiscal_year, "{t}"."{c}" AS member, '
                        f"{self.measures_sql()} {joins} GROUP BY 1, 2 ORDER BY 1, 3 DESC")
            out[role] = [{**_row(df.iloc[[i]]),
                          "member": _s(df.iloc[i]["member"]),
                          "fiscal_year": _s(df.iloc[i]["fiscal_year"])}
                         for i in range(len(df))]
        return out

    def concentration(self):
        """How much of revenue sits in the top few members of each dimension."""
        rev = self._qual("revenue")
        if not rev:
            return None
        out = {}
        for role in ("customer", "product", "subcategory"):
            t, c = self.col(role)
            if not t:
                continue
            joins = self._fact_join([t])
            df = self.q(f'SELECT "{t}"."{c}" AS member, SUM({rev}) AS revenue {joins} '
                        f"GROUP BY 1 ORDER BY 2 DESC")
            members = [(_s(r["member"]), float(r["revenue"] or 0)) for _, r in df.iterrows()]
            excluded = [{"member": m, "revenue": v} for m, v in members if is_placeholder(m)]
            real = [(m, v) for m, v in members if not is_placeholder(m)]
            total = sum(v for _, v in real) or 1.0
            cum, marks = 0.0, {}
            for i, (_m, v) in enumerate(real, start=1):
                cum += v
                for k in (5, 10, 20, 50, 100):
                    if i == k:
                        marks[f"top{k}_pct"] = round(cum / total * 100, 2)
                if "members_to_80pct" not in marks and cum / total >= 0.8:
                    marks["members_to_80pct"] = i
            out[role] = {"members": len(real), "total_revenue": total, **marks,
                         "excluded_placeholders": excluded,
                         "top": [{"member": m, "revenue": v} for m, v in real[:10]]}
        return out

    def movers(self):
        """Biggest year-over-year revenue swings at the subcategory grain."""
        rev = self._qual("revenue")
        sub_t, sub_c = self.col("subcategory")
        fy = self._qual("fiscal_year")
        fy_t, _ = self.col("fiscal_year")
        if not (rev and sub_t and fy):
            return None
        joins = self._fact_join([sub_t, fy_t])
        df = self.q(f'SELECT {fy} AS fiscal_year, "{sub_t}"."{sub_c}" AS member, '
                    f"SUM({rev}) AS revenue {joins} GROUP BY 1, 2")
        years = sorted({_s(v) for v in df["fiscal_year"].tolist() if v is not None})
        if len(years) < 2:
            return None
        last, prev = years[-1], years[-2]
        cur = {_s(r["member"]): float(r["revenue"]) for _, r in df.iterrows()
               if _s(r["fiscal_year"]) == last}
        old = {_s(r["member"]): float(r["revenue"]) for _, r in df.iterrows()
               if _s(r["fiscal_year"]) == prev}
        rows = []
        for m in set(cur) | set(old):
            a, b = cur.get(m, 0.0), old.get(m, 0.0)
            rows.append({"member": m, "current": a, "previous": b, "delta": a - b,
                         "pct": _pct(a, b)})
        rows.sort(key=lambda r: -abs(r["delta"]))
        return {"current_year": last, "previous_year": prev, "rows": rows[:15]}

    def quality(self):
        """The checks a report never shows and an analyst always wants."""
        issues = []
        for r in self.sm.relationships:
            sql = (f'SELECT COUNT(*) AS n FROM "{r["fromTable"]}" '
                   f'LEFT JOIN "{r["toTable"]}" ON "{r["fromTable"]}"."{r["fromColumn"]}" '
                   f'= "{r["toTable"]}"."{r["toColumn"]}" '
                   f'WHERE "{r["toTable"]}"."{r["toColumn"]}" IS NULL')
            n = int(self.q(sql).iloc[0]["n"])
            if n:
                issues.append({
                    "kind": "referential_integrity",
                    "detail": f'{n:,} rows in [{r["fromTable"]}] carry a '
                              f'[{r["fromColumn"]}] with no match in [{r["toTable"]}]',
                    "rows": n, "active": r["isActive"],
                    "effect": "Power BI shows these under a blank member of "
                              f'[{r["toTable"]}]' if r["isActive"] else
                              "invisible unless the inactive relationship is activated, "
                              f'but still adds a blank row to [{r["toTable"]}]'})
        # Scan every dimension attribute, not only the ones a role points at:
        # the placeholder that matters is often on a column nothing charts yet.
        rev = self._qual("revenue")
        fact = self.roles["fact_table"]
        seen_placeholders = {}
        for t in self.sm.model["tables"]:
            if t["name"] == fact:
                continue
            for c in t["columns"]:
                if c.get("isHidden") or (c["dataType"] or "") != "string":
                    continue
                df = self.q(f'SELECT "{c["name"]}" AS member, COUNT(*) AS n '
                            f'FROM "{t["name"]}" GROUP BY 1')
                bad = [(_s(r["member"]), int(r["n"])) for _, r in df.iterrows()
                       if is_placeholder(r["member"])]
                for member, n in bad:
                    key = (t["name"], member)
                    if key in seen_placeholders:
                        seen_placeholders[key]["columns"].append(c["name"])
                        seen_placeholders[key]["detail"] = (
                            f'[{t["name"]}] has a {member or "NULL"} member across '
                            f'{len(seen_placeholders[key]["columns"])} attributes, '
                            f'carrying {seen_placeholders[key]["revenue_share_pct"]}% of revenue')
                        continue
                    share = None
                    if rev:
                        joins = self._fact_join([t["name"]])
                        cond = (f'"{t["name"]}"."{c["name"]}" IS NULL' if member is None
                                else f'"{t["name"]}"."{c["name"]}" = \'{member}\'')
                        got = self.q(f"SELECT SUM(CASE WHEN {cond} THEN {rev} END) AS a, "
                                     f"SUM({rev}) AS b {joins}")
                        a, b = got.iloc[0]["a"], got.iloc[0]["b"]
                        share = round(float(a or 0) / float(b or 1) * 100, 2)
                    entry = {
                        "kind": "placeholder_member",
                        "detail": f'[{t["name"]}].[{c["name"]}] has {n:,} member(s) valued '
                                  f'{member or "NULL"}, carrying {share}% of revenue',
                        "table": t["name"], "member": member, "columns": [c["name"]],
                        "rows": n, "revenue_share_pct": share,
                        "effect": "a placeholder member absorbs a whole segment; any "
                                  "ranking or share of this attribute is wrong until it "
                                  "is excluded, and filtering it out changes every total"}
                    seen_placeholders[key] = entry
                    issues.append(entry)
        p = self.period() or {}
        for per in p.get("periods", []):
            if per.get("partial"):
                issues.append({
                    "kind": "partial_period",
                    "detail": f'{per["fiscal_year"]} holds data to {per["last"]} but the '
                              f'calendar runs to {per["calendar_last"]}',
                    "effect": "comparing this period with a complete one understates it"})
        for e in p.get("empty_periods", []):
            issues.append({"kind": "empty_period",
                           "detail": f"{e} exists in the date table with no fact rows",
                           "effect": "it appears in every slicer and returns blank"})
        return issues

    def unused(self):
        """Tables and columns the report never touches - dead weight to prune."""
        spec_path = self.sm.dir / "report-spec.json"
        if not spec_path.exists():
            return None
        spec = read_json(spec_path)
        used = set()
        for page in spec["pages"]:
            for v in page["visuals"]:
                for fields in v["fields"].values():
                    for f in fields:
                        if f.get("table") and f.get("column"):
                            used.add((f["table"], f["column"]))
                        if f.get("table") and f.get("level"):
                            used.add((f["table"], f["level"]))
                for flt in v.get("filters", []):
                    if flt.get("table") and flt.get("column"):
                        used.add((flt["table"], flt["column"]))
        tables_used = {t for t, _ in used}
        return {
            "columns_referenced": len(used),
            "tables_referenced": sorted(tables_used),
            "tables_unreferenced": sorted(set(self.sm.tables) - tables_used),
            "note": "unreferenced tables still load and refresh; they are cost without a reader",
        }


def _row(df):
    if not len(df):
        return {}
    r = df.iloc[0]
    out = {}
    for k in df.columns:
        v = r[k]
        if v is None or (isinstance(v, float) and v != v):
            out[k] = None
        elif hasattr(v, "item"):
            out[k] = v.item()
        else:
            out[k] = v
    return out


def _s(v):
    return None if v is None else str(v)


def _pct(cur, prev):
    if not prev or cur is None:
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


def _safe_pct(a, b):
    if not b or a is None:
        return None
    return round(a / b * 100, 2)


def to_markdown(f: dict) -> str:
    L = [f"# Findings - {f['model']['name']}", ""]
    L.append(f"{f['model']['tables']} tables, {f['model']['rows']:,} rows.")
    L.append("")
    L.append("## Detected roles")
    for k, v in f["roles"].items():
        L.append(f"- **{k}**: `{v}`  _( {f['roleEvidence'][k]} )_")
    L.append("")
    if f.get("totals"):
        t = f["totals"]
        L.append("## Totals")
        for k, v in t.items():
            L.append(f"- {k}: {v:,.2f}" if isinstance(v, float) else f"- {k}: {v:,}")
        L.append("")
    if f.get("by_year"):
        L.append("## By fiscal year")
        L.append("| year | revenue | gross profit | margin % | YoY % | orders | AOV |")
        L.append("|---|---|---|---|---|---|---|")
        for r in f["by_year"]:
            L.append("| {fiscal_year} | {revenue:,.0f} | {gp} | {m} | {y} | {o} | {a} |".format(
                fiscal_year=r.get("fiscal_year"), revenue=r.get("revenue") or 0,
                gp=f"{r['gross_profit']:,.0f}" if r.get("gross_profit") else "-",
                m=f"{r['margin_pct']:.2f}" if r.get("margin_pct") else "-",
                y=f"{r['revenue_yoy_pct']:+.1f}" if r.get("revenue_yoy_pct") else "-",
                o=f"{r['orders']:,}" if r.get("orders") else "-",
                a=f"{r['aov']:,.0f}" if r.get("aov") else "-"))
        L.append("")
    if f.get("concentration"):
        L.append("## Concentration")
        for role, c in f["concentration"].items():
            L.append(f"- **{role}**: {c['members']:,} real members, top 10 = "
                     f"{c.get('top10_pct', 0):.1f}% of revenue, "
                     f"{c.get('members_to_80pct', '-')} members reach 80%")
            for x in c.get("excluded_placeholders", []):
                L.append(f"    - excluded placeholder `{x['member']}` "
                         f"holding {x['revenue']:,.0f}")
        L.append("")
    if f.get("movers"):
        m = f["movers"]
        L.append(f"## Movers, {m['previous_year']} -> {m['current_year']}")
        for r in m["rows"][:10]:
            L.append(f"- {r['member']}: {r['delta']:+,.0f} "
                     f"({r['pct']:+.1f}%)" if r["pct"] is not None else
                     f"- {r['member']}: {r['delta']:+,.0f}")
        L.append("")
    if f.get("quality"):
        L.append("## Data quality")
        for q in f["quality"]:
            L.append(f"- **{q['kind']}**: {q['detail']}")
            L.append(f"  - effect: {q['effect']}")
        L.append("")
    if f.get("unused") and f["unused"].get("tables_unreferenced"):
        L.append("## Unreferenced by the report")
        L.append("- " + ", ".join(f["unused"]["tables_unreferenced"]))
        L.append("")
    if f.get("skipped"):
        L.append("## Skipped")
        for s in f["skipped"]:
            L.append(f"- {s}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=str(PROJECT_ROOT / "build" / "model"))
    ap.add_argument("--roles", help="JSON file overriding detected roles")
    ap.add_argument("-o", "--out", default=str(PROJECT_ROOT / "build" / "analysis"))
    a = ap.parse_args()

    sm = SemanticModel(Path(a.model))
    overrides = read_json(Path(a.roles)) if a.roles else {}
    findings = Analyzer(sm, overrides).run()

    out = Path(a.out)
    write_json(out / "findings.json", findings)
    (out / "findings.md").write_text(to_markdown(findings), encoding="utf-8")
    log("  roles: " + ", ".join(f"{k}={v}" for k, v in findings["roles"].items() if v))
    log(f"  {len(findings.get('quality') or [])} data-quality findings")
    if findings.get("skipped"):
        for s in findings["skipped"]:
            log(f"  skipped {s}")
    log(f"\n-> {out/'findings.json'}  and  {out/'findings.md'}")


if __name__ == "__main__":
    main()

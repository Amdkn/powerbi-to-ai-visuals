#!/usr/bin/env python3
"""Stage 3: rebuild the original report as a self-contained HTML page.

    python3 tools/replicate_html.py --model build/model --out build/replica

Fidelity is the whole point here, so the page is *compiled from the report
definition*, never eyeballed from a screenshot. Layout, titles, colours, sort
order, visual-level filters and the saved slicer selection all come out of
report-spec.json; the numbers come out of DuckDB.

Every visual carries a small cube - its own fields, grouped, plus the column
the page's slicer filters on - so the slicer keeps working in the browser.
That client-side re-aggregation is only sound for additive measures, so a
visual holding an average or a distinct count is marked ``followsSlicer:
false`` and renders at the report's saved filter state instead of quietly
returning a wrong number.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_common import PROJECT_ROOT, log, read_json  # noqa: E402
from semantic import SemanticModel                   # noqa: E402

import pandas as pd                                  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent / "templates"

# Roles that hold categories rather than numbers, for visuals where the field
# kind alone is ambiguous.
ADDITIVE_AGGS = {"Sum", "Count", "CountNonNull"}


def is_measure_field(f: dict) -> bool:
    return f.get("kind") in ("aggregation", "measure")


def is_additive(f: dict, sm: SemanticModel) -> bool:
    if f.get("kind") == "aggregation":
        return f.get("function") in ADDITIVE_AGGS
    if f.get("kind") == "measure":
        spec = sm.measures.get(f.get("measure") or f.get("name", ""))
        sql = (spec or {}).get("sql") or ""
        head = sql.strip().upper()
        return head.startswith("SUM(") or head.startswith("COUNT(")
    return False


def jsonable(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.strftime("%Y-%m-%d")
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


class ReplicaBuilder:
    def __init__(self, model_dir: Path, spec_path: Path):
        self.sm = SemanticModel(model_dir)
        self.spec = read_json(spec_path)
        self.theme = self.spec.get("theme", {})

    # --------------------------------------------------------------- slicer
    def page_slicer(self, page: dict):
        """The first slicer on the page becomes the page's filter control."""
        for v in page["visuals"]:
            if v["type"] == "slicer" and v["fields"].get("Values"):
                return v
        return None

    def slicer_payload(self, slicer: dict) -> dict:
        levels = [self.sm.parse_ref(f) for f in slicer["fields"]["Values"]]
        labels = [f.get("displayName") or f.get("level") or f.get("column")
                  for f in slicer["fields"]["Values"]]
        table = levels[0]["table"]
        cols = [lv["column"] for lv in levels]
        sql = 'SELECT DISTINCT ' + ", ".join(f'"{c}"' for c in cols) \
              + f' FROM "{table}" ORDER BY ' + ", ".join(f'"{c}"' for c in cols)
        df = self.sm.con.execute(sql).fetchdf()

        tree, index = [], {}
        for _, r in df.iterrows():
            top = jsonable(r[cols[0]])
            node = index.get(top)
            if node is None:
                node = {"key": f"L0|{top}", "value": top,
                        "label": "(Blank)" if top is None else str(top),
                        "blank": top is None, "expanded": False, "children": []}
                index[top] = node
                tree.append(node)
            if len(cols) > 1:
                child = jsonable(r[cols[1]])
                node["children"].append({
                    "key": f"L1|{child}", "value": child,
                    "label": _date_label(child) if _looks_date(df[cols[1]]) else str(child),
                })
        blank = self._blank_member(table)
        if blank and not any(n["blank"] for n in tree):
            tree.insert(0, {"key": "L0|__blank__", "value": None, "label": "(Blank)",
                            "blank": True, "expanded": False, "children": []})
        return {"tree": tree, "levelColumns": cols, "levelLabels": labels,
                "table": table, "blankMember": bool(blank), "blankReason": blank}

    def _blank_member(self, table: str) -> str | None:
        """Power BI adds a blank row to a dimension when a fact table holds keys
        the dimension does not have - including through *inactive*
        relationships. That is why this report's year slicer shows (Blank)."""
        for r in self.sm.relationships:
            if r["toTable"] != table:
                continue
            sql = (f'SELECT COUNT(*) FROM "{r["fromTable"]}" f '
                   f'LEFT JOIN "{table}" d ON f."{r["fromColumn"]}" = d."{r["toColumn"]}" '
                   f'WHERE d."{r["toColumn"]}" IS NULL')
            n = self.sm.con.execute(sql).fetchone()[0]
            if n:
                state = "active" if r["isActive"] else "inactive"
                return (f'{n:,} rows in [{r["fromTable"]}] have a '
                        f'[{r["fromColumn"]}] with no match in [{table}] '
                        f'({state} relationship)')
        return None

    # --------------------------------------------------------------- visuals
    def build_visual(self, v: dict, page: dict, slicer_info) -> dict:
        base = {"id": v["id"], "type": v["type"],
                "x": round(v["x"], 2), "y": round(v["y"], 2), "z": v.get("z", 0),
                "width": round(v["width"], 2), "height": round(v["height"], 2),
                "title": v.get("title"),
                "shadow": bool((v.get("options") or {}).get("dropShadow"))}

        if v["type"] == "textbox":
            base["paragraphs"] = (v.get("options") or {}).get("paragraphs", [])
            return base
        if v["type"] == "basicShape":
            base.update({k: (v.get("options") or {}).get(k)
                         for k in ("fillColor", "lineColor", "lineTransparency", "roundEdge")})
            return base
        if v["type"] == "slicer":
            base["header"] = ", ".join(slicer_info["levelLabels"]) if slicer_info else ""
            base["selectAllCheckbox"] = (v.get("slicerState") or {}).get("selectAllCheckbox", True)
            return base

        dims, measures = [], []
        for role, fields in v["fields"].items():
            for f in fields:
                (measures if is_measure_field(f) else dims).append({**f, "role": role})
        if not measures and not dims:
            base["note"] = "no fields bound"
            return base

        dim_refs = [self.sm.parse_ref(d) for d in dims]
        m_refs = [self.sm.parse_ref(m) for m in measures]
        filters = list(v.get("filters") or []) + list(page.get("filters") or []) \
            + list(self.spec.get("filters") or [])

        follows = bool(slicer_info) and all(is_additive(m, self.sm) for m in measures)
        query_dims = list(dim_refs)
        slicer_dim = None
        if follows:
            leaf_table, leaf_col = slicer_info["table"], slicer_info["levelColumns"][-1]
            slicer_dim = {"kind": "column", "table": leaf_table, "column": leaf_col,
                          "ref": f"{leaf_table}.{leaf_col}"}
            existing = next((d for d in query_dims
                             if d["table"] == leaf_table and d["column"] == leaf_col), None)
            if existing:
                # The axis already carries the slicer column (the area chart's
                # month axis is the slicer's month level); reuse that column
                # rather than grouping by it twice under a different ref.
                slicer_dim = existing
            else:
                try:
                    self.sm.join_path(self.sm.base_table([m["ref"] for m in m_refs]), leaf_table)
                    query_dims = query_dims + [slicer_dim]
                except KeyError:
                    follows, slicer_dim = False, None

        try:
            sql, df = self.sm.query(dims=query_dims, measures=m_refs, filters=filters)
        except Exception as e:                                  # noqa: BLE001
            base["note"] = f"query failed: {e}"
            base["type"] = "unsupported"
            return base

        cols = list(df.columns)
        rows = [[jsonable(x) for x in row] for row in df.itertuples(index=False, name=None)]

        def idx(ref):
            return cols.index(ref) if ref in cols else None

        base.update({
            "cols": cols, "rows": rows,
            "dims": [{"ref": d["ref"], "label": _dim_label(orig, d),
                      "isDate": self._is_date(d)} for d, orig in zip(dim_refs, dims)],
            "measures": self._measures(m_refs, measures, v),
            "dimIdx": [idx(d["ref"]) for d in dim_refs],
            "sortIdx": [idx(f"{d['ref']}__sort") for d in dim_refs],
            "valueIdx": [idx(m["ref"]) for m in m_refs],
            "slicerCol": idx(slicer_dim["ref"]) if slicer_dim else None,
            "followsSlicer": follows,
            "sortRules": self._sort_rules(v, dim_refs, m_refs),
            "defaultSort": [{"kind": "dim", "index": i, "desc": False}
                            for i in range(len(dim_refs))],
            "dataBars": (v.get("options") or {}).get("dataBars"),
            "columnWidth": (v.get("options") or {}).get("columnWidth"),
            "sql": sql,
        })
        return base

    def _measures(self, m_refs, originals, v):
        out = []
        for i, (m, orig) in enumerate(zip(m_refs, originals)):
            key = orig.get("queryRef") or m["ref"]
            color = (v.get("colors") or {}).get(key) or (v.get("colors") or {}).get("__default__")
            if not color:
                palette = self.theme.get("dataColors") or ["#118DFF"]
                color = palette[i % len(palette)]
            out.append({"ref": m["ref"], "label": _measure_label(orig, m), "color": color})
        return out

    def _sort_rules(self, v, dim_refs, m_refs):
        rules = []
        for s in v.get("sort") or []:
            ref = s.get("ref")
            desc = str(s.get("direction", "")).lower().startswith("desc")
            for i, m in enumerate(m_refs):
                if m["ref"] == ref or ref == s.get("queryRef"):
                    rules.append({"kind": "value", "index": i, "desc": desc})
                    break
            else:
                for i, d in enumerate(dim_refs):
                    if d["ref"] == ref:
                        rules.append({"kind": "dim", "index": i, "desc": desc})
                        break
        return rules

    def _is_date(self, d):
        try:
            return "date" in (self.sm.column(d["table"], d["column"])["dataType"] or "").lower()
        except KeyError:
            return False

    # ----------------------------------------------------------------- page
    def build(self) -> dict:
        pages, slicer_payload, initial = [], None, []
        for p in self.spec["pages"]:
            slicer = self.page_slicer(p)
            info = self.slicer_payload(slicer) if slicer else None
            if info and slicer_payload is None:
                slicer_payload = info
                initial = self._initial_selection(slicer, info)
            visuals = [self.build_visual(v, p, info) for v in p["visuals"]]
            pages.append({"name": p["name"], "displayName": p["displayName"],
                          "width": p["width"], "height": p["height"], "visuals": visuals})
        active = next((i for i, p in enumerate(self.spec["pages"])
                       if p["name"] == self.spec.get("activePage")), 0)
        return {"name": self.spec.get("name"), "theme": self.theme,
                "pages": pages, "activePageIndex": active,
                "slicer": slicer_payload, "initialSelection": initial}

    def _initial_selection(self, slicer, info) -> list:
        """Reproduce the selection the report was saved with."""
        keys = []
        for sel in (slicer.get("slicerState") or {}).get("selection", []):
            if sel.get("op") != "in":
                continue
            level = 0 if sel.get("column") == info["levelColumns"][0] else 1
            for val in sel.get("values", []):
                if level == 0:
                    keys.append(f"L0|{val}")
                else:
                    keys.append(f"L1|{val}")
        # A slicer bound to the hierarchy's Year level saves the year *column*
        # value, which may differ from the level column when they are aliased.
        if not keys:
            return []
        known = {n["key"] for n in info["tree"]} | {
            c["key"] for n in info["tree"] for c in n.get("children", [])}
        return [k for k in keys if k in known]


def _looks_date(series) -> bool:
    return "datetime" in str(series.dtype)


def _date_label(v) -> str:
    if not v:
        return "(Blank)"
    try:
        return pd.Timestamp(v).strftime("%b %Y")
    except (ValueError, TypeError):
        return str(v)


def _dim_label(orig, resolved) -> str:
    return orig.get("displayName") or orig.get("level") or orig.get("column") \
        or resolved.get("column") or resolved["ref"]


def _measure_label(orig, resolved) -> str:
    if orig.get("displayName"):
        return orig["displayName"]
    if orig.get("kind") == "measure":
        return orig.get("measure") or resolved["ref"]
    return orig.get("column") or resolved["ref"]


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
<div class="shell">
  <div class="topbar">
    <strong>{title}</strong>
    <span class="badge">replica of the Power BI report</span>
    <span class="spacer"></span>
    <span class="badge">{tables} tables &middot; {rows} rows</span>
    <span class="badge">built from the report definition, not a screenshot</span>
  </div>
  <div class="stage"><div id="canvas-wrap" class="canvas-wrap"><div id="canvas" class="canvas"></div></div></div>
  <div id="tabs" class="tabs"></div>
</div>
<script>window.REPORT = {payload};</script>
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
    ap.add_argument("--spec", default=None)
    ap.add_argument("--out", default=str(PROJECT_ROOT / "build" / "replica"))
    a = ap.parse_args()

    model_dir = Path(a.model)
    spec = Path(a.spec) if a.spec else model_dir / "report-spec.json"
    b = ReplicaBuilder(model_dir, spec)
    payload = b.build()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report-data.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")

    html = HTML.format(
        title=payload["name"] or "Power BI replica",
        css=(TEMPLATES / "replica.css").read_text(encoding="utf-8"),
        js=(TEMPLATES / "replica.js").read_text(encoding="utf-8"),
        payload=json.dumps(payload, separators=(",", ":")),
        tables=len(b.sm.tables),
        rows=f"{b.sm.model['totalRows']:,}",
    )
    (out / "index.html").write_text(html, encoding="utf-8")

    for p in payload["pages"]:
        for v in p["visuals"]:
            if v.get("rows") is not None:
                log(f"  {p['displayName']:<8} {v['type']:<12} "
                    f"{len(v['rows']):>5} cube rows  slicer={'yes' if v.get('followsSlicer') else 'no'}")
    size = (out / "index.html").stat().st_size
    log(f"\n{out / 'index.html'}  ({size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

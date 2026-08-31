#!/usr/bin/env python3
"""Stage 2: read the report definition and write a normalised report spec.

    python3 tools/pbir_parse.py "<PBIP folder | .Report folder | .pbix>" \
        -o build/model/report-spec.json

Power BI stores a report in one of two shapes:

  * PBIR   - ``definition/pages/<page>/visuals/<id>/visual.json`` (a .pbip, and
             .pbix files saved by recent Desktop builds)
  * Layout - a single ``Report/Layout`` JSON whose ``config`` members are
             themselves JSON *strings* (every older .pbix)

Both are flattened to the same spec so the renderers downstream never branch on
the container. What matters for a faithful rebuild and is easy to lose:

  * visual-level filters (this report hides ``[Not Applicable]`` resellers in
    the matrix only - miss it and every subtotal is wrong)
  * the saved slicer selection (the report ships filtered to FY2019)
  * theme colour references, which are indexes, not hex
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_common import PROJECT_ROOT, log, read_json, write_json  # noqa: E402

# Microsoft.PowerBI QueryAggregateFunction
AGG = {0: "Sum", 1: "Avg", 2: "Count", 3: "Min", 4: "Max",
       5: "CountNonNull", 6: "Median", 7: "StdDev", 8: "Var"}


# ------------------------------------------------------------------ literals

def literal(value):
    """PBI literals arrive as strings: 'FY2019'  27L  16D  true  null."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    s = str(value).strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.lower() == "null":
        return None
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if s and s[-1] in "LDM" and s[:-1].replace(".", "", 1).lstrip("-").isdigit():
        body = s[:-1]
        return int(body) if s[-1] == "L" else float(body)
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return s


def _expr_literal(node):
    if isinstance(node, dict):
        if "Literal" in node:
            return literal(node["Literal"].get("Value"))
        if "expr" in node:
            return _expr_literal(node["expr"])
    return None


# --------------------------------------------------------------------- fields

def field_ref(f: dict) -> dict:
    """Normalise one field reference into {kind, table, ...} plus a stable ref."""
    if not isinstance(f, dict):
        return {"kind": "unknown", "raw": f}
    if "Column" in f:
        c = f["Column"]
        t = _entity(c.get("Expression"))
        return {"kind": "column", "table": t, "column": c.get("Property"),
                "ref": f"{t}.{c.get('Property')}", "label": c.get("Property")}
    if "Measure" in f:
        m = f["Measure"]
        t = _entity(m.get("Expression"))
        return {"kind": "measure", "table": t, "measure": m.get("Property"),
                "ref": f"{t}.{m.get('Property')}", "label": m.get("Property")}
    if "Aggregation" in f:
        a = f["Aggregation"]
        inner = field_ref(a.get("Expression", {}))
        fn = AGG.get(a.get("Function"), str(a.get("Function")))
        return {"kind": "aggregation", "function": fn, "table": inner.get("table"),
                "column": inner.get("column"),
                "ref": f"{fn}({inner.get('ref')})", "label": inner.get("column")}
    if "HierarchyLevel" in f:
        h = f["HierarchyLevel"]
        hier = h.get("Expression", {}).get("Hierarchy", {})
        t = _entity(hier.get("Expression"))
        return {"kind": "hierarchyLevel", "table": t,
                "hierarchy": hier.get("Hierarchy"), "level": h.get("Level"),
                "ref": f"{t}.{hier.get('Hierarchy')}.{h.get('Level')}",
                "label": h.get("Level")}
    if "SparklineData" in f:
        return field_ref(f["SparklineData"].get("Value", {}))
    key = next(iter(f), "unknown")
    return {"kind": key.lower(), "raw": f, "ref": key}


def _entity(expr) -> str | None:
    if not isinstance(expr, dict):
        return None
    if "SourceRef" in expr:
        sr = expr["SourceRef"]
        return sr.get("Entity") or sr.get("Source")
    for v in expr.values():
        got = _entity(v)
        if got:
            return got
    return None


# -------------------------------------------------------------------- filters

def normalise_filter(flt: dict, source_map: dict | None = None) -> list:
    """Flatten a PBI filter's Where clause to [{table, column, op, values}]."""
    out = []
    if not isinstance(flt, dict):
        return out
    source_map = {**(source_map or {}),
                  **{f["Name"]: f.get("Entity") for f in flt.get("From", []) if "Name" in f}}
    for w in flt.get("Where", []):
        cond = w.get("Condition", {})
        out.extend(_condition(cond, source_map))
    return out


def _resolve_alias(fr: dict, smap: dict) -> dict:
    """Where clauses address tables through the From aliases ('r' -> Reseller)."""
    t = fr.get("table")
    if t in smap and smap[t]:
        fr["table"] = smap[t]
        if fr.get("column"):
            fr["ref"] = f"{smap[t]}.{fr['column']}"
    return fr


def _condition(cond: dict, smap: dict) -> list:
    if "Not" in cond:
        inner = _condition(cond["Not"].get("Expression", {}), smap)
        for c in inner:
            c["op"] = {"in": "not_in", "not_in": "in"}.get(c["op"], "not_" + c["op"])
        return inner
    if "In" in cond:
        node = cond["In"]
        cols = [_resolve_alias(field_ref(e), smap) for e in node.get("Expressions", [])]
        vals = [[_expr_literal(v) if isinstance(v, dict) else v for v in row]
                for row in node.get("Values", [])]
        if len(cols) == 1:
            return [{"table": cols[0].get("table"), "column": cols[0].get("column"),
                     "ref": cols[0].get("ref"), "op": "in",
                     "values": [row[0] for row in vals if row]}]
        return [{"columns": [c.get("ref") for c in cols], "op": "in_tuples",
                 "values": vals}]
    if "Comparison" in cond:
        cmp_ = cond["Comparison"]
        left = _resolve_alias(field_ref(cmp_.get("Left", {})), smap)
        kind = {0: "eq", 1: "gt", 2: "gte", 3: "lt", 4: "lte"}.get(cmp_.get("ComparisonKind"), "eq")
        return [{"table": left.get("table"), "column": left.get("column"),
                 "ref": left.get("ref"), "op": kind,
                 "values": [_expr_literal(cmp_.get("Right", {}))]}]
    if "And" in cond:
        return _condition(cond["And"].get("Left", {}), smap) + \
               _condition(cond["And"].get("Right", {}), smap)
    if "Or" in cond:
        return [{"op": "or",
                 "branches": [_condition(cond["Or"].get("Left", {}), smap),
                              _condition(cond["Or"].get("Right", {}), smap)]}]
    return [{"op": "unsupported", "raw": cond}]


def filters_from_config(cfg) -> list:
    """filterConfig (PBIR) or the legacy 'filters' JSON string (Layout)."""
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            return []
    entries = cfg.get("filters", []) if isinstance(cfg, dict) else (cfg or [])
    out = []
    for f in entries:
        conds = normalise_filter(f.get("filter", {}))
        if not conds:
            continue
        fld = field_ref(f.get("expression") or f.get("field") or {})
        for c in conds:
            c.setdefault("table", fld.get("table"))
            c.setdefault("column", fld.get("column"))
            c["filterName"] = f.get("name")
        out.extend(conds)
    return out


# ---------------------------------------------------------------------- theme

class Theme:
    """Resolves ThemeDataColor references to hex.

    ColorId is an index into the report's palette, and the palette is *not*
    dataColors alone: slot 0 is the background, slot 1 the foreground, and the
    data colours start at slot 2. Verified against this report - the header
    shape asks for ColorId 6 and renders NewExecutive dataColors[4] (#6B91C9),
    and the due-date series asks for ColorId 5 and renders dataColors[3]
    (#DD6B7F).
    """

    def __init__(self, base: dict | None = None, custom: dict | None = None):
        self.base = base or {}
        self.custom = custom or {}
        merged = dict(self.base)
        merged.update({k: v for k, v in self.custom.items() if v})
        self.data_colors = merged.get("dataColors") or []
        self.background = merged.get("background", "#FFFFFF")
        self.foreground = merged.get("foreground", "#252423")
        self.table_accent = merged.get("tableAccent", self.data_colors[0] if self.data_colors else "#118DFF")
        self.minimum = merged.get("minimum", "#DEEFFF")
        self.maximum = merged.get("maximum", self.table_accent)
        self.text_classes = merged.get("textClasses", {}) or self.base.get("textClasses", {})
        self.name = self.custom.get("name") or self.base.get("name")

    def palette(self, color_id: int) -> str:
        if color_id == 0:
            return self.background
        if color_id == 1:
            return self.foreground
        idx = color_id - 2
        if 0 <= idx < len(self.data_colors):
            return self.data_colors[idx]
        return self.data_colors[idx % len(self.data_colors)] if self.data_colors else self.foreground

    def series_color(self, index: int) -> str:
        return self.data_colors[index % len(self.data_colors)] if self.data_colors else "#118DFF"

    def resolve(self, node) -> str | None:
        """Accepts the several shapes a colour property takes in the JSON."""
        if node is None:
            return None
        if isinstance(node, str):
            return self._named(node)
        if isinstance(node, dict):
            if "solid" in node:
                return self.resolve(node["solid"].get("color"))
            if "expr" in node:
                return self.resolve(node["expr"])
            if "Literal" in node:
                return self._named(literal(node["Literal"].get("Value")))
            if "ThemeDataColor" in node:
                t = node["ThemeDataColor"]
                return shade(self.palette(int(t.get("ColorId", 0))),
                             float(t.get("Percent", 0) or 0))
            if "color" in node:
                return self.resolve(node["color"])
        return None

    def _named(self, v):
        if not isinstance(v, str):
            return None
        named = {"foreground": self.foreground, "background": self.background,
                 "minColor": self.minimum, "maxColor": self.maximum,
                 "tableAccent": self.table_accent}
        return named.get(v, v)


def shade(hex_color: str, percent: float) -> str:
    """Power BI tints (negative percent) and shades (positive) a palette slot."""
    if not percent or not hex_color or not hex_color.startswith("#"):
        return hex_color
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    if percent > 0:                      # darker
        f = 1 - min(percent, 1)
        r, g, b = (int(v * f) for v in (r, g, b))
    else:                                # lighter
        f = min(-percent, 1)
        r, g, b = (int(v + (255 - v) * f) for v in (r, g, b))
    return "#%02x%02x%02x" % (r, g, b)


def load_theme(report_dir: Path, report_json: dict) -> Theme:
    def _load(entry):
        if not entry:
            return None
        for pkg in report_json.get("resourcePackages", []):
            for item in pkg.get("items", []):
                if item.get("name") == entry.get("name"):
                    p = report_dir / "StaticResources" / pkg.get("name", "SharedResources") / item["path"]
                    if p.exists():
                        return read_json(p)
        hits = list(report_dir.rglob(f"{entry.get('name')}.json"))
        return read_json(hits[0]) if hits else None

    tc = report_json.get("themeCollection", {})
    return Theme(_load(tc.get("baseTheme")), _load(tc.get("customTheme")))


# ------------------------------------------------------------------ PBIR read

def parse_pbir(report_dir: Path) -> dict:
    defs = report_dir / "definition"
    report_json = read_json(defs / "report.json")
    theme = load_theme(report_dir, report_json)
    order = read_json(defs / "pages" / "pages.json")

    pages = []
    for page_name in order.get("pageOrder", []):
        pdir = defs / "pages" / page_name
        if not (pdir / "page.json").exists():
            continue
        pj = read_json(pdir / "page.json")
        visuals = []
        for vfile in sorted((pdir / "visuals").glob("*/visual.json")):
            visuals.append(parse_visual(read_json(vfile), theme))
        visuals.sort(key=lambda v: v.get("z", 0))
        pages.append({
            "name": page_name,
            "displayName": pj.get("displayName", page_name),
            "width": pj.get("width", 1280), "height": pj.get("height", 720),
            "displayOption": pj.get("displayOption", "FitToPage"),
            "filters": filters_from_config(pj.get("filterConfig")),
            "visuals": visuals,
        })

    return {
        "format": "pbir",
        "activePage": order.get("activePageName"),
        "theme": theme_dict(theme),
        "filters": filters_from_config(report_json.get("filterConfig")),
        "settings": report_json.get("settings", {}),
        "pages": pages,
    }


def theme_dict(theme: Theme) -> dict:
    return {"name": theme.name, "dataColors": theme.data_colors,
            "background": theme.background, "foreground": theme.foreground,
            "tableAccent": theme.table_accent, "minimum": theme.minimum,
            "maximum": theme.maximum, "textClasses": theme.text_classes}


def parse_visual(vj: dict, theme: Theme) -> dict:
    pos = vj.get("position", {})
    vis = vj.get("visual", {}) or {}
    vtype = vis.get("visualType") or ("group" if "visualGroup" in vj else "unknown")
    out = {
        "id": vj.get("name"), "type": vtype,
        "x": pos.get("x", 0), "y": pos.get("y", 0), "z": pos.get("z", 0),
        "width": pos.get("width", 0), "height": pos.get("height", 0),
        "tabOrder": pos.get("tabOrder"),
        "fields": {}, "sort": [], "filters": [], "colors": {}, "options": {},
    }

    q = vis.get("query", {}) or {}
    for role, spec in (q.get("queryState") or {}).items():
        out["fields"][role] = [
            {**field_ref(p.get("field", {})),
             "queryRef": p.get("queryRef"),
             "active": p.get("active", True),
             "displayName": p.get("displayName")}
            for p in spec.get("projections", [])]
    for s in (q.get("sortDefinition") or {}).get("sort", []):
        out["sort"].append({**field_ref(s.get("field", {})),
                            "direction": s.get("direction", "Ascending")})
    out["isDefaultSort"] = bool((q.get("sortDefinition") or {}).get("isDefaultSort"))

    out["filters"] = filters_from_config(vis.get("filterConfig") or vj.get("filterConfig"))

    objects = vis.get("objects", {}) or {}
    vco = vis.get("visualContainerObjects", {}) or {}
    out["title"] = _title(vco)
    out["objects"] = objects
    out["visualContainerObjects"] = vco
    out["options"] = extract_options(vtype, objects, vco, theme)
    out["colors"] = extract_colors(objects, theme)
    if vtype == "slicer":
        out["slicerState"] = slicer_state(objects, vis)
    return out


def _title(vco: dict) -> dict | None:
    t = (vco.get("title") or [{}])[0].get("properties", {})
    if not t:
        return None
    return {"text": _expr_literal(t.get("text")),
            "fontSize": _expr_literal(t.get("fontSize")),
            "color": _expr_literal(t.get("fontColor")),
            "show": _expr_literal(t.get("show"))}


def extract_colors(objects: dict, theme: Theme) -> dict:
    """dataPoint overrides, keyed by the metadata selector they apply to."""
    colors = {}
    for entry in objects.get("dataPoint", []) or []:
        sel = (entry.get("selector") or {}).get("metadata") or "__default__"
        c = theme.resolve((entry.get("properties") or {}).get("fill"))
        if c:
            colors[sel] = c
    return colors


def extract_options(vtype: str, objects: dict, vco: dict, theme: Theme) -> dict:
    """The handful of formatting knobs the renderers actually honour."""
    o = {}
    if vtype == "basicShape":
        gen = (objects.get("general") or [{}])[0].get("properties", {})
        fill = (objects.get("fill") or [{}])[0].get("properties", {})
        line = (objects.get("line") or [{}])[0].get("properties", {})
        o["shapeType"] = _expr_literal(gen.get("shapeType")) or "rectangle"
        o["fillColor"] = theme.resolve(fill.get("fillColor"))
        o["lineTransparency"] = _expr_literal(line.get("transparency"))
        o["lineColor"] = theme.resolve(line.get("lineColor"))
        o["roundEdge"] = _expr_literal(gen.get("roundEdge"))
    if vtype == "textbox":
        paras = []
        gen = (objects.get("general") or [{}])[0].get("properties", {})
        for p in gen.get("paragraphs", []) or []:
            runs = [{"text": r.get("value", ""), **(r.get("textStyle") or {})}
                    for r in p.get("textRuns", [])]
            paras.append({"runs": runs, "align": p.get("horizontalTextAlignment")})
        o["paragraphs"] = paras
    for entry in objects.get("columnFormatting", []) or []:
        props = (entry.get("properties") or {}).get("dataBars")
        if props:
            o.setdefault("dataBars", {})[(entry.get("selector") or {}).get("metadata")] = {
                "positiveColor": theme.resolve(props.get("positiveColor")),
                "negativeColor": theme.resolve(props.get("negativeColor")),
                "axisColor": theme.resolve(props.get("axisColor")),
                "hideText": _expr_literal(props.get("hideText")),
                "reverseDirection": _expr_literal(props.get("reverseDirection")),
            }
    for entry in objects.get("columnWidth", []) or []:
        w = _expr_literal((entry.get("properties") or {}).get("value"))
        if w:
            o.setdefault("columnWidth", {})[(entry.get("selector") or {}).get("metadata")] = w
    hdr = (objects.get("header") or [{}])[0].get("properties", {})
    if hdr:
        o["headerFontSize"] = _expr_literal(hdr.get("textSize"))
    bg = (vco.get("background") or [{}])[0].get("properties", {})
    if bg:
        o["containerBackground"] = _expr_literal(bg.get("show"))
    shadow = (vco.get("dropShadow") or [{}])[0].get("properties", {})
    if shadow:
        o["dropShadow"] = _expr_literal(shadow.get("show"))
    return o


def slicer_state(objects: dict, vis: dict) -> dict:
    """The selection saved with the report - a slicer is a filter that renders."""
    gen = (objects.get("general") or [{}])[0].get("properties", {})
    flt = (gen.get("filter") or {}).get("filter")
    sel = normalise_filter(flt) if flt else []
    mode = _expr_literal(((objects.get("data") or [{}])[0].get("properties", {})).get("mode"))
    selall = _expr_literal(((objects.get("selection") or [{}])[0].get("properties", {})).get("selectAllCheckboxEnabled"))
    return {"selection": sel, "mode": mode or "Basic",
            "selectAllCheckbox": bool(selall),
            "expansionStates": vis.get("expansionStates", [])}


# ---------------------------------------------------------------- Layout read

def parse_layout(layout: dict, theme: Theme) -> dict:
    """Legacy single-file report container found in older .pbix files."""
    pages = []
    for sec in sorted(layout.get("sections", []), key=lambda s: s.get("ordinal", 0)):
        visuals = []
        for vc in sec.get("visualContainers", []):
            cfg = _jload(vc.get("config"))
            single = cfg.get("singleVisual", {})
            vtype = single.get("visualType", "unknown")
            proj = single.get("projections", {}) or {}
            fields = {}
            select = {s.get("Name"): s for s in
                      (single.get("prototypeQuery", {}) or {}).get("Select", [])}
            for role, items in proj.items():
                out_items = []
                for it in items:
                    sel = select.get(it.get("queryRef")) or {}
                    fr = field_ref({k: v for k, v in sel.items() if k != "Name"}) if sel else \
                        {"kind": "unknown", "ref": it.get("queryRef")}
                    fr["queryRef"] = it.get("queryRef")
                    fr["active"] = it.get("active", True)
                    out_items.append(fr)
                fields[role] = out_items
            objects = single.get("objects", {}) or {}
            vco = single.get("vcObjects", {}) or {}
            visuals.append({
                "id": vc.get("id") or cfg.get("name"), "type": vtype,
                "x": vc.get("x", 0), "y": vc.get("y", 0), "z": vc.get("z", 0),
                "width": vc.get("width", 0), "height": vc.get("height", 0),
                "fields": fields, "sort": [], "filters": filters_from_config(vc.get("filters")),
                "title": _title(vco), "objects": objects, "visualContainerObjects": vco,
                "options": extract_options(vtype, objects, vco, theme),
                "colors": extract_colors(objects, theme),
                **({"slicerState": slicer_state(objects, single)} if vtype == "slicer" else {}),
            })
        visuals.sort(key=lambda v: v.get("z", 0))
        cfg = _jload(sec.get("config"))
        pages.append({"name": sec.get("name"), "displayName": sec.get("displayName"),
                      "width": sec.get("width", 1280), "height": sec.get("height", 720),
                      "displayOption": "FitToPage",
                      "filters": filters_from_config(sec.get("filters")),
                      "visuals": visuals})
    return {"format": "layout", "activePage": pages[0]["name"] if pages else None,
            "theme": theme_dict(theme),
            "filters": filters_from_config(layout.get("filters")),
            "settings": {}, "pages": pages}


def _jload(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


# ----------------------------------------------------------------------- main

def parse_report(source: Path) -> dict:
    source = source.resolve()
    if source.is_file() and source.suffix.lower() == ".pbix":
        return _from_pbix(source)
    report_dir = source
    if not (report_dir / "definition").is_dir():
        found = list(source.glob("*.Report")) or list(source.glob("*/*.Report"))
        if not found:
            raise SystemExit(f"No report definition under {source}")
        report_dir = found[0]
    spec = parse_pbir(report_dir)
    spec["name"] = report_dir.stem
    spec["sourceDir"] = str(report_dir)
    return spec


def _from_pbix(pbix: Path) -> dict:
    with zipfile.ZipFile(pbix) as z:
        names = set(z.namelist())
        tmp = Path(__import__("tempfile").mkdtemp(prefix="pbix_report_"))
        if "Report/definition/report.json" in names:
            for n in names:
                if n.startswith("Report/"):
                    dest = tmp / n[len("Report/"):]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(n))
            spec = parse_pbir(tmp)
        elif "Report/Layout" in names:
            layout = json.loads(z.read("Report/Layout").decode("utf-16-le", "ignore").lstrip("﻿"))
            base = custom = None
            for n in names:
                if n.startswith("Report/StaticResources") and n.endswith(".json"):
                    data = json.loads(z.read(n).decode("utf-8-sig"))
                    if "BaseThemes" in n:
                        base = data
                    else:
                        custom = data
            spec = parse_layout(layout, Theme(base, custom))
        else:
            raise SystemExit("No Report/definition or Report/Layout inside the .pbix")
    spec["name"] = pbix.stem
    spec["sourceDir"] = str(pbix)
    return spec


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="PBIP folder, *.Report folder, or .pbix")
    ap.add_argument("-o", "--out",
                    default=str(PROJECT_ROOT / "build" / "model" / "report-spec.json"))
    a = ap.parse_args()
    spec = parse_report(Path(a.source))
    write_json(Path(a.out), spec)
    for p in spec["pages"]:
        log(f"  {p['displayName']:<10} {len(p['visuals'])} visuals: "
            + ", ".join(sorted({v['type'] for v in p['visuals']})))
    log(f"\n{len(spec['pages'])} pages -> {a.out}")


if __name__ == "__main__":
    main()

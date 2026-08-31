#!/usr/bin/env python3
"""The semantic layer: model.json + DuckDB behind one query() call.

This is what makes the extracted model *usable* rather than merely extracted.
It knows the star schema, so callers ask for fields and measures and never
write joins:

    sm = SemanticModel("build/model")
    sql, df = sm.query(dims=["Product.Category"],
                       measures=["Sum(Sales.Sales Amount)"],
                       filters=[{"table": "Date", "column": "Fiscal Year",
                                 "op": "in", "values": ["FY2019"]}])

Three details carry most of the fidelity:

* Sort-by columns. ``Product[Category]`` sorts by ``Product[Sorting]``, which
  is why Power BI renders Bikes, Components, Clothing, Accessories and not
  alphabetical order. Every dimension carries its sort key into the result.
* Role-playing dates. A measure wrapped in USERELATIONSHIP reaches the date
  table down a different key. Measures are grouped by relationship path and
  each group gets its own aggregate, then the groups are joined on the
  dimensions - which is exactly how the two series of the area chart differ.
* Hierarchy levels. ``Date.Fiscal.Month`` is a level, not a column; it resolves
  through the hierarchy defined in the model.
"""
from __future__ import annotations

import json
import re
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_common import read_json, require  # noqa: E402

require("duckdb", "pandas")

import duckdb                      # noqa: E402

import dax_sql                     # noqa: E402

AGG_SQL = {"Sum": "SUM", "Avg": "AVG", "Min": "MIN", "Max": "MAX",
           "Count": "COUNT", "CountNonNull": "COUNT", "Median": "MEDIAN",
           "StdDev": "STDDEV", "Var": "VARIANCE", "DistinctCount": "COUNT(DISTINCT %s)"}

AGG_RE = re.compile(r"^(?P<fn>[A-Za-z]+)\((?P<inner>.+)\)$")


class SemanticModel:
    def __init__(self, model_dir):
        self.dir = Path(model_dir)
        self.model = read_json(self.dir / "model.json")
        self.tables = {t["name"]: t for t in self.model["tables"]}
        self.relationships = self.model["relationships"]
        self.db_path = self.dir / "model.duckdb"
        self._con = None
        self.measures = {}
        for m in self.model.get("measures", []):
            t = dax_sql.translate(m["expression"], self._resolve_col_sql)
            self.measures[m["name"]] = {**m, **t}
            self.measures[f"{m['table']}.{m['name']}"] = self.measures[m["name"]]

    # ------------------------------------------------------------ connection
    @property
    def con(self):
        if self._con is None:
            self._con = duckdb.connect(str(self.db_path), read_only=True)
        return self._con

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None

    # ------------------------------------------------------------- resolving
    def _resolve_col_sql(self, table, column):
        t = self._table_of(table, column)
        return f'"{t}"."{column}"'

    def _table_of(self, table, column):
        if table and table in self.tables:
            return table
        for name, t in self.tables.items():
            if any(c["name"] == column for c in t["columns"]):
                return name
        raise KeyError(f"column [{column}] not found in the model")

    def column(self, table, column):
        t = self.tables.get(table)
        if not t:
            raise KeyError(f"table {table!r} not in the model")
        for c in t["columns"]:
            if c["name"] == column:
                return c
        raise KeyError(f"{table}[{column}] not in the model")

    def hierarchy_level(self, table, hierarchy, level):
        for h in self.tables.get(table, {}).get("hierarchies", []):
            if h["name"] == hierarchy:
                for lv in h["levels"]:
                    if lv["name"] == level:
                        return lv["column"]
        # A level name that matches a column is the common fallback.
        try:
            self.column(table, level)
            return level
        except KeyError:
            raise KeyError(f"{table}.{hierarchy}.{level} not found")

    def parse_ref(self, ref):
        """Accept the spec's field dicts, or a string shorthand."""
        if isinstance(ref, dict):
            k = ref.get("kind")
            if k == "column":
                return {"kind": "column", "table": ref["table"], "column": ref["column"],
                        "ref": ref.get("ref") or f"{ref['table']}.{ref['column']}"}
            if k == "hierarchyLevel":
                col = self.hierarchy_level(ref["table"], ref["hierarchy"], ref["level"])
                return {"kind": "column", "table": ref["table"], "column": col,
                        "ref": ref.get("ref"), "level": ref["level"]}
            if k == "aggregation":
                return {"kind": "aggregation", "function": ref["function"],
                        "table": ref["table"], "column": ref["column"],
                        "ref": ref.get("ref") or f"{ref['function']}({ref['table']}.{ref['column']})"}
            if k == "measure":
                name = ref.get("measure") or ref.get("name")
                return {"kind": "measure", "name": name, "ref": ref.get("ref") or name}
            raise KeyError(f"unsupported field kind {k!r}")

        s = str(ref).strip()
        m = AGG_RE.match(s)
        if m and m.group("fn") in AGG_SQL:
            table, _, column = m.group("inner").rpartition(".")
            return {"kind": "aggregation", "function": m.group("fn"),
                    "table": table or self._table_of(None, column),
                    "column": column, "ref": s}
        if s in self.measures:
            return {"kind": "measure", "name": self.measures[s]["name"], "ref": s}
        parts = s.split(".")
        if len(parts) == 3:
            col = self.hierarchy_level(parts[0], parts[1], parts[2])
            return {"kind": "column", "table": parts[0], "column": col, "ref": s, "level": parts[2]}
        if len(parts) == 2:
            if parts[0] in self.tables:
                try:
                    self.column(parts[0], parts[1])
                    return {"kind": "column", "table": parts[0], "column": parts[1], "ref": s}
                except KeyError:
                    pass
            if parts[1] in self.measures:
                return {"kind": "measure", "name": parts[1], "ref": s}
        raise KeyError(f"cannot resolve field {s!r}")

    # ----------------------------------------------------------- join graph
    def _edges(self, override=None):
        """Undirected adjacency. ``override`` swaps one relationship's key,
        which is how USERELATIONSHIP is honoured."""
        edges = {}
        rels = []
        for r in self.relationships:
            if r["isActive"]:
                rels.append(r)
        if override:
            ov = {"fromTable": override["fromTable"], "fromColumn": override["fromColumn"],
                  "toTable": override["toTable"], "toColumn": override["toColumn"],
                  "isActive": True}
            rels = [r for r in rels
                    if not (r["fromTable"] == ov["fromTable"] and r["toTable"] == ov["toTable"])]
            rels.append(ov)
        for r in rels:
            edges.setdefault(r["fromTable"], []).append((r["toTable"], r, True))
            edges.setdefault(r["toTable"], []).append((r["fromTable"], r, False))
        return edges

    def join_path(self, base, target, override=None):
        if base == target:
            return []
        edges = self._edges(override)
        seen, q = {base}, deque([(base, [])])
        while q:
            node, path = q.popleft()
            for nxt, rel, forward in edges.get(node, []):
                if nxt in seen:
                    continue
                newpath = path + [(node, nxt, rel, forward)]
                if nxt == target:
                    return newpath
                seen.add(nxt)
                q.append((nxt, newpath))
        raise KeyError(f"no relationship path from {base} to {target}")

    def base_table(self, measures):
        for m in measures:
            f = self.parse_ref(m)
            if f["kind"] == "aggregation":
                return f["table"]
            if f["kind"] == "measure":
                return self.measures[f["name"]]["table"]
        # No measures: the biggest table wins.
        return max(self.tables.values(), key=lambda t: t.get("rowCount", 0))["name"]

    # --------------------------------------------------------------- queries
    def query(self, dims=(), measures=(), filters=(), order=None, limit=None,
              include_sort_keys=True, base=None):
        """Returns (sql, DataFrame). Column names are the field refs."""
        dfields = [self.parse_ref(d) for d in dims]
        mfields = [self.parse_ref(m) for m in measures]
        base = base or self.base_table(measures)

        groups = {}      # relationship-override signature -> [measure fields]
        for f in mfields:
            ov = None
            if f["kind"] == "measure":
                spec = self.measures[f["name"]]
                if spec.get("unsupported"):
                    raise ValueError(
                        f"measure [{f['name']}] has no SQL translation: {spec['unsupported']}")
                ov = spec.get("userelation")
            groups.setdefault(json.dumps(ov, sort_keys=True), []).append(f)

        selects = []
        for sig, fields in groups.items():
            ov = json.loads(sig)
            selects.append(self._aggregate_sql(base, dfields, fields, filters, ov,
                                               include_sort_keys))
        sql = selects[0] if len(selects) == 1 else self._merge(selects, dfields,
                                                               include_sort_keys)
        sql = self._wrap_order(sql, dfields, mfields, order, limit, include_sort_keys)
        df = self.con.execute(sql).fetchdf()
        return sql, df

    def _aggregate_sql(self, base, dfields, mfields, filters, override, include_sort_keys):
        needed = {f["table"] for f in dfields if f.get("table")}
        needed |= {f["table"] for f in mfields if f.get("table")}
        needed |= {f["table"] for f in filters if isinstance(f, dict) and f.get("table")}
        joins, seen = [], {base}
        for t in sorted(needed - {base}):
            for a, b, rel, _fwd in self.join_path(base, t, override):
                if b in seen:
                    continue
                left = f'"{rel["fromTable"]}"."{rel["fromColumn"]}"'
                right = f'"{rel["toTable"]}"."{rel["toColumn"]}"'
                joins.append(f'LEFT JOIN "{b}" ON {left} = {right}')
                seen.add(b)

        cols, group = [], []
        for f in dfields:
            expr = f'"{f["table"]}"."{f["column"]}"'
            cols.append(f'{expr} AS "{f["ref"]}"')
            group.append(expr)
            if include_sort_keys:
                sk = self.column(f["table"], f["column"]).get("sortByColumn")
                if sk:
                    skexpr = f'"{f["table"]}"."{sk}"'
                    cols.append(f'{skexpr} AS "{f["ref"]}__sort"')
                    group.append(skexpr)
        for f in mfields:
            cols.append(f'{self._measure_sql(f)} AS "{f["ref"]}"')

        where = self._where(filters)
        sql = (f'SELECT {", ".join(cols)}\nFROM "{base}"\n'
               + ("\n".join(joins) + "\n" if joins else "")
               + (f"WHERE {where}\n" if where else "")
               + (f"GROUP BY {', '.join(group)}" if group else ""))
        return sql

    def _measure_sql(self, f):
        if f["kind"] == "aggregation":
            tpl = AGG_SQL.get(f["function"], "SUM")
            col = f'"{f["table"]}"."{f["column"]}"'
            return tpl % col if "%s" in tpl else f"{tpl}({col})"
        spec = self.measures[f["name"]]
        return spec["sql"]

    def _merge(self, selects, dfields, include_sort_keys):
        """FULL OUTER JOIN the per-relationship aggregates on the dimensions."""
        keys = [f'"{f["ref"]}"' for f in dfields]
        if include_sort_keys:
            for f in dfields:
                if self.column(f["table"], f["column"]).get("sortByColumn"):
                    keys.append(f'"{f["ref"]}__sort"')
        parts = [f"q{i} AS (\n{s}\n)" for i, s in enumerate(selects)]
        if not keys:
            sel = "SELECT " + ", ".join(f"q{i}.*" for i in range(len(selects)))
            frm = " CROSS JOIN ".join(f"q{i}" for i in range(len(selects)))
            return "WITH " + ",\n".join(parts) + f"\n{sel}\nFROM {frm}"
        coalesced = [f"COALESCE({', '.join(f'q{i}.{k}' for i in range(len(selects)))}) AS {k}"
                     for k in keys]
        others = []
        for i, s in enumerate(selects):
            for line in re.findall(r'AS ("(?:[^"]|"")+")', s.split("FROM")[0]):
                if line not in keys:
                    others.append(f"q{i}.{line}")
        joins = " ".join(
            f"FULL OUTER JOIN q{i} ON " + " AND ".join(
                f"q0.{k} IS NOT DISTINCT FROM q{i}.{k}" for k in keys)
            for i in range(1, len(selects)))
        return ("WITH " + ",\n".join(parts) + "\nSELECT " + ", ".join(coalesced + others)
                + f"\nFROM q0 {joins}")

    def _wrap_order(self, sql, dfields, mfields, order, limit, include_sort_keys):
        terms = []
        for o in (order or []):
            if isinstance(o, str):
                terms.append(f'"{o}"')
                continue
            f = self.parse_ref(o.get("field", o))
            direction = "DESC" if str(o.get("direction", "asc")).lower().startswith("desc") else "ASC"
            terms.append(f'"{f["ref"]}" {direction}')
        if not terms and dfields:
            for f in dfields:
                key = f["ref"]
                if include_sort_keys and self.column(f["table"], f["column"]).get("sortByColumn"):
                    key = f"{f['ref']}__sort"
                terms.append(f'"{key}" ASC')
        out = sql
        if terms:
            out = f"SELECT * FROM (\n{sql}\n) ORDER BY {', '.join(terms)}"
        if limit:
            out += f"\nLIMIT {int(limit)}"
        return out

    def _where(self, filters):
        clauses = []
        for f in filters or []:
            if not isinstance(f, dict) or f.get("op") in (None, "unsupported"):
                continue
            if f.get("op") == "or":
                branches = [" AND ".join(self._clause(c) for c in br if self._clause(c))
                            for br in f.get("branches", [])]
                branches = [b for b in branches if b]
                if branches:
                    clauses.append("(" + " OR ".join(branches) + ")")
                continue
            c = self._clause(f)
            if c:
                clauses.append(c)
        return " AND ".join(clauses)

    def _clause(self, f):
        if not f.get("column"):
            return ""
        col = f'"{f["table"]}"."{f["column"]}"'
        op = f.get("op")
        vals = f.get("values") or []
        if op in ("in", "not_in"):
            if not vals:
                return ""
            lits = ", ".join(_lit(v) for v in vals)
            nulls = any(v is None for v in vals)
            base = f"{col} IN ({lits})"
            if nulls:
                base = f"({base} OR {col} IS NULL)"
            return base if op == "in" else f"NOT ({base})"
        sym = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(op)
        if sym and vals:
            return f"{col} {sym} {_lit(vals[0])}"
        return ""

    # -------------------------------------------------------------- describe
    def describe(self) -> str:
        out = [f"# {self.model['name']}",
               f"{len(self.tables)} tables, {self.model['totalRows']:,} rows, "
               f"{len(self.model['measures'])} measures", ""]
        for t in self.model["tables"]:
            out.append(f"## {t['name']}  ({t.get('rowCount', 0):,} rows)")
            for c in t["columns"]:
                bits = [c["dataType"]]
                if c.get("isHidden"):
                    bits.append("hidden")
                if c.get("sortByColumn"):
                    bits.append(f"sorted by [{c['sortByColumn']}]")
                if c.get("isCalculated"):
                    bits.append(f"calculated: {c['expression']}")
                out.append(f"  - [{c['name']}]  ({', '.join(bits)})")
            for h in t.get("hierarchies", []):
                out.append(f"  - hierarchy {h['name']}: "
                           + " > ".join(lv["name"] for lv in h["levels"]))
            out.append("")
        if self.model["measures"]:
            out.append("## Measures")
            for m in self.model["measures"]:
                spec = self.measures[m["name"]]
                out.append(f"  - [{m['name']}] = {m['expression']}")
                out.append(f"      SQL: {spec['sql'] or 'UNSUPPORTED - ' + str(spec['unsupported'])}")
                if spec.get("userelation"):
                    u = spec["userelation"]
                    out.append(f"      via {u['fromTable']}[{u['fromColumn']}] -> "
                               f"{u['toTable']}[{u['toColumn']}]")
            out.append("")
        out.append("## Relationships")
        for r in self.relationships:
            flag = "" if r["isActive"] else "  (inactive)"
            out.append(f"  - {r['fromTable']}[{r['fromColumn']}] -> "
                       f"{r['toTable']}[{r['toColumn']}]{flag}")
        return "\n".join(out)


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Describe or query the extracted model")
    ap.add_argument("model_dir", nargs="?", default="build/model")
    ap.add_argument("--describe", action="store_true")
    ap.add_argument("--dims", default="")
    ap.add_argument("--measures", default="")
    ap.add_argument("--sql", help="run raw SQL instead")
    a = ap.parse_args()
    sm = SemanticModel(a.model_dir)
    if a.sql:
        print(sm.con.execute(a.sql).fetchdf().to_string())
    elif a.describe or not (a.dims or a.measures):
        print(sm.describe())
    else:
        sql, df = sm.query(dims=[d for d in a.dims.split(",") if d],
                           measures=[m for m in a.measures.split(",") if m])
        print(sql, "\n")
        print(df.to_string())

#!/usr/bin/env python3
"""Stage 5: the interface an agent uses to hold a conversation with the model.

    python3 tools/pbi_chat.py schema
    python3 tools/pbi_chat.py measures
    python3 tools/pbi_chat.py sample Sales -n 5
    python3 tools/pbi_chat.py query --dims "Product.Category" \
        --measures "Sum(Sales.Sales Amount)" --filters "Date.Fiscal Year in FY2019"
    python3 tools/pbi_chat.py sql "SELECT ..." --format md

There is no language model in here. The agent reading a question *is* the
language model; this gives it a safe, typed way to reach the data:

* ``schema`` and ``measures`` tell it what exists, including which DAX measures
  have a working SQL translation and which do not
* ``query`` builds the joins from the model's relationships, so the agent asks
  in terms of fields rather than getting them wrong by hand
* ``sql`` is the escape hatch for anything the semantic layer cannot express

Every path is read-only: the connection is opened read-only, statements are
checked against a deny list, and results are capped. An agent cannot damage the
extracted model through this tool, which is what makes it safe to hand to one.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbi_common import PROJECT_ROOT, read_json  # noqa: E402
from semantic import SemanticModel              # noqa: E402

import pandas as pd                            # noqa: E402

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|"
    r"install|load|pragma|set|call|vacuum|checkpoint)\b", re.I)

FILTER_RE = re.compile(
    r"^\s*(?P<field>[^<>=!]+?)\s+(?P<op>not in|in|>=|<=|<>|!=|=|>|<)\s+(?P<value>.+?)\s*$",
    re.I)

OPS = {"in": "in", "not in": "not_in", "=": "eq", "<>": "ne", "!=": "ne",
       ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}


def parse_filter(text: str) -> dict:
    """`Date.Fiscal Year in FY2019,FY2020` -> a filter dict.

    Values are split on commas; wrap a value in quotes to keep a comma in it.
    """
    m = FILTER_RE.match(text)
    if not m:
        raise SystemExit(f"cannot read filter {text!r}. Use: <Table.Column> <op> <value[,value]>")
    field = m.group("field").strip()
    table, _, column = field.rpartition(".")
    raw = m.group("value").strip()
    values = [_coerce(v) for v in _split_values(raw)]
    return {"table": table, "column": column, "op": OPS[m.group("op").lower()],
            "values": values}


def _split_values(raw: str):
    out, buf, quote = [], "", None
    for ch in raw:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf += ch
        elif ch in "'\"":
            quote = ch
        elif ch == ",":
            out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip() or not out:
        out.append(buf.strip())
    return [v for v in out if v != ""]


def _coerce(v: str):
    low = v.lower()
    if low in ("null", "blank", "none"):
        return None
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


KEYLIKE = re.compile(r"(key|id)$", re.I)


def column_formats(df) -> dict:
    """A display format per numeric column, or None to leave the column alone.

    One rule gets at least one column wrong, so there are three. Money needs
    separators and cents. A count needs separators and no cents, but only once
    it is long enough to need them - a fiscal year is not 2,019. A surrogate
    key needs neither: 20170707 is a date key, not twenty million.
    """
    specs = {}
    for col in df.columns:
        series = df[col]
        if not pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
            specs[col] = None
        elif KEYLIKE.search(str(col)):
            specs[col] = ".0f"
        else:
            values = series.dropna()
            if len(values) and bool((values == values.round()).all()):
                specs[col] = ",.0f" if float(values.abs().max()) >= 10000 else ".0f"
            else:
                specs[col] = ",.2f"
    return specs


def as_display(df):
    """Format every numeric column to a string, once.

    Both output paths need this and neither can be trusted to do it: pandas'
    display.float_format prints a total as 9.46205e+07 in markdown, because
    to_markdown hands tabulate df.values, which upcasts every int column to
    float on the way. Formatting here means the two agree by construction.
    """
    specs = column_formats(df)
    out = df.copy()
    for col, spec in specs.items():
        if spec:
            out[col] = df[col].map(
                lambda v, s=spec: "" if pd.isna(v) else format(v, s))
    return out, specs


def emit(df, fmt: str, sql: str | None = None, show_sql: bool = False) -> None:
    if show_sql and sql:
        print("-- sql\n" + sql + "\n")
    if fmt == "json":
        print(df.to_json(orient="records", indent=1, date_format="iso"))
    elif fmt == "csv":
        print(df.to_csv(index=False), end="")
    elif fmt == "md":
        shown, specs = as_display(df)
        print(shown.to_markdown(
            index=False, disable_numparse=True,
            colalign=["right" if specs[c] else "left" for c in df.columns]))
    else:
        shown, _ = as_display(df)
        print(shown.to_string(index=False))
    print(f"\n[{len(df)} rows]", file=sys.stderr)


def main() -> None:
    # The shared options are attached to both the top level and every
    # subcommand, so an agent can write them on either side of the verb.
    # SUPPRESS keeps the subparser from overwriting a value given before the
    # verb; the defaults are filled in once, after parsing.
    S = argparse.SUPPRESS
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=S)
    common.add_argument("--format", default=S, choices=["table", "json", "csv", "md"])
    common.add_argument("--limit", type=int, default=S, help="row cap on any result")
    common.add_argument("--show-sql", action="store_true", default=S)

    ap = argparse.ArgumentParser(description=__doc__, parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("schema", parents=[common], help="tables, columns, hierarchies, relationships")
    sub.add_parser("measures", parents=[common], help="DAX measures and their SQL translation")
    sub.add_parser("roles", parents=[common], help="business roles detected by the analyzer")

    s = sub.add_parser("sample", parents=[common], help="first rows of one table")
    s.add_argument("table")
    s.add_argument("-n", type=int, default=10)

    d = sub.add_parser("distinct", parents=[common], help="distinct values of one column, with counts")
    d.add_argument("field", help="Table.Column")

    q = sub.add_parser("query", parents=[common], help="semantic query: fields in, joins handled")
    q.add_argument("--dims", default="", help="comma-separated field refs")
    q.add_argument("--measures", default="", help="comma-separated measures or Agg(Table.Column)")
    q.add_argument("--filters", action="append", default=[],
                   help="repeatable: \"Date.Fiscal Year in FY2019\"")
    q.add_argument("--order", default="", help="field ref, optionally with :desc")

    r = sub.add_parser("sql", parents=[common], help="read-only SQL against the extracted tables")
    r.add_argument("statement")

    a = ap.parse_args()
    for key, default in (("model", str(PROJECT_ROOT / "build" / "model")),
                         ("format", "table"), ("limit", 200), ("show_sql", False)):
        if not hasattr(a, key):
            setattr(a, key, default)
    pd.set_option("display.float_format", lambda v: f"{v:,.2f}")
    pd.set_option("display.max_colwidth", 60)
    sm = SemanticModel(Path(a.model))

    if a.cmd == "schema":
        print(sm.describe())
        return

    if a.cmd == "measures":
        for name, spec in sorted({m["name"]: sm.measures[m["name"]]
                                  for m in sm.model["measures"]}.items()):
            print(f"[{name}]  (table {spec['table']})")
            print(f"  DAX: {spec['expression']}")
            print(f"  SQL: {spec['sql'] or 'not translatable - ' + str(spec['unsupported'])}")
            if spec.get("userelation"):
                u = spec["userelation"]
                print(f"  joins {u['fromTable']}[{u['fromColumn']}] -> {u['toTable']}[{u['toColumn']}]")
            if spec.get("filters"):
                print(f"  extra filters: {spec['filters']}")
            print()
        return

    if a.cmd == "roles":
        f = Path(a.model).parent / "analysis" / "findings.json"
        if not f.exists():
            f = PROJECT_ROOT / "build" / "analysis" / "findings.json"
        if not f.exists():
            raise SystemExit("run tools/analyze_model.py first")
        data = read_json(f)
        for k, v in data["roles"].items():
            print(f"{k:<14} {v or '-':<34} {data['roleEvidence'].get(k, '')}")
        return

    if a.cmd == "sample":
        df = sm.con.execute(f'SELECT * FROM "{a.table}" LIMIT {int(a.n)}').fetchdf()
        emit(df, a.format)
        return

    if a.cmd == "distinct":
        table, _, column = a.field.rpartition(".")
        sql = (f'SELECT "{column}" AS value, COUNT(*) AS rows FROM "{table}" '
               f'GROUP BY 1 ORDER BY 2 DESC LIMIT {int(a.limit)}')
        emit(sm.con.execute(sql).fetchdf(), a.format, sql, a.show_sql)
        return

    if a.cmd == "query":
        dims = [x.strip() for x in a.dims.split(",") if x.strip()]
        measures = [x.strip() for x in a.measures.split(",") if x.strip()]
        filters = [parse_filter(f) for f in a.filters]
        order = None
        if a.order:
            ref, _, direction = a.order.partition(":")
            order = [{"field": ref.strip(), "direction": direction or "asc"}]
        sql, df = sm.query(dims=dims, measures=measures, filters=filters,
                           order=order, limit=a.limit)
        drop = [c for c in df.columns if c.endswith("__sort")]
        emit(df.drop(columns=drop), a.format, sql, a.show_sql)
        return

    if a.cmd == "sql":
        stmt = a.statement.strip().rstrip(";")
        if FORBIDDEN.search(stmt) or not re.match(r"^\s*(select|with|describe|show)\b", stmt, re.I):
            raise SystemExit("read-only: only SELECT / WITH / DESCRIBE / SHOW are allowed")
        if not re.search(r"\blimit\b", stmt, re.I):
            stmt = f"SELECT * FROM (\n{stmt}\n) LIMIT {int(a.limit)}"
        emit(sm.con.execute(stmt).fetchdf(), a.format, stmt, a.show_sql)
        return


if __name__ == "__main__":
    main()

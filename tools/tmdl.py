"""A small, dependency-free TMDL reader.

TMDL (Tabular Model Definition Language) is the text format a Power BI Desktop
"Save as .pbip" writes for the semantic model. It is indentation-structured:

    table Sales
        measure 'Sales Amount by Due Date' = ```
                CALCULATE(SUM(Sales[Sales Amount]), USERELATIONSHIP(...))
                ```
            formatString: \\#,0.00

        column 'Order Quantity'
            dataType: int64
            isHidden
            summarizeBy: sum

This module turns that into a node tree and then into plain dicts. It is
deliberately tolerant: unknown keywords are preserved as generic nodes rather
than raising, because TMDL gains properties with every Power BI release.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FENCE = "```"


@dataclass
class Node:
    kind: str = ""           # table / column / measure / partition / annotation ...
    name: str = ""           # object name, quotes stripped
    expr: str | None = None  # right-hand side of "= ..." including fenced blocks
    props: dict = field(default_factory=dict)   # "dataType: int64" -> {"dataType": "int64"}
    flags: list = field(default_factory=list)   # bare lines such as "isHidden"
    children: list = field(default_factory=list)

    def find(self, kind: str):
        return [c for c in self.children if c.kind == kind]

    def first(self, kind: str):
        for c in self.children:
            if c.kind == kind:
                return c
        return None


def _indent(line: str) -> int:
    n = 0
    for ch in line:
        if ch == "\t":
            n += 1
        elif ch == " ":
            n += 0.25  # tolerate space-indented files
        else:
            break
    return int(n)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1].replace(s[0] * 2, s[0])
    return s


_HEAD_RE = re.compile(r"^(?P<kind>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<rest>.*)$")


def parse(text: str) -> Node:
    lines = text.replace("\r\n", "\n").split("\n")
    root = Node(kind="root")
    stack: list[tuple[int, Node]] = [(-1, root)]
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.strip().startswith("//"):
            i += 1
            continue
        ind = _indent(raw)
        body = raw.strip()

        # Attach to the nearest shallower parent.
        while stack and stack[-1][0] >= ind:
            stack.pop()
        parent = stack[-1][1]

        expr = None
        # "<something> = <value>"  |  "<something> ="  |  "<something> = ```"
        eq = _split_assignment(body)
        if eq is not None:
            body, rhs = eq
            if rhs.startswith(FENCE):
                expr, i = _read_fence(lines, i)
            elif rhs == "":
                expr, i = _read_block(lines, i, ind)
            else:
                expr = rhs

        m = _HEAD_RE.match(body)
        if not m:
            i += 1
            continue
        kind, rest = m.group("kind"), m.group("rest").strip()

        if ":" in body and expr is None and not rest.startswith("'"):
            # property line: "dataType: int64"
            key, _, val = body.partition(":")
            if " " not in key.strip():
                parent.props[key.strip()] = val.strip()
                i += 1
                continue

        if not rest and expr is None:
            parent.flags.append(kind)
            i += 1
            continue

        node = Node(kind=kind, name=_unquote(rest), expr=expr)
        parent.children.append(node)
        stack.append((ind, node))
        i += 1
    return root


def _split_assignment(body: str):
    """Split on the first top-level '=' that is not part of ':=' or '=='."""
    depth = 0
    quote = None
    for idx, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "=" and depth == 0:
            if idx and body[idx - 1] in ":<>!=":
                continue
            if idx + 1 < len(body) and body[idx + 1] == "=":
                continue
            return body[:idx].strip(), body[idx + 1:].strip()
    return None


def _read_fence(lines, i):
    """Consume a ``` fenced expression starting on line i."""
    out = []
    i += 1
    while i < len(lines) and lines[i].strip() != FENCE:
        out.append(lines[i].strip())
        i += 1
    return "\n".join(out).strip(), i + 1


def _read_block(lines, i, ind):
    """Consume an indented block that follows a bare '=' line."""
    out = []
    j = i + 1
    while j < len(lines):
        if not lines[j].strip():
            out.append("")
            j += 1
            continue
        if _indent(lines[j]) <= ind:
            break
        out.append(lines[j].strip())
        j += 1
    return "\n".join(out).strip(), j - 1


# ---------------------------------------------------------------- interpreters

def load_table(path: Path) -> dict:
    root = parse(Path(path).read_text(encoding="utf-8-sig"))
    tnode = root.first("table")
    if tnode is None:
        return {}
    table = {
        "name": tnode.name,
        "isHidden": "isHidden" in tnode.flags,
        "columns": [],
        "measures": [],
        "hierarchies": [],
        "partitions": [],
    }
    for c in tnode.find("column"):
        table["columns"].append({
            "name": c.name,
            "dataType": c.props.get("dataType"),  # absent on calculated columns
            "isHidden": "isHidden" in c.flags,
            "formatString": c.props.get("formatString"),
            "summarizeBy": c.props.get("summarizeBy", "none"),
            "sourceColumn": c.props.get("sourceColumn", c.name),
            "sortByColumn": c.props.get("sortByColumn"),
            "expression": c.expr,           # set for calculated columns
            "isCalculated": c.expr is not None,
            "description": c.props.get("description"),
        })
    for m in tnode.find("measure"):
        table["measures"].append({
            "name": m.name,
            "table": tnode.name,
            "expression": (m.expr or "").strip(),
            "formatString": m.props.get("formatString"),
            "displayFolder": m.props.get("displayFolder"),
            "description": m.props.get("description"),
            "isHidden": "isHidden" in m.flags,
        })
    for h in tnode.find("hierarchy"):
        table["hierarchies"].append({
            "name": h.name,
            "levels": [{"name": lv.name, "column": _unquote(lv.props.get("column", ""))}
                       for lv in h.find("level")],
        })
    for p in tnode.find("partition"):
        table["partitions"].append({
            "name": p.name,
            "mode": p.props.get("mode", "import"),
            "sourceType": (p.expr or "m").strip(),   # "m", "calculated", ...
            "source": (p.first("source").expr if p.first("source") else None),
        })
    return table


def load_relationships(path: Path) -> list:
    root = parse(Path(path).read_text(encoding="utf-8-sig"))
    out = []
    for r in root.find("relationship"):
        frm = r.props.get("fromColumn", "")
        to = r.props.get("toColumn", "")
        out.append({
            "name": r.name,
            "fromTable": _qual_table(frm), "fromColumn": _qual_col(frm),
            "toTable": _qual_table(to), "toColumn": _qual_col(to),
            "isActive": r.props.get("isActive", "true").lower() != "false",
            "crossFilteringBehavior": r.props.get("crossFilteringBehavior", "singleDirection"),
            "fromCardinality": r.props.get("fromCardinality", "many"),
            "toCardinality": r.props.get("toCardinality", "one"),
        })
    return out


def _split_qual(ref: str):
    """'Sales Order'.SalesOrderLineKey -> ("Sales Order", "SalesOrderLineKey")"""
    ref = ref.strip()
    if ref.startswith("'"):
        end = ref.index("'", 1)
        return ref[1:end], ref[end + 2:]
    table, _, col = ref.partition(".")
    return table, col


def _qual_table(ref: str) -> str:
    return _split_qual(ref)[0]


def _qual_col(ref: str) -> str:
    return _unquote(_split_qual(ref)[1])

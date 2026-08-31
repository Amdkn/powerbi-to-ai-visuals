#!/usr/bin/env python3
"""A deliberately small DAX -> SQL translator.

Full DAX is a language with its own evaluation model; this is not that. It
covers the measure shapes that dominate real reports - an aggregate, an
arithmetic combination of aggregates, DIVIDE, and CALCULATE with a filter or a
USERELATIONSHIP - and refuses everything else loudly instead of guessing.

A refusal is not a dead end: the measure still lands in model.json with the
original DAX and a ``reason``, so an agent can hand-write the SQL and register
it. Silence would be the dangerous behaviour; a wrong number that looks right
is worse than a gap.

Returned by :func:`translate`:
    sql          SQL fragment over the model's tables, or None
    grain        "aggregate" (safe to SUM per group) or "row"
    userelation  {fromTable, fromColumn, toTable, toColumn} when CALCULATE
                 re-routes a relationship, so the query builder can swap joins
    filters      extra filters CALCULATE applies
    unsupported  reason string when sql is None
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|--[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:[^"]|"")*")
  | (?P<qname>'(?:[^']|'')*')
  | (?P<bracket>\[(?:[^\]]|\]\])*\])
  | (?P<number>\d+\.\d+|\.\d+|\d+)
  | (?P<op><=|>=|<>|&&|\|\||[-+*/^<>=&,()])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
""", re.X | re.S)


@dataclass
class Tok:
    kind: str
    text: str


def tokenize(src: str) -> list:
    out, pos = [], 0
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        if not m:
            raise DaxError(f"cannot tokenize at {src[pos:pos + 24]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        out.append(Tok(kind, m.group()))
    return out


class DaxError(Exception):
    pass


# --------------------------------------------------------------------- parser

@dataclass
class Node:
    kind: str                       # num | str | col | measure | func | binop | unary
    value: str = ""
    table: str | None = None
    args: list = field(default_factory=list)


class Parser:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def eat(self, text=None):
        tok = self.peek()
        if tok is None:
            raise DaxError("unexpected end of expression")
        if text and tok.text.upper() != text.upper():
            raise DaxError(f"expected {text!r}, got {tok.text!r}")
        self.i += 1
        return tok

    def parse(self):
        n = self.expr()
        if self.peek():
            raise DaxError(f"trailing tokens from {self.peek().text!r}")
        return n

    def expr(self):
        """Lowest precedence first: OR, AND, comparison, then arithmetic."""
        n = self.and_expr()
        while self.peek() and self.peek().text.upper() in ("||", "OR"):
            self.eat()
            n = Node("binop", "OR", args=[n, self.and_expr()])
        return n

    def and_expr(self):
        n = self.cmp_expr()
        while self.peek() and self.peek().text.upper() in ("&&", "AND"):
            self.eat()
            n = Node("binop", "AND", args=[n, self.cmp_expr()])
        return n

    def cmp_expr(self):
        n = self.arith()
        if self.peek() and self.peek().text in ("=", "<>", "<", ">", "<=", ">="):
            op = self.eat().text
            n = Node("binop", op, args=[n, self.arith()])
        return n

    def arith(self):
        n = self.term()
        while self.peek() and self.peek().text in ("+", "-", "&"):
            op = self.eat().text
            n = Node("binop", op, args=[n, self.term()])
        return n

    def term(self):
        n = self.factor()
        while self.peek() and self.peek().text in ("*", "/"):
            op = self.eat().text
            n = Node("binop", op, args=[n, self.factor()])
        return n

    def factor(self):
        tok = self.peek()
        if tok and tok.text == "-":
            self.eat()
            return Node("unary", "-", args=[self.factor()])
        return self.primary()

    def primary(self):
        tok = self.peek()
        if tok is None:
            raise DaxError("unexpected end of expression")
        if tok.kind == "number":
            self.eat()
            return Node("num", tok.text)
        if tok.kind == "string":
            self.eat()
            return Node("str", tok.text[1:-1].replace('""', '"'))
        if tok.text == "(":
            self.eat("(")
            n = self.expr()
            self.eat(")")
            return n
        if tok.kind == "bracket":                        # [Measure] or [Column]
            self.eat()
            return Node("measure", _unbracket(tok.text))
        if tok.kind == "qname":                          # 'Sales Order'[Col]
            self.eat()
            table = tok.text[1:-1].replace("''", "'")
            nxt = self.peek()
            if nxt and nxt.kind == "bracket":
                self.eat()
                return Node("col", _unbracket(nxt.text), table=table)
            return Node("tableref", table)
        if tok.kind == "ident":
            self.eat()
            nxt = self.peek()
            if nxt and nxt.kind == "bracket":
                self.eat()
                return Node("col", _unbracket(nxt.text), table=tok.text)
            if nxt and nxt.text == "(":
                self.eat("(")
                args = []
                if self.peek() and self.peek().text != ")":
                    args.append(self.expr())
                    while self.peek() and self.peek().text == ",":
                        self.eat(",")
                        args.append(self.expr())
                self.eat(")")
                return Node("func", tok.text.upper(), args=args)
            return Node("tableref", tok.text)
        raise DaxError(f"unexpected token {tok.text!r}")


def _unbracket(text: str) -> str:
    return text[1:-1].replace("]]", "]")


# ------------------------------------------------------------------- compiler

SIMPLE_AGG = {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
              "COUNT": "COUNT", "COUNTA": "COUNT", "DISTINCTCOUNT": "COUNT(DISTINCT %s)"}


class Compiler:
    def __init__(self, resolve_column):
        self.resolve_column = resolve_column      # (table, column) -> "T"."C"
        self.userelation = None
        self.filters = []
        self.grain = "row"

    def compile(self, node: Node) -> str:
        return getattr(self, f"_{node.kind}")(node)

    def _num(self, n):
        return n.value

    def _str(self, n):
        return "'" + n.value.replace("'", "''") + "'"

    def _col(self, n):
        return self.resolve_column(n.table, n.value)

    def _measure(self, n):
        raise DaxError(f"reference to measure [{n.value}] - translate it first")

    def _tableref(self, n):
        raise DaxError(f"bare table reference {n.value!r} is not supported here")

    def _unary(self, n):
        return f"(-{self.compile(n.args[0])})"

    def _binop(self, n):
        a, b = (self.compile(x) for x in n.args)
        if n.value == "&":
            return f"({a} || {b})"
        if n.value == "/":
            return f"({a} / NULLIF({b}, 0))"
        op = {"=": "=", "<>": "<>", "AND": "AND", "OR": "OR"}.get(n.value, n.value)
        return f"({a} {op} {b})"

    def _func(self, n):
        name = n.value
        if name in SIMPLE_AGG:
            self.grain = "aggregate"
            arg = self.compile(n.args[0])
            tpl = SIMPLE_AGG[name]
            return tpl % arg if "%s" in tpl else f"{tpl}({arg})"
        if name == "COUNTROWS":
            self.grain = "aggregate"
            return "COUNT(*)"
        if name == "SUMX":
            self.grain = "aggregate"
            if len(n.args) != 2:
                raise DaxError("SUMX(table, expression) expected")
            return f"SUM({self.compile(n.args[1])})"
        if name in ("AVERAGEX", "MINX", "MAXX", "COUNTX"):
            self.grain = "aggregate"
            fn = {"AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT"}[name]
            return f"{fn}({self.compile(n.args[1])})"
        if name == "DIVIDE":
            num = self.compile(n.args[0])
            den = self.compile(n.args[1])
            alt = self.compile(n.args[2]) if len(n.args) > 2 else "NULL"
            return f"COALESCE({num} / NULLIF({den}, 0), {alt})"
        if name in ("IF", "IFERROR"):
            if name == "IF" and len(n.args) >= 2:
                cond = self.compile(n.args[0])
                a = self.compile(n.args[1])
                b = self.compile(n.args[2]) if len(n.args) > 2 else "NULL"
                return f"CASE WHEN {cond} THEN {a} ELSE {b} END"
            raise DaxError(f"{name} form not supported")
        if name == "CALCULATE":
            return self._calculate(n)
        if name == "USERELATIONSHIP":
            self._userelationship(n)
            return ""
        if name in ("ABS", "ROUND", "FLOOR", "CEILING", "SQRT", "LOG", "EXP"):
            return f"{name}({', '.join(self.compile(a) for a in n.args)})"
        if name == "BLANK":
            return "NULL"
        raise DaxError(f"{name}() is not in the supported subset")

    def _calculate(self, n):
        if not n.args:
            raise DaxError("CALCULATE needs an expression")
        body = self.compile(n.args[0])
        for extra in n.args[1:]:
            if extra.kind == "func" and extra.value == "USERELATIONSHIP":
                self._userelationship(extra)
            elif extra.kind == "binop" and extra.value in ("=", "<", ">", "<=", ">=", "<>"):
                left, right = extra.args
                if left.kind != "col":
                    raise DaxError("CALCULATE filter must compare a column")
                self.filters.append({
                    "table": left.table, "column": left.value,
                    "op": {"=": "eq", "<>": "ne", "<": "lt", ">": "gt",
                           "<=": "lte", ">=": "gte"}[extra.value],
                    "values": [_const(right)]})
            else:
                raise DaxError("only USERELATIONSHIP and simple column filters "
                               "are supported inside CALCULATE")
        return body

    def _userelationship(self, n):
        if len(n.args) != 2 or any(a.kind != "col" for a in n.args):
            raise DaxError("USERELATIONSHIP(column, column) expected")
        a, b = n.args
        self.userelation = {"fromTable": a.table, "fromColumn": a.value,
                            "toTable": b.table, "toColumn": b.value}


def _const(node: Node):
    if node.kind == "num":
        return float(node.value) if "." in node.value else int(node.value)
    if node.kind == "str":
        return node.value
    raise DaxError("filter value must be a constant")


def translate(dax: str, resolve_column) -> dict:
    """dax -> {sql, grain, userelation, filters, unsupported}"""
    out = {"dax": (dax or "").strip(), "sql": None, "grain": None,
           "userelation": None, "filters": [], "unsupported": None}
    if not out["dax"]:
        out["unsupported"] = "empty expression"
        return out
    try:
        ast = Parser(tokenize(out["dax"])).parse()
        c = Compiler(resolve_column)
        sql = c.compile(ast)
        if c.grain != "aggregate":
            raise DaxError("expression does not reduce to an aggregate")
        out.update(sql=sql, grain=c.grain, userelation=c.userelation, filters=c.filters)
    except DaxError as e:
        out["unsupported"] = str(e)
    except Exception as e:                       # parser bug or exotic syntax
        out["unsupported"] = f"{type(e).__name__}: {e}"
    return out


if __name__ == "__main__":
    import json
    import sys

    def rc(t, c):
        return f'"{t}"."{c}"'

    for expr in sys.argv[1:]:
        print(json.dumps(translate(expr, rc), indent=2))

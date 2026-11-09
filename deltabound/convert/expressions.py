"""Translate legacy (BODS) expressions to Spark SQL expressions.

A small tokenizer and Pratt parser build an AST; the emitter maps functions and
operators. Anything without a mapping becomes a Todo instead of a guess, and the
caller marks that transform as not converted.

Tool-specific note: the grammar and FUNCTIONS table follow SAP Data Services
expressions. A second source tool gets its own table here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

AGGREGATES = {"sum", "count", "count_distinct", "max", "min", "avg"}

# BODS date masks -> Spark datetime patterns, longest tokens first.
DATE_MASK = [("YYYY", "yyyy"), ("YY", "yy"), ("MONTH", "MMMM"), ("MON", "MMM"), ("MM", "MM"), ("DD", "dd"),
             ("HH24", "HH"), ("HH", "hh"), ("MI", "mm"), ("SS", "ss"), ("DAY", "EEEE"), ("DY", "EEE")]


class TranslationError(Exception):
    pass


# ---------- tokens ----------

TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<string>'(?:[^']|'')*')
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<var>\$[A-Za-z_][\w$]*)
  | (?P<ident>[A-Za-z_][\w$#]*)
  | (?P<op><>|!=|<=|>=|\|\||[=<>+\-*/(),.\[\]])
""", re.VERBOSE)


@dataclass
class Tok:
    kind: str
    text: str


def tokenize(text: str) -> list[Tok]:
    out, pos = [], 0
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            raise TranslationError(f"unexpected character {text[pos]!r} at {pos}")
        pos = m.end()
        if m.lastgroup != "ws":
            out.append(Tok(m.lastgroup or "", m.group()))
    return out


# ---------- AST ----------

@dataclass
class Lit:
    sql: str  # literal already in Spark SQL form


@dataclass
class Var:
    name: str  # without '$'


@dataclass
class Col:
    parts: list[str]


@dataclass
class Call:
    name: str
    args: list["Node"]


@dataclass
class Unary:
    op: str
    arg: "Node"


@dataclass
class Binary:
    op: str
    left: "Node"
    right: "Node"


@dataclass
class Postfix:  # IS [NOT] NULL
    op: str
    arg: "Node"


@dataclass
class Between:
    arg: "Node"
    low: "Node"
    high: "Node"
    negated: bool


@dataclass
class InList:
    arg: "Node"
    items: list["Node"]
    negated: bool


Node = Union[Lit, Var, Col, Call, Unary, Binary, Postfix, Between, InList]

BINARY = {"OR": 1, "AND": 2, "=": 4, "<>": 4, "!=": 4, "<": 4, ">": 4, "<=": 4, ">=": 4, "LIKE": 4,
          "||": 5, "+": 6, "-": 6, "*": 7, "/": 7}


class Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0

    def peek(self, k: int = 0) -> Optional[Tok]:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def word(self, k: int = 0) -> str:
        t = self.peek(k)
        return t.text.upper() if t and t.kind in ("ident", "op") else ""

    def take(self, text: Optional[str] = None) -> Tok:
        t = self.peek()
        if t is None or (text is not None and t.text.upper() != text):
            raise TranslationError(f"expected {text or 'more input'}, found {t.text if t else 'end'}")
        self.i += 1
        return t

    def parse(self) -> Node:
        node = self.expr(0)
        if self.peek() is not None:
            raise TranslationError(f"unexpected {self.peek().text!r}")
        return node

    def expr(self, min_prec: int) -> Node:
        left = self.prefix()
        while True:
            w = self.word()
            if w == "IS" and min_prec <= 4:
                self.take()
                neg = self.word() == "NOT"
                if neg:
                    self.take()
                self.take("NULL")
                left = Postfix("IS NOT NULL" if neg else "IS NULL", left)
                continue
            negated = w == "NOT" and self.word(1) in ("LIKE", "IN", "BETWEEN")
            op = self.word(1) if negated else w
            if op == "BETWEEN" and min_prec <= 4:
                self.take() if not negated else (self.take(), self.take())
                low = self.expr(5)
                self.take("AND")
                left = Between(left, low, self.expr(5), negated)
                continue
            if op == "IN" and min_prec <= 4:
                self.take() if not negated else (self.take(), self.take())
                self.take("(")
                items = [self.expr(0)]
                while self.word() == ",":
                    self.take()
                    items.append(self.expr(0))
                self.take(")")
                left = InList(left, items, negated)
                continue
            prec = BINARY.get(op)
            if prec is None or prec < min_prec:
                return left
            if negated:
                self.take()
            self.take()
            right = self.expr(prec + 1)
            left = Binary(f"NOT {op}" if negated else op, left, right)

    def prefix(self) -> Node:
        t = self.take()
        up = t.text.upper()
        if t.kind == "number":
            return Lit(t.text)
        if t.kind == "string":
            body = t.text[1:-1]  # BODS '' stays '' (Spark accepts it); backslash is literal in BODS
            return Lit("'" + body.replace("\\", "\\\\") + "'")
        if t.kind == "var":
            return Var(t.text[1:])
        if up == "NULL":
            return Lit("NULL")
        if up == "NOT":
            return Unary("NOT", self.expr(3))
        if t.text in ("-", "+"):
            return Unary(t.text, self.expr(8))
        if t.text == "(":
            node = self.expr(0)
            self.take(")")
            return node
        if t.kind == "ident":
            if self.peek() is not None and self.peek().text == "(":
                self.take("(")
                args: list[Node] = []
                if self.word() != ")":
                    args.append(self.expr(0))
                    while self.word() == ",":
                        self.take()
                        args.append(self.expr(0))
                self.take(")")
                return Call(t.text.lower(), args)
            parts = [t.text]
            while self.peek() is not None and self.peek().text == "." and self.peek(1) and self.peek(1).kind == "ident":
                self.take()
                parts.append(self.take().text)
            return Col(parts)
        raise TranslationError(f"unexpected {t.text!r}")


# ---------- emitting ----------

def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def date_mask(lit: Node) -> str:
    if not isinstance(lit, Lit) or not lit.sql.startswith("'"):
        raise TranslationError("date format mask is not a literal")
    mask = lit.sql[1:-1]
    out, i = [], 0
    while i < len(mask):
        for src, dst in DATE_MASK:
            if mask[i:].upper().startswith(src):
                out.append(dst)
                i += len(src)
                break
        else:
            ch = mask[i]
            out.append(ch if not ch.isalpha() else f"''{ch}''")  # quote stray letters as literal text
            i += 1
    return "'" + "".join(out) + "'"


@dataclass
class Translation:
    sql: str
    todos: list[str] = field(default_factory=list)
    variables: set[str] = field(default_factory=set)
    has_aggregate: bool = False


class Emitter:
    def __init__(self, placeholders: Optional[dict[str, str]] = None):
        self.placeholders = placeholders or {}  # identifier -> ready SQL (used for lookup calls)
        self.todos: list[str] = []
        self.variables: set[str] = set()
        self.has_aggregate = False

    def emit(self, n: Node) -> str:
        if isinstance(n, Lit):
            return n.sql
        if isinstance(n, Var):
            self.variables.add(n.name)
            return f"system.session.{n.name}"
        if isinstance(n, Col):
            if len(n.parts) == 1 and n.parts[0] in self.placeholders:
                return self.placeholders[n.parts[0]]
            return ".".join(q(p) for p in n.parts)
        if isinstance(n, Unary):
            return f"(NOT {self.emit(n.arg)})" if n.op == "NOT" else f"{n.op}{self.emit(n.arg)}"
        if isinstance(n, Binary):
            op = "<>" if n.op == "!=" else n.op
            return f"({self.emit(n.left)} {op} {self.emit(n.right)})"
        if isinstance(n, Postfix):
            return f"({self.emit(n.arg)} {n.op})"
        if isinstance(n, Between):
            return f"({self.emit(n.arg)} {'NOT ' if n.negated else ''}BETWEEN {self.emit(n.low)} AND {self.emit(n.high)})"
        if isinstance(n, InList):
            return f"({self.emit(n.arg)} {'NOT ' if n.negated else ''}IN ({', '.join(self.emit(i) for i in n.items)}))"
        assert isinstance(n, Call)
        rule = FUNCTIONS.get(n.name)
        if rule is None:
            self.todos.append(f"function {n.name}() has no Spark mapping")
            return f"{n.name}({', '.join(self.emit(a) for a in n.args)})"
        if n.name in AGGREGATES:
            self.has_aggregate = True
        try:
            return rule(self, n.args)
        except TranslationError as e:
            self.todos.append(f"{n.name}(): {e}")
            return f"{n.name}({', '.join(self.emit(a) for a in n.args)})"


def _same(spark_name: str, arity: Optional[set[int]] = None) -> Callable[[Emitter, list[Node]], str]:
    def rule(em: Emitter, args: list[Node]) -> str:
        if arity is not None and len(args) not in arity:
            raise TranslationError(f"unsupported number of arguments ({len(args)})")
        return f"{spark_name}({', '.join(em.emit(a) for a in args)})"
    return rule


def _to_date(em: Emitter, a: list[Node]) -> str:
    if len(a) == 1:
        return f"to_date({em.emit(a[0])})"
    if len(a) == 2:
        return f"to_date({em.emit(a[0])}, {date_mask(a[1])})"
    raise TranslationError("expects 1 or 2 arguments")


def _to_char(em: Emitter, a: list[Node]) -> str:
    if len(a) == 1:
        return f"CAST({em.emit(a[0])} AS STRING)"
    if len(a) == 2:
        return f"date_format({em.emit(a[0])}, {date_mask(a[1])})"
    raise TranslationError("expects 1 or 2 arguments")


def _decode(em: Emitter, a: list[Node]) -> str:
    if len(a) < 3 or len(a) % 2 == 0:
        raise TranslationError("expects condition/value pairs and a default")
    whens = " ".join(f"WHEN {em.emit(a[i])} THEN {em.emit(a[i + 1])}" for i in range(0, len(a) - 1, 2))
    return f"CASE {whens} ELSE {em.emit(a[-1])} END"


def _ifthenelse(em: Emitter, a: list[Node]) -> str:
    if len(a) != 3:
        raise TranslationError("expects 3 arguments")
    return f"IF({em.emit(a[0])}, {em.emit(a[1])}, {em.emit(a[2])})"


def _count_distinct(em: Emitter, a: list[Node]) -> str:
    return f"COUNT(DISTINCT {', '.join(em.emit(x) for x in a)})"


def _now(em: Emitter, a: list[Node]) -> str:
    if a:
        raise TranslationError("expects no arguments")
    return "current_timestamp()"


FUNCTIONS: dict[str, Callable[[Emitter, list[Node]], str]] = {
    "nvl": _same("coalesce", {2}),
    "ifthenelse": _ifthenelse,
    "decode": _decode,
    "sysdate": _now,
    "systime": _now,
    "to_date": _to_date,
    "to_char": _to_char,
    "to_number": lambda em, a: f"CAST({em.emit(a[0])} AS DECIMAL(38,9))" if len(a) == 1 else _bad(),
    "to_decimal": lambda em, a: f"CAST({em.emit(a[0])} AS DECIMAL(38,9))" if len(a) == 1 else _bad(),
    "upper": _same("upper", {1}),
    "lower": _same("lower", {1}),
    "initcap": _same("initcap", {1}),
    "length": _same("length", {1}),
    "ltrim": _same("ltrim", {1}),
    "rtrim": _same("rtrim", {1}),
    "lpad": _same("lpad", {3}),
    "rpad": _same("rpad", {3}),
    "substr": _same("substring", {2, 3}),
    "replace_substr": _same("replace", {3}),
    "round": _same("round", {1, 2}),
    "trunc": _same("trunc", {2}),
    "ceil": _same("ceil", {1}),
    "floor": _same("floor", {1}),
    "abs": _same("abs", {1}),
    "mod": _same("mod", {2}),
    "month": _same("month", {1}),
    "year": _same("year", {1}),
    "day_in_month": _same("dayofmonth", {1}),
    "add_months": _same("add_months", {2}),
    "sum": _same("sum", {1}),
    "count": _same("count", {1}),
    "max": _same("max", {1}),
    "min": _same("min", {1}),
    "avg": _same("avg", {1}),
    "count_distinct": _count_distinct,
}


def _bad() -> str:
    raise TranslationError("unsupported number of arguments")


def translate(expr: str, placeholders: Optional[dict[str, str]] = None) -> Translation:
    """Translate one expression. Never raises: problems are returned as todos."""
    em = Emitter(placeholders)
    try:
        sql = em.emit(Parser(expr).parse())
    except TranslationError as e:
        return Translation(sql=expr, todos=[f"could not parse expression: {e}"])
    return Translation(sql=sql, todos=em.todos, variables=em.variables, has_aggregate=em.has_aggregate)


def strip_outer_parens(sql: str) -> str:
    """'(a = b)' -> 'a = b' when the parentheses wrap the whole expression."""
    if not (sql.startswith("(") and sql.endswith(")")):
        return sql
    depth, in_quote = 0, False
    for i, ch in enumerate(sql):
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote:
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(sql) - 1:
                return sql
    return sql[1:-1]

"""One dataflow -> one Databricks notebook (Python source format).

Each transform becomes a cell that defines a DataFrame and a temporary view of
the same name, mostly as a Spark SQL statement close to the original logic.
A transform without a rule becomes a cell that raises NotImplementedError: the
notebook fails loudly until a person has reviewed it. Never a silent guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .. import graph
from ..ir import Estate, EtlObject, LookupRef, Transform
from .config import ConvertConfig
from .expressions import q, strip_outer_parens, translate
from .model import DataflowResult, TransformResult

HEADER = "# Databricks notebook source"
CELL = "\n\n# COMMAND ----------\n\n"

SOURCE_KINDS = {"table_source", "file_source"}
TARGET_KINDS = {"table_target", "file_target"}
JOIN_SQL = {"inner": "INNER JOIN", "left_outer": "LEFT OUTER JOIN", "right_outer": "RIGHT OUTER JOIN",
            "full_outer": "FULL OUTER JOIN", "cross": "CROSS JOIN"}
FLIP = {"left_outer": "right_outer", "right_outer": "left_outer"}
# Source-tool option names the converter understands (BODS); see Transform.options.
OPT_LOAD_MODE = "loadMode"
OVERWRITE_MODES = {"truncate", "replace", "overwrite"}

RUNTIME = '''# deltabound runtime helpers (generated)
from delta.tables import DeltaTable
from pyspark.sql import functions as F


def _sql_literal(value):
    if value is None or value == "":
        return "NULL"
    return "'" + str(value).replace("\\\\", "\\\\\\\\").replace("'", "\\\\'") + "'"


def _set_var(name, sql_type, value):
    """Legacy variable -> Spark session variable, usable in every SQL statement below."""
    spark.sql(f"DECLARE OR REPLACE VARIABLE {name} {sql_type}")
    spark.sql(f"SET VARIABLE {name} = CAST({_sql_literal(value)} AS {sql_type})")'''

SCD1 = '''

def _scd1_merge(src, table, keys, compare):
    """Upsert: update changed rows, insert new keys. Identity key columns fill themselves."""
    compare = compare or [c for c in src.columns if c not in keys]
    on = " AND ".join(f"t.`{k}` = s.`{k}`" for k in keys)
    changed = " OR ".join(f"NOT (t.`{c}` <=> s.`{c}`)" for c in compare)
    (DeltaTable.forName(spark, table).alias("t")
        .merge(src.alias("s"), on)
        .whenMatchedUpdate(condition=changed, set={c: f"s.`{c}`" for c in src.columns if c not in keys})
        .whenNotMatchedInsert(values={c: f"s.`{c}`" for c in src.columns})
        .execute())'''

SCD2 = '''

def _scd2_merge(src, table, keys, compare, valid_from, valid_to, flag, current, noncurrent, open_end, effective):
    """History preserving: expire the current version of changed rows and insert new versions."""
    compare = compare or [c for c in src.columns if c not in keys]
    changed = " OR ".join(f"NOT (t.`{c}` <=> s.`{c}`)" for c in compare)
    current_rows = spark.table(table).where(f"`{flag}` = {_sql_literal(current)}")
    match = [F.col(f"s.`{k}`") == F.col(f"t.`{k}`") for k in keys]
    new_versions = (src.alias("s").join(current_rows.alias("t"), match).where(changed).select("s.*")
                    .select(*[F.lit(None).cast(src.schema[k].dataType).alias(f"_mk_{k}") for k in keys], "*"))
    staged = src.select(*[F.col(f"`{k}`").alias(f"_mk_{k}") for k in keys], "*").unionByName(new_versions)
    on = " AND ".join(f"t.`{k}` = s.`_mk_{k}`" for k in keys) + f" AND t.`{flag}` = {_sql_literal(current)}"
    values = {c: f"s.`{c}`" for c in src.columns}
    values |= {valid_from: effective, valid_to: f"DATE {_sql_literal(open_end)}", flag: _sql_literal(current)}
    (DeltaTable.forName(spark, table).alias("t")
        .merge(staged.alias("s"), on)
        .whenMatchedUpdate(condition=changed, set={valid_to: effective, flag: _sql_literal(noncurrent)})
        .whenNotMatchedInsert(values=values)
        .execute())'''


def py_name(name: str) -> str:
    return "df_" + re.sub(r"\W", "_", name)


def spark_type(data_type: Optional[str]) -> Optional[str]:
    if not data_type:
        return None
    t = data_type.strip().lower()
    if re.fullmatch(r"(n?varchar|n?char|varchar2|string|long)(\(\d+\))?", t):
        return "STRING"
    if m := re.fullmatch(r"(decimal|numeric)\((\d+),\s*(\d+)\)", t):
        return f"DECIMAL({m.group(2)},{m.group(3)})"
    return {"int": "INT", "integer": "INT", "bigint": "BIGINT", "smallint": "SMALLINT", "date": "DATE",
            "datetime": "TIMESTAMP", "timestamp": "TIMESTAMP", "double": "DOUBLE", "real": "DOUBLE",
            "float": "DOUBLE", "boolean": "BOOLEAN"}.get(t)


def indent(sql: str, n: int = 4) -> str:
    return "\n".join(" " * n + line if line else line for line in sql.splitlines())


@dataclass
class Cell:
    code: str
    result: TransformResult


@dataclass
class Context:
    estate: Estate
    df: EtlObject
    cfg: ConvertConfig
    by_name: dict[str, Transform]
    branch_owner: dict[str, str]
    consumers: dict[str, list[Transform]]
    variables: set[str] = field(default_factory=set)
    helpers: set[str] = field(default_factory=set)
    writes: list[str] = field(default_factory=list)
    ddl: list[tuple[str, str]] = field(default_factory=list)  # (table, CREATE TABLE or comment) for the setup notebook

    def cite(self, t: Transform) -> str:
        return f"df:{self.df.name}/{t.name}"

    def table(self, t: Transform) -> tuple[str, bool]:
        assert t.table is not None
        return self.cfg.table(t.table)

    def lookup_table(self, lk: LookupRef) -> tuple[str, bool]:
        assert lk.table is not None
        return self.cfg.table(lk.table)


def todo_cell(ctx: Context, t: Transform, reasons: list[str], sketch: str = "", rule: Optional[str] = None) -> Cell:
    what = f"{t.kind} ({t.raw_tag})" if t.kind == "opaque" else t.kind
    lines = [f"# source: {ctx.cite(t)} ({what})", "# TODO(deltabound): this step was not converted. Review the original and write it by hand:"]
    lines += [f"#   - {r}" for r in reasons]
    if sketch:
        lines += ["# Partial translation for reference:"] + [f"# {l}" for l in sketch.splitlines()]
    lines.append(f'raise NotImplementedError("TODO(deltabound): {ctx.cite(t)} needs manual conversion")')
    return Cell("\n".join(lines), TransformResult(cite=ctx.cite(t), kind=t.kind, rule=rule, status="todo", todos=reasons))


def view(name: str, code: str) -> str:
    return f'{code}\n{py_name(name)}.createOrReplaceTempView("{name}")'


# ---------- rules ----------

def rule_table_source(ctx: Context, t: Transform) -> Cell:
    if t.table is None:
        return todo_cell(ctx, t, ["source has no table reference"])
    name, mapped = ctx.table(t)
    res = TransformResult(cite=ctx.cite(t), kind=t.kind, rule="table_source")
    if not mapped:
        res.notes.append(f"datastore {t.table.datastore} is not mapped in [convert.datastores]; using {name}")
    code = view(t.name, f'# source: {ctx.cite(t)} (table_source)\n{py_name(t.name)} = spark.read.table("{name}")')
    return Cell(code, res)


def _lookup_join(ctx: Context, lk: LookupRef, alias: str, res: TransformResult) -> Optional[tuple[str, list[str]]]:
    """LEFT JOIN to the lookup table, one row per match key chosen by the return policy.

    Returns (join SQL, [column SQL per return column]) or None (with a todo recorded).
    """
    if lk.table is None:
        res.todos.append(f"{lk.function} on a table that is only known at run time: {lk.raw or ''}".strip())
        return None
    if not lk.return_columns or not lk.match_columns:
        res.todos.append(f"{lk.function} without return or match columns")
        return None
    order = {"max": "DESC", "min": "ASC", None: "DESC"}.get(lk.policy)
    if order is None:
        res.todos.append(f"lookup return policy {lk.policy!r} has no rule (only max/min)")
        return None
    if lk.policy is None:
        res.notes.append(f"{alias}: no return policy given; when several rows match, the highest {lk.return_columns[0]} wins")
    table, mapped = ctx.lookup_table(lk)
    if not mapped:
        res.notes.append(f"lookup table datastore {lk.table.datastore} is not mapped; using {table}")
    match_cols = [c for c, _ in lk.match_columns]
    on = [f"{q(alias)}.`_rn` = 1"]
    for col, expr in lk.match_columns:
        tr = translate(expr)
        res.todos += tr.todos
        ctx.variables |= tr.variables
        on.append(f"{q(alias)}.{q(col)} = {tr.sql}")
    cols = ", ".join(q(c) for c in dict.fromkeys(match_cols + lk.return_columns))
    sub = (f"LEFT JOIN (\n  SELECT {cols},\n    ROW_NUMBER() OVER (PARTITION BY {', '.join(q(c) for c in match_cols)} "
           f"ORDER BY {q(lk.return_columns[0])} {order}) AS `_rn`\n  FROM {table}\n) AS {q(alias)}\n  ON {' AND '.join(on)}")
    values = []
    for i, ret in enumerate(lk.return_columns):
        default = lk.default_values[i] if i < len(lk.default_values) else "NULL"
        d = translate(default)
        res.todos += d.todos
        values.append(f"coalesce({q(alias)}.{q(ret)}, {d.sql})")
    if lk.cache:
        res.notes.append(f"{alias}: legacy cache hint {lk.cache} ignored (Spark plans the join itself)")
    return sub, values


def _from_clause(t: Transform, res: TransformResult) -> tuple[str, list[str]]:
    """FROM ... JOIN ... built from the inputs and join pairs; extra conditions go to WHERE."""
    if not t.inputs:
        res.todos.append("query has no inputs")
        return "", []
    present = [t.inputs[0]]
    parts = [f"FROM {q(t.inputs[0])}"]
    extra: list[str] = []
    pending = list(t.joins)
    while pending:
        progressed = False
        for j in list(pending):
            cond = translate(j.condition) if j.condition else None
            if cond:
                res.todos += cond.todos
            on = f" ON {strip_outer_parens(cond.sql)}" if cond and j.join_type != "cross" else ""
            if j.left in present and j.right not in present:
                parts.append(f"{JOIN_SQL.get(j.join_type, 'INNER JOIN')} {q(j.right)}{on}")
                present.append(j.right)
            elif j.right in present and j.left not in present:
                parts.append(f"{JOIN_SQL.get(FLIP.get(j.join_type, j.join_type), 'INNER JOIN')} {q(j.left)}{on}")
                present.append(j.left)
            elif j.left in present and j.right in present:
                if cond:
                    extra.append(strip_outer_parens(cond.sql))
            else:
                continue
            if j.join_type not in JOIN_SQL:
                res.todos.append(f"join type {j.join_type!r} has no rule")
            pending.remove(j)
            progressed = True
        if not progressed:
            res.todos.append("join pairs do not connect to the first input: " + ", ".join(f"{j.left}-{j.right}" for j in pending))
            break
    for i in t.inputs:
        if i not in present:
            parts.append(f"CROSS JOIN {q(i)}")
            present.append(i)
            res.notes.append(f"{i} has no join condition; joined as CROSS JOIN (conditions may be in WHERE)")
    return "\n".join(parts), extra


def rule_query(ctx: Context, t: Transform) -> Cell:
    res = TransformResult(cite=ctx.cite(t), kind=t.kind, rule="query")
    lookup_joins: list[str] = []
    select: list[str] = []
    has_agg = False
    n = 0
    for c in t.columns:
        expr = c.expression or "NULL"
        placeholders: dict[str, str] = {}
        for lk in [lk for lk in t.lookups if lk.output_column == c.column]:
            n += 1
            joined = _lookup_join(ctx, lk, f"_lk{n}", res)
            if joined is None or lk.raw is None or lk.raw not in expr:
                if joined is not None:
                    res.todos.append(f"lookup for {c.column} could not be placed in its expression")
                continue
            lookup_joins.append(joined[0])
            key = f"__LK{n}__"
            placeholders[key] = joined[1][0]
            expr = expr.replace(lk.raw, key, 1)
        tr = translate(expr, placeholders)
        res.todos += [f"{c.column}: {x}" for x in tr.todos]
        ctx.variables |= tr.variables
        has_agg = has_agg or tr.has_aggregate
        sql = strip_outer_parens(tr.sql)
        typ = spark_type(c.data_type)
        if c.data_type and typ is None:
            res.notes.append(f"{c.column}: legacy type {c.data_type} not mapped; column left uncast")
        select.append(f"CAST({sql} AS {typ}) AS {q(c.column)}" if typ else f"{sql} AS {q(c.column)}")
    if not select:
        res.todos.append("query defines no output columns")
    from_sql, extra = _from_clause(t, res)
    lines = ["SELECT", indent(",\n".join(select)), from_sql, *lookup_joins]
    where = []
    if t.where:
        w = translate(t.where)
        res.todos += [f"where: {x}" for x in w.todos]
        ctx.variables |= w.variables
        where.append(strip_outer_parens(w.sql))
    where += extra
    if where:
        lines.append("WHERE " + "\n  AND ".join(f"({w})" if len(where) > 1 else w for w in where))
    if t.group_by:
        g = [translate(x) for x in t.group_by]
        for x in g:
            res.todos += [f"group by: {y}" for y in x.todos]
            ctx.variables |= x.variables
        lines.append("GROUP BY " + ", ".join(strip_outer_parens(x.sql) for x in g))
    elif has_agg:
        res.notes.append("aggregates without GROUP BY: one row for the whole input")
    if t.order_by:
        feeds_file = any(c.kind == "file_target" for c in ctx.consumers.get(t.name, []))
        if feeds_file:
            o = [(translate(s.expression), s.direction) for s in t.order_by]
            for x, _ in o:
                res.todos += [f"order by: {y}" for y in x.todos]
            lines.append("ORDER BY " + ", ".join(f"{strip_outer_parens(x.sql)} {d.upper()}" for x, d in o))
        else:
            res.notes.append("ORDER BY dropped: row order is not kept in Delta tables")
    sql = "\n".join(lines)
    if res.todos:
        return todo_cell(ctx, t, res.todos, sketch=sql, rule="query")
    code = view(t.name, f'# source: {ctx.cite(t)} (query)\n{py_name(t.name)} = spark.sql("""\n{sql}\n""")')
    return Cell(code, res)


def rule_lookup(ctx: Context, t: Transform) -> Cell:
    res = TransformResult(cite=ctx.cite(t), kind=t.kind, rule="lookup")
    if len(t.inputs) != 1 or not t.lookups:
        return todo_cell(ctx, t, ["lookup transform needs exactly one input and a lookup definition"])
    joins, cols = [], []
    for i, lk in enumerate(t.lookups, 1):
        joined = _lookup_join(ctx, lk, f"_lk{i}", res)
        if joined:
            joins.append(joined[0])
            cols += [f"{v} AS {q(r)}" for v, r in zip(joined[1], lk.return_columns)]
    sql = "\n".join([f"SELECT {q(t.inputs[0])}.*,", indent(",\n".join(cols)), f"FROM {q(t.inputs[0])}", *joins])
    if res.todos:
        return todo_cell(ctx, t, res.todos, sketch=sql, rule="lookup")
    return Cell(view(t.name, f'# source: {ctx.cite(t)} (lookup)\n{py_name(t.name)} = spark.sql("""\n{sql}\n""")'), res)


def rule_case(ctx: Context, t: Transform) -> Cell:
    res = TransformResult(cite=ctx.cite(t), kind=t.kind, rule="case")
    if len(t.inputs) != 1 or not t.branches:
        return todo_cell(ctx, t, ["case needs exactly one input and at least one branch"])
    res.notes.append("assumes the first matching branch wins (BODS option 'row can be TRUE for one case only'); "
                     "check the original setting")
    conds: list[str] = []
    cells = [f"# source: {ctx.cite(t)} (case): one view per branch; NULL conditions count as false"]
    for b in t.branches:
        if b.is_default:
            where = "NOT (" + " OR ".join(f"coalesce({c}, false)" for c in conds) + ")" if conds else "true"
        else:
            tr = translate(b.condition or "NULL")
            res.todos += [f"branch {b.name}: {x}" for x in tr.todos]
            ctx.variables |= tr.variables
            c = strip_outer_parens(tr.sql)
            where = f"coalesce({c}, false)" + "".join(f" AND NOT coalesce({p}, false)" for p in conds)
            conds.append(c)
        cells.append(view(b.name, f'{py_name(b.name)} = spark.sql("""SELECT * FROM {q(t.inputs[0])} WHERE {where}""")'))
    if res.todos:
        return todo_cell(ctx, t, res.todos, sketch="\n".join(cells[1:]), rule="case")
    return Cell("\n".join(cells), res)


def rule_merge(ctx: Context, t: Transform) -> Cell:
    res = TransformResult(cite=ctx.cite(t), kind=t.kind, rule="merge")
    if len(t.inputs) < 2:
        return todo_cell(ctx, t, ["merge needs at least two inputs"])
    expr = py_name(t.inputs[0]) + "".join(f".unionByName({py_name(i)})" for i in t.inputs[1:])
    res.notes.append("inputs must have the same columns (BODS Merge requires it); unionByName fails otherwise")
    return Cell(view(t.name, f"# source: {ctx.cite(t)} (merge)\n{py_name(t.name)} = {expr}"), res)


def rule_table_target(ctx: Context, t: Transform) -> Cell:
    res = TransformResult(cite=ctx.cite(t), kind=t.kind, rule="table_target")
    if t.table is None or len(t.inputs) != 1:
        return todo_cell(ctx, t, ["target needs a table and exactly one input"])
    name, mapped = ctx.table(t)
    if not mapped:
        res.notes.append(f"datastore {t.table.datastore} is not mapped in [convert.datastores]; using {name}")
    load = t.options.get(OPT_LOAD_MODE, "append").lower()
    mode = "overwrite" if load in OVERWRITE_MODES else "append"
    if load not in OVERWRITE_MODES | {"append"}:
        res.todos.append(f"load mode {load!r} has no rule")
        return todo_cell(ctx, t, res.todos, rule="table_target")
    ctx.writes.append(name)
    _add_ddl(ctx, name, _columns_of(ctx, t.inputs[0]), res)
    code = (f"# source: {ctx.cite(t)} (table_target, load mode {load})\n"
            f'{py_name(t.inputs[0])}.write.format("delta").mode("{mode}").saveAsTable("{name}")')
    return Cell(code, res)


RULES = {
    "table_source": rule_table_source,
    "query": rule_query,
    "lookup": rule_lookup,
    "case": rule_case,
    "merge": rule_merge,
    "table_target": rule_table_target,
}


# ---------- slowly changing dimension pattern (TC [-> HP] [-> KG] -> target) ----------

@dataclass
class ScdChain:
    tc: Transform
    hp: Optional[Transform]
    kg: Optional[Transform]
    target: Transform


def find_scd_chains(ctx: Context) -> list[ScdChain]:
    chains = []
    for target in ctx.df.transforms:
        if target.kind != "table_target" or len(target.inputs) != 1:
            continue
        hp = kg = None
        cur = ctx.by_name.get(target.inputs[0])
        if cur is not None and cur.kind == "key_generation" and len(cur.inputs) == 1:
            kg, cur = cur, ctx.by_name.get(cur.inputs[0])
        if cur is not None and cur.kind == "history_preserving" and len(cur.inputs) == 1:
            hp, cur = cur, ctx.by_name.get(cur.inputs[0])
        if cur is not None and cur.kind == "table_comparison" and len(cur.inputs) == 1:
            chains.append(ScdChain(cur, hp, kg, target))
    return chains


def scd_cell(ctx: Context, ch: ScdChain) -> Cell:
    t = ch.target
    res = TransformResult(cite=ctx.cite(t), kind="table_target", rule="scd2_merge" if ch.hp else "scd1_merge")
    steps = " -> ".join(x.name for x in (ch.tc, ch.hp, ch.kg, t) if x is not None)
    if not ch.tc.key_columns:
        return todo_cell(ctx, t, [f"{ch.tc.name}: comparison key columns are not in the export"], rule=res.rule)
    if t.table is None:
        return todo_cell(ctx, t, ["target has no table"], rule=res.rule)
    name, mapped = ctx.table(t)
    if not mapped:
        res.notes.append(f"datastore {t.table.datastore} is not mapped in [convert.datastores]; using {name}")
    if ch.tc.table is not None and ch.tc.table.qualified != t.table.qualified:
        res.notes.append(f"{ch.tc.name} compares against {ch.tc.table.qualified} but writes {t.table.qualified}")
    if ch.kg is not None:
        res.notes.append(f"{ch.kg.name}: key {ch.kg.generated_key or '?'} becomes a Delta identity column "
                         "(values differ from legacy keys)")
    src = py_name(ch.tc.inputs[0])
    keys, compare = repr(ch.tc.key_columns), repr(ch.tc.compare_columns)
    ctx.writes.append(name)
    cols = _columns_of(ctx, ch.tc.inputs[0])
    if ch.hp is None:
        ctx.helpers.add("scd1")
        _add_ddl(ctx, name, cols, res, identity=ch.kg.generated_key if ch.kg else None)
        call = f'_scd1_merge({src}, "{name}", keys={keys}, compare={compare})'
    else:
        ctx.helpers.add("scd2")
        d, h = ctx.cfg.scd, ch.hp.history
        vf = (h and h.valid_from) or d.valid_from
        vt = (h and h.valid_to) or d.valid_to
        flag = (h and h.current_flag) or d.current_flag
        cur = (h and h.current_value) or d.current_value
        noncur = d.noncurrent_value or ("0" if cur == "1" else "N")
        end = (h and h.open_end) or d.open_end
        defaulted = [n for n, v in (("valid_from", h and h.valid_from), ("valid_to", h and h.valid_to),
                                    ("current flag", h and h.current_flag)) if not v]
        if defaulted:
            res.notes.append(f"{ch.hp.name}: {', '.join(defaulted)} not in the export; using defaults "
                             f"{vf}/{vt}/{flag}='{cur}'")
        _add_ddl(ctx, name, cols, res, identity=ch.kg.generated_key if ch.kg else None,
                 history=[(vf, "DATE"), (vt, "DATE"), (flag, "STRING")])
        call = (f'_scd2_merge({src}, "{name}", keys={keys}, compare={compare},\n'
                f'            valid_from="{vf}", valid_to="{vt}", flag="{flag}", current="{cur}", noncurrent="{noncur}",\n'
                f'            open_end="{end}", effective="{d.effective_date}")')
    code = f"# source: {ctx.cite(t)} (slowly changing dimension: {steps})\n{call}"
    return Cell(code, res)


# ---------- target table DDL ----------

def _columns_of(ctx: Context, output: str) -> Optional[list[tuple[str, Optional[str]]]]:
    """Typed (Spark) columns at a transform output; None if unknown."""
    cols = graph.output_columns(ctx.estate, ctx.df.name, output)
    return None if cols is None else [(c, spark_type(t)) for c, t in cols]


def _add_ddl(ctx: Context, table: str, cols: Optional[list[tuple[str, Optional[str]]]], res: TransformResult,
             identity: Optional[str] = None, history: Optional[list[tuple[str, str]]] = None) -> None:
    if cols is None or any(t is None for _, t in cols):
        res.notes.append(f"target schema not fully known; create {table} before running (no DDL generated)")
        ctx.ddl.append((table, f"-- {table}: schema not fully known from the export; create it by hand"))
        return
    defs = []
    if identity:
        defs.append(f"{q(identity)} BIGINT GENERATED ALWAYS AS IDENTITY")
    defs += [f"{q(c)} {t}" for c, t in cols if c != identity]
    defs += [f"{q(c)} {t}" for c, t in (history or []) if c not in {x for x, _ in cols}]
    ctx.ddl.append((table, f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(f"  {d}" for d in defs) + "\n) USING DELTA"))


# ---------- assembly ----------

def topo_order(ctx: Context) -> list[Transform]:
    deps = {t.name: {ctx.branch_owner.get(i, i) for i in t.inputs} & set(ctx.by_name) for t in ctx.df.transforms}
    order, done = [], set()
    while len(order) < len(ctx.df.transforms):
        ready = [t for t in ctx.df.transforms if t.name not in done and deps[t.name] <= done]
        if not ready:
            raise ValueError(f"{ctx.df.name}: transforms form a cycle")
        for t in ready:  # keep document order among ready transforms
            order.append(t)
            done.add(t.name)
    return order


def convert_dataflow(estate: Estate, name: str, cfg: ConvertConfig,
                     header_note: str = "") -> tuple[str, DataflowResult, list[tuple[str, str]]]:
    """(notebook source, result, (table, DDL) for the tables this dataflow writes)."""
    df = estate.objects[name]
    branch_owner = {b.name: t.name for t in df.transforms for b in t.branches}
    consumers: dict[str, list[Transform]] = {}
    for t in df.transforms:
        for i in t.inputs:
            consumers.setdefault(i, []).append(t)
    ctx = Context(estate, df, cfg, {t.name: t for t in df.transforms}, branch_owner, consumers)

    chains = find_scd_chains(ctx)
    claimed = {x.name: ch for ch in chains for x in (ch.tc, ch.hp, ch.kg, ch.target) if x is not None}
    cells: list[Cell] = []
    results: list[TransformResult] = []
    for t in topo_order(ctx):
        ch = claimed.get(t.name)
        if ch is not None:
            if t is ch.target:
                cell = scd_cell(ctx, ch)
                cells.append(cell)
                results.append(cell.result)
            else:
                results.append(TransformResult(cite=ctx.cite(t), kind=t.kind, rule="scd_merge",
                                               notes=[f"converted as part of the merge into {ch.target.name}"]))
            continue
        rule = RULES.get(t.kind)
        cell = rule(ctx, t) if rule else todo_cell(ctx, t, [f"no conversion rule for {t.kind}"
                                                            + (f" ({t.raw_tag})" if t.kind == "opaque" else "")])
        cells.append(cell)
        results.append(cell.result)

    runtime = RUNTIME + (SCD1 if "scd1" in ctx.helpers else "") + (SCD2 if "scd2" in ctx.helpers else "")
    params = _parameter_cell(estate, sorted(ctx.variables))
    todo = sum(1 for r in results if r.status == "todo")
    head = (f"{HEADER}\n# Generated by deltabound from dataflow {name}. {header_note}\n"
            f"# {len(results)} transforms, {todo} TODO. Search for TODO(deltabound) before running.")
    body = CELL.join([head, runtime, params, *[c.code for c in cells]]) + "\n"
    result = DataflowResult(dataflow=name, notebook=f"notebooks/dataflows/{name}.py", transforms=results,
                            variables=sorted(ctx.variables), writes=ctx.writes)
    return body, result, ctx.ddl


def variable_type(estate: Estate, var: str) -> str:
    for o in estate.objects.values():
        for v in o.variables:
            if v.name.lstrip("$") == var:
                return spark_type(v.data_type) or "STRING"
    return "STRING"


def variable_default(estate: Estate, var: str) -> str:
    for o in estate.objects.values():
        for v in o.variables:
            if v.name.lstrip("$") == var and v.default is not None:
                return v.default
    return ""


def _parameter_cell(estate: Estate, variables: list[str]) -> str:
    if not variables:
        return "# Parameters: this dataflow uses no legacy variables."
    lines = ["# Parameters: legacy variables arrive as job/task parameters and become Spark session variables."]
    for v in variables:
        lines.append(f'dbutils.widgets.text("{v}", {variable_default(estate, v)!r})')
    for v in variables:
        lines.append(f'_set_var("{v}", "{variable_type(estate, v)}", dbutils.widgets.get("{v}"))')
    return "\n".join(lines)

"""Run a validation manifest: step-level comparisons in the job's call order.

Every check is generated SQL that runs on both engines (DuckDB, Spark): TRY_CAST,
IS NOT DISTINCT FROM, EXCEPT ALL and NOT EXISTS are common to both.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Optional

from .. import __version__, graph
from ..ir import Estate, ObjectKind, Transform
from .engines import Engine, TypeInfo, classify
from .manifest import Defaults, Manifest, Rules, Step, ValidateError, Waiver
from .model import CheckResult, Finding, StepResult, ValidationRun, worst

L, R = "l", "r"  # aliases in generated SQL


class StepContext:
    def __init__(self, engine: Engine, estate: Estate, manifest: Manifest, step: Step, index: int, today: dt.date):
        self.engine, self.estate, self.m, self.step, self.today = engine, estate, manifest, step, today
        self.d: Defaults = manifest.defaults
        self.lv, self.rv = f"legacy_{index}", f"new_{index}"
        engine.register(self.lv, step.legacy, manifest.base)
        engine.register(self.rv, step.databricks, manifest.base)
        self.lcols = dict(engine.columns(self.lv))
        self.rcols = dict(engine.columns(self.rv))
        self.lname = {c.lower(): c for c in self.lcols}
        self.rname = {c.lower(): c for c in self.rcols}
        ignore = {c.lower() for c in [*self.d.ignore_columns, *step.ignore_columns]}
        self.ignored = sorted(c for c in set(self.lname) | set(self.rname) if c in ignore)
        self.keys = [k.lower() for k in step.keys]
        missing = [k for k in self.keys if k not in self.lname or k not in self.rname]
        if missing:
            raise ValidateError(f"key column(s) {missing} not on both sides")
        self.compare = [c for c in self.lname if c in self.rname and c not in ignore and c not in self.keys]

    # ---------- SQL pieces ----------

    def q(self, ident: str) -> str:
        return self.engine.q(ident)

    def rtype(self, col: str) -> TypeInfo:
        return self.rcols[self.rname[col]]

    def lexpr(self, col: str) -> str:
        """Legacy value cast to the new side's type (CSV columns arrive as text)."""
        return f"TRY_CAST({L}.{self.q(self.lname[col])} AS {self.rtype(col).native})"

    def rexpr(self, col: str) -> str:
        return f"{R}.{self.q(self.rname[col])}"

    def cmp(self, side: str, col: str) -> str:
        """Value as compared: numbers widen on both sides so a cast never rounds a difference away."""
        fam = self.rtype(col).family
        raw = f"{L}.{self.q(self.lname[col])}" if side == L else self.rexpr(col)
        if fam in ("decimal", "integer"):
            return f"TRY_CAST({raw} AS DECIMAL(38,9))"
        if fam == "float":
            return f"TRY_CAST({raw} AS DOUBLE)"
        return self.lexpr(col) if side == L else raw

    def norm(self, expr: str, col: str, rules: Rules) -> str:
        if self.rtype(col).family != "string":
            return expr
        if rules.trim:
            expr = f"trim({expr})"
        if not rules.case_sensitive:
            expr = f"upper({expr})"
        if rules.empty_is_null:
            expr = f"nullif({expr}, '')"
        return expr

    def equal(self, col: str, rules: Rules) -> tuple[str, str]:
        """(SQL condition, human-readable rule)."""
        a, b = self.norm(self.cmp(L, col), col, rules), self.norm(self.cmp(R, col), col, rules)
        fam = self.rtype(col).family
        both_null = f"({a} IS NULL AND {b} IS NULL)"
        if fam in ("decimal", "float") and (rules.numeric_abs or rules.numeric_rel):
            within = f"abs({a} - {b}) <= {rules.numeric_abs}"
            if rules.numeric_rel:
                within = f"({within} OR abs({a} - {b}) <= {rules.numeric_rel} * abs({a}))"
            return (f"({both_null} OR coalesce({within}, false))",
                    f"within {rules.numeric_abs} absolute" + (f" or {rules.numeric_rel:%} relative" if rules.numeric_rel else ""))
        if fam == "timestamp" and rules.timestamp_seconds:
            diff = f"abs({self.engine.seconds(a)} - {self.engine.seconds(b)})"
            return f"({both_null} OR coalesce({diff} <= {rules.timestamp_seconds}, false))", f"within {rules.timestamp_seconds} s"
        desc = "exact"
        if fam == "string":
            desc = ", ".join(x for x, on in (("trimmed", rules.trim), ("case-insensitive", not rules.case_sensitive),
                                             ("'' = NULL", rules.empty_is_null)) if on) or "exact"
        return f"({a} IS NOT DISTINCT FROM {b})", desc

    def key_join(self) -> str:
        return " AND ".join(f"{self.lexpr(k)} IS NOT DISTINCT FROM {self.rexpr(k)}" for k in self.keys)

    def key_select(self) -> str:
        return ", ".join(f"{self.rexpr(k)} AS {self.q(self.rname[k])}" for k in self.keys)

    def count(self, sql: str) -> int:
        return int(next(iter(self.engine.rows(sql)[0].values())) or 0)

    def mask(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        masked = {c.lower() for c in self.d.mask_columns}
        out = []
        for r in rows:
            out.append({k: ("#" + hashlib.sha256(str(v).encode()).hexdigest()[:10] if any(m in k.lower() for m in masked)
                            and v is not None else _jsonable(v)) for k, v in r.items()})
        return out

    def waive(self, check: str, column: Optional[str]) -> Optional[Waiver]:
        return next((w for w in self.m.waivers if w.applies(self.step.dataflow, check, column, self.today)), None)


def _jsonable(v: Any) -> Any:
    from decimal import Decimal

    if isinstance(v, Decimal):  # widened for comparison: show significant digits only
        return format(v.normalize(), "f")
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _finding(ctx: StepContext, check: str, column: Optional[str], issue: str, status: str, **kw: Any) -> Finding:
    f = Finding(column=column, issue=issue, status=status, **kw)
    if status == "fail" and (w := ctx.waive(check, column)) is not None:
        f.status, f.waiver = "waived", f"{w.reason} (approved by {w.approved_by})"
    return f


# ---------- checks ----------

def check_schema(ctx: StepContext) -> CheckResult:
    findings: list[Finding] = []
    for c in sorted(set(ctx.lname) - set(ctx.rname) - set(ctx.ignored)):
        findings.append(_finding(ctx, "schema", ctx.lname[c], "missing in Databricks output", "fail"))
    for c in sorted(set(ctx.rname) - set(ctx.lname) - set(ctx.ignored)):
        findings.append(_finding(ctx, "schema", ctx.rname[c], "extra column in Databricks output", "warn"))
    expected = expected_types(ctx)
    for col, legacy_type in (expected or {}).items():
        if col not in ctx.rname or col in ctx.ignored or not legacy_type:
            continue
        exp, got = classify(legacy_type), ctx.rtype(col)
        name = ctx.rname[col]
        if exp.family == "other":
            continue
        compatible = exp.family == got.family or (exp.family == "integer" and got.family == "decimal" and got.scale == 0)
        if not compatible:
            findings.append(_finding(ctx, "schema", name, f"type {got.native} where the legacy job produces {legacy_type}", "fail"))
        elif exp.family == "decimal" and got.family == "decimal":
            if (got.scale or 0) < (exp.scale or 0):
                findings.append(_finding(ctx, "schema", name, f"{got.native} has fewer decimals than legacy {legacy_type}", "fail"))
            elif (got.precision or 0) < (exp.precision or 0):
                findings.append(_finding(ctx, "schema", name, f"{got.native} is narrower than legacy {legacy_type}", "warn"))
        elif exp.family == "string" and (n := _length(legacy_type)):
            longest = ctx.count(f"SELECT max(length({ctx.rexpr(col)})) FROM {ctx.q(ctx.rv)} {R}")
            if longest > n:
                findings.append(_finding(ctx, "schema", name, f"values up to {longest} characters; legacy {legacy_type} "
                                         f"holds {n}", "warn"))
    status = worst([f.status for f in findings]) if findings else "pass"
    summary = "columns and types match" if not findings else f"{len(findings)} issue(s)"
    if expected is None:
        summary += "; expected types unknown from the IR (only column names compared)"
    return CheckResult(name="schema", status=status, summary=summary, findings=findings,
                       metrics={"legacy_columns": len(ctx.lname), "databricks_columns": len(ctx.rname),
                                "ignored": [ctx.lname.get(c) or ctx.rname[c] for c in ctx.ignored]})


def _length(legacy_type: str) -> Optional[int]:
    import re

    m = re.fullmatch(r"\s*n?varchar2?\s*\((\d+)\)\s*", legacy_type, re.I)
    return int(m.group(1)) if m else None


def expected_types(ctx: StepContext) -> Optional[dict[str, Optional[str]]]:
    t = target_transform(ctx.estate, ctx.step)
    if t is None or len(t.inputs) != 1:
        return None
    cols = graph.output_columns(ctx.estate, ctx.step.dataflow, t.inputs[0])
    return None if cols is None else {c.lower(): ty for c, ty in cols}


def check_row_count(ctx: StepContext) -> CheckResult:
    a = ctx.count(f"SELECT count(*) FROM {ctx.q(ctx.lv)}")
    b = ctx.count(f"SELECT count(*) FROM {ctx.q(ctx.rv)}")
    diff = abs(a - b)
    ok = diff <= ctx.d.row_count_abs or (a and diff <= ctx.d.row_count_pct / 100 * a)
    tol = f"exact" if not (ctx.d.row_count_abs or ctx.d.row_count_pct) else \
        f"within {ctx.d.row_count_abs} rows or {ctx.d.row_count_pct}%"
    f = None if ok else _finding(ctx, "row_count", None, f"{a} legacy rows vs {b} Databricks rows", "fail")
    status = "pass" if ok else f.status  # type: ignore[union-attr]
    return CheckResult(name="row_count", status=status, summary=f"{a} vs {b} ({tol})",
                       metrics={"legacy": a, "databricks": b}, findings=[f] if f else [])


def check_keys(ctx: StepContext) -> tuple[CheckResult, bool]:
    """(result, keys usable for a value comparison)."""
    keys_l = ", ".join(ctx.q(ctx.lname[k]) for k in ctx.keys)
    keys_r = ", ".join(ctx.q(ctx.rname[k]) for k in ctx.keys)
    dup_l = ctx.count(f"SELECT count(*) FROM (SELECT {keys_l} FROM {ctx.q(ctx.lv)} GROUP BY {keys_l} HAVING count(*) > 1) t")
    dup_r = ctx.count(f"SELECT count(*) FROM (SELECT {keys_r} FROM {ctx.q(ctx.rv)} GROUP BY {keys_r} HAVING count(*) > 1) t")
    only = {}
    samples: list[dict] = []
    for side, a, b, alias_a, alias_b in (("legacy", ctx.lv, ctx.rv, L, R), ("databricks", ctx.rv, ctx.lv, R, L)):
        cond = ctx.key_join()
        base = f"FROM {ctx.q(a)} {alias_a} WHERE NOT EXISTS (SELECT 1 FROM {ctx.q(b)} {alias_b} WHERE {cond})"
        only[side] = ctx.count(f"SELECT count(*) {base}")
        if only[side]:
            sel = ", ".join(f"{alias_a}.{ctx.q((ctx.lname if alias_a == L else ctx.rname)[k])} AS {ctx.q(k.upper())}" for k in ctx.keys)
            rows = ctx.engine.rows(f"SELECT {sel} {base} LIMIT {ctx.d.max_sample_rows}")
            samples += [{"only_in": side, **r} for r in ctx.mask(rows)]
    findings = []
    if dup_l or dup_r:
        findings.append(_finding(ctx, "keys", None, f"duplicate keys: {dup_l} in legacy, {dup_r} in Databricks", "fail",
                                 metrics={"legacy": dup_l, "databricks": dup_r}))
    if only["legacy"]:
        findings.append(_finding(ctx, "keys", None, f"{only['legacy']} row(s) only in legacy", "fail"))
    if only["databricks"]:
        findings.append(_finding(ctx, "keys", None, f"{only['databricks']} row(s) only in Databricks", "fail"))
    status = worst([f.status for f in findings]) if findings else "pass"
    summary = "keys unique and matching" if not findings else "; ".join(f.issue for f in findings)
    return (CheckResult(name="keys", status=status, summary=summary, findings=findings, samples=samples,
                        metrics={"duplicate_legacy": dup_l, "duplicate_databricks": dup_r,
                                 "only_legacy": only["legacy"], "only_databricks": only["databricks"]}),
            not (dup_l or dup_r))


def check_values(ctx: StepContext) -> CheckResult:
    if not ctx.compare:
        return CheckResult(name="values", status="skipped", summary="no non-key columns to compare")
    rules = {c: ctx.step.rules(ctx.d, c) for c in ctx.compare}
    eq = {c: ctx.equal(c, rules[c]) for c in ctx.compare}
    join = f"FROM {ctx.q(ctx.lv)} {L} JOIN {ctx.q(ctx.rv)} {R} ON {ctx.key_join()}"
    sums = ", ".join(f"sum(CASE WHEN {eq[c][0]} THEN 0 ELSE 1 END) AS {ctx.q('m_' + c)}" for c in ctx.compare)
    counts = ctx.engine.rows(f"SELECT count(*) AS {ctx.q('matched')}, {sums} {join}")[0]
    matched = int(counts["matched"] or 0)
    findings = []
    for c in ctx.compare:
        n = int(counts["m_" + c] or 0)
        if not n:
            continue
        name = ctx.rname[c]
        rows = ctx.engine.rows(f"SELECT {ctx.key_select()}, {ctx.cmp(L, c)} AS {ctx.q('legacy')}, "
                               f"{ctx.cmp(R, c)} AS {ctx.q('databricks')} {join} WHERE NOT {eq[c][0]} "
                               f"LIMIT {ctx.d.max_sample_rows}")
        findings.append(_finding(ctx, "values", name, f"{n} of {matched} matched rows differ ({eq[c][1]})", "fail",
                                 metrics={"mismatches": n, "rate": round(n / matched, 6) if matched else None},
                                 samples=ctx.mask(rows), hints=lineage_hints(ctx, name)))
    status = worst([f.status for f in findings]) if findings else "pass"
    summary = (f"{len(ctx.compare)} column(s) equal on {matched} matched rows" if not findings
               else f"{len(findings)} of {len(ctx.compare)} column(s) differ on {matched} matched rows")
    return CheckResult(name="values", status=status, summary=summary, findings=findings,
                       metrics={"matched_rows": matched, "columns": {ctx.rname[c]: eq[c][1] for c in ctx.compare}})


def check_multiset(ctx: StepContext) -> CheckResult:
    """No keys: compare whole rows (normalised) as multisets. Tolerances do not apply here."""
    cols = sorted(ctx.compare)
    if not cols:
        return CheckResult(name="multiset", status="skipped", summary="no common columns")
    exact = Rules(numeric_abs=0, numeric_rel=0, trim=ctx.d.trim, case_sensitive=ctx.d.case_sensitive,
                  empty_is_null=ctx.d.empty_is_null)
    ls = ", ".join(f"{ctx.norm(ctx.lexpr(c), c, exact)} AS {ctx.q(ctx.rname[c])}" for c in cols)
    rs = ", ".join(f"{ctx.norm(ctx.rexpr(c), c, exact)} AS {ctx.q(ctx.rname[c])}" for c in cols)
    lsel, rsel = f"SELECT {ls} FROM {ctx.q(ctx.lv)} {L}", f"SELECT {rs} FROM {ctx.q(ctx.rv)} {R}"
    only_l = f"({lsel} EXCEPT ALL {rsel})"
    only_r = f"({rsel} EXCEPT ALL {lsel})"
    n_l = ctx.count(f"SELECT count(*) FROM {only_l} t")
    n_r = ctx.count(f"SELECT count(*) FROM {only_r} t")
    samples = [{"only_in": "legacy", **r} for r in ctx.mask(ctx.engine.rows(f"SELECT * FROM {only_l} t LIMIT {ctx.d.max_sample_rows}"))] if n_l else []
    samples += [{"only_in": "databricks", **r} for r in ctx.mask(ctx.engine.rows(f"SELECT * FROM {only_r} t LIMIT {ctx.d.max_sample_rows}"))] if n_r else []
    findings = []
    if n_l or n_r:
        findings.append(_finding(ctx, "multiset", None, f"{n_l} row(s) only in legacy, {n_r} only in Databricks "
                                 "(compared without keys, exact after normalisation)", "fail"))
    status = findings[0].status if findings else "pass"
    return CheckResult(name="multiset", status=status, samples=samples, findings=findings,
                       summary="identical rows (no keys; exact comparison)" if not findings else findings[0].issue,
                       metrics={"only_legacy": n_l, "only_databricks": n_r, "columns": [ctx.rname[c] for c in cols]})


# ---------- IR context: targets, lineage hints, cascading ----------

def target_transform(estate: Estate, step: Step) -> Optional[Transform]:
    df = estate.objects.get(step.dataflow)
    if df is None:
        return None
    return next((t for t in df.transforms if t.kind == "table_target" and t.table
                 and t.table.qualified.lower() == step.target.lower()), None)


def lineage_hints(ctx: StepContext, column: str, limit: int = 4) -> list[str]:
    t = target_transform(ctx.estate, ctx.step)
    if t is None or t.table is None:
        return []
    g = graph.column_lineage(ctx.estate, ctx.step.dataflow)
    node = next((n for n in g.nodes if n.lower() == f"{t.table.qualified}.{column}".lower()), None)
    if node is None:
        return []
    import networkx as nx

    hints = []
    for n in sorted(nx.ancestors(g, node)):
        expr = g.nodes[n].get("expression")
        if expr and expr != n:
            hints.append(f"{n} = {expr}")
    return [f"computed in {ctx.step.dataflow}: {h}" for h in hints[:limit]]


def reads(estate: Estate, dataflow: str) -> set[str]:
    df = estate.objects.get(dataflow)
    if df is None:
        return set()
    out = {t.table.qualified.lower() for t in df.transforms if t.kind == "table_source" and t.table}
    out |= {lk.table.qualified.lower() for t in df.transforms for lk in t.lookups if lk.table}
    return out


def step_order(estate: Estate, m: Manifest) -> list[Step]:
    if m.job in estate.objects and estate.objects[m.job].kind == ObjectKind.JOB:
        order = {n: i for i, n in enumerate(graph.reached_in_order(estate, m.job))}
        return sorted(m.steps, key=lambda s: order.get(s.dataflow, len(order)))
    return list(m.steps)


# ---------- entry point ----------

def run_validation(estate: Estate, m: Manifest, engine: Engine, today: Optional[dt.date] = None,
                   conversion_report: Optional[dict] = None) -> ValidationRun:
    today = today or dt.date.today()
    todos: dict[str, list[str]] = {}
    for d in (conversion_report or {}).get("dataflows", []):
        todos[d["dataflow"]] = [f"{t['cite']}: {x}" for t in d["transforms"] for x in t.get("todos", [])]
    results: list[StepResult] = []
    failed_targets: dict[str, tuple[int, str]] = {}  # target table -> (position, dataflow) of a failing step
    for i, step in enumerate(step_order(estate, m), 1):
        res = StepResult(dataflow=step.dataflow, target=step.target, status="pass",
                         convert_todos=todos.get(step.dataflow, []))
        if step.dataflow not in estate.objects:
            res.error = f"{step.dataflow} is not in the legacy export"
        try:
            ctx = StepContext(engine, estate, m, step, i, today)
            res.inputs = {"legacy": engine.fingerprint(step.legacy, m.base),
                          "databricks": engine.fingerprint(step.databricks, m.base)}
            res.rules = {"keys": step.keys, "ignored": ctx.ignored,
                         "column_rules": {c: step.rules(m.defaults, c).model_dump() for c in step.columns}}
            res.checks.append(check_schema(ctx))
            res.checks.append(check_row_count(ctx))
            if ctx.keys:
                keys, usable = check_keys(ctx)
                res.checks.append(keys)
                res.checks.append(check_values(ctx) if usable else
                                  CheckResult(name="values", status="skipped", summary="skipped: duplicate keys"))
            else:
                res.checks.append(check_multiset(ctx))
        except ValidateError as e:
            res.error = str(e)
        except Exception as e:  # engine errors: report, keep going with the next step
            res.error = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
        res.status = "fail" if res.error else worst([c.status for c in res.checks])
        if res.status == "fail":
            upstream = sorted(failed_targets[t] for t in reads(estate, step.dataflow) if t in failed_targets)
            res.cascading_from = upstream[0][1] if upstream else None  # earliest failing step in call order
            failed_targets[step.target.lower()] = (i, step.dataflow)
        results.append(res)
    statuses = [r.status for r in results]
    overall = "fail" if "fail" in statuses else "pass_with_waivers" if "waived" in statuses else "pass"
    return ValidationRun(
        job=m.job, business_date=m.business_date, engine=engine.name,
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), status=overall, steps=results,
        defaults=m.defaults.model_dump(),
        waivers=[w.model_dump(mode="json") | {"expired": bool(w.expires and w.expires < today)} for w in m.waivers],
        inputs={"ir": "sha256:" + hashlib.sha256(estate.model_dump_json().encode()).hexdigest(),
                "deltabound": __version__},
    )


def dumps(run: ValidationRun) -> str:
    return json.dumps(run.model_dump(mode="json"), indent=2, default=str) + "\n"

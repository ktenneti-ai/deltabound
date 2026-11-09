"""One job -> a Databricks Asset Bundle: notebooks, job definition, conversion report.

The call tree is flattened into tasks. Control flow mapping:
  if (simple `$VAR op literal`) -> condition_task on the parameter
  if (anything else)            -> notebook that evaluates the condition, then condition_task
  while with a retry counter    -> max_retries on the task;  other loops -> TODO (runs once)
  catch block                   -> run_if AT_LEAST_ONE_FAILED on the try tasks
  script                        -> notebook; `$VAR = expr;` statements set task values
Scripts are placed by EtlObject.script_steps (IR 0.2.1+); older IR files have no
script positions, so scripts then run first and the report flags each one.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import __version__, graph
from ..ir import IR_VERSION, CallStep, Estate, EtlObject, ObjectKind, ScriptStep
from .config import ConvertConfig
from .dataflow import CELL, HEADER, RUNTIME, convert_dataflow, variable_default, variable_type
from .expressions import translate
from .model import ConversionResult, TaskResult

SIMPLE_CONDITION = re.compile(r"^\$(\w+)\s*(=|<>|!=|<=|>=|<|>)\s*('([^']*)'|-?\d+(?:\.\d+)?)$")
CONDITION_OPS = {"=": "EQUAL_TO", "<>": "NOT_EQUAL", "!=": "NOT_EQUAL", "<": "LESS_THAN", "<=": "LESS_THAN_OR_EQUAL",
                 ">": "GREATER_THAN", ">=": "GREATER_THAN_OR_EQUAL"}
RETRY_LOOP = re.compile(r"\$\w*RETR\w*\s*<\s*(\d+)", re.IGNORECASE)
ASSIGNMENT = re.compile(r"^\$(\w+)\s*=\s*(.+)$", re.S)
RAISE = re.compile(r"^raise_exception\s*\((.*)\)$", re.I | re.S)
PRINT = re.compile(r"^print\s*\(\s*'((?:[^']|'')*)'\s*\)$", re.I | re.S)


def task_key(*parts: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]", "_", "__".join(p for p in parts if p))
    return key if len(key) <= 100 else key[:89] + "_" + hashlib.sha1(key.encode()).hexdigest()[:10]


def split_statements(script: str) -> list[str]:
    out, cur, in_quote = [], [], False
    for ch in script:
        if ch == "'":
            in_quote = not in_quote
        if ch == ";" and not in_quote:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return [s for s in out if s]


@dataclass
class State:
    deps: list[dict] = field(default_factory=list)
    run_if: Optional[str] = None


@dataclass
class JobBuilder:
    estate: Estate
    job: EtlObject
    cfg: ConvertConfig
    result: ConversionResult
    tasks: list[dict] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    var_source: dict[str, str] = field(default_factory=dict)  # variable -> task that last set it
    params: set[str] = field(default_factory=set)
    dataflow_vars: dict[str, list[str]] = field(default_factory=dict)

    # ---------- tasks ----------

    def unique(self, key: str) -> str:
        base, n = key, 2
        while key in self.keys:
            key, n = task_key(base, str(n)), n + 1
        self.keys.add(key)
        return key

    def add(self, key: str, kind: str, source: str, st: State, body: dict, variables: list[str] = (),
            todos: Optional[list[str]] = None, notes: Optional[list[str]] = None) -> str:
        key = self.unique(key)
        task: dict[str, Any] = {"task_key": key}
        if st.deps:
            task["depends_on"] = st.deps
        if st.run_if:
            task["run_if"] = st.run_if
        if "notebook_task" in body:
            base = {v: f"{{{{tasks.{self.var_source[v]}.values.{v}}}}}" for v in variables if v in self.var_source}
            if base:
                body["notebook_task"]["base_parameters"] = base
            if self.cfg.compute == "job_cluster":
                task["job_cluster_key"] = "main"
        task.update(body)
        if todos:
            task["description"] = "TODO(deltabound): " + "; ".join(todos)
        self.tasks.append(task)
        self.params |= set(variables)
        self.result.tasks.append(TaskResult(task_key=key, kind=kind, source=source, todos=todos or [], notes=notes or []))
        return key

    def notebook(self, path: str) -> dict:
        return {"notebook_task": {"notebook_path": f"../{path}"}}

    # ---------- walking the call tree ----------

    def walk(self, obj: EtlObject, st: State, path: tuple[str, ...]) -> State:
        # Calls and scripts in flow order. IR < 0.2.1 has no script positions: scripts go first (flagged).
        steps: list[tuple] = [(c.order, 0, 0, "call", c) for c in obj.calls]
        if obj.script_steps:
            steps += [(sc.after_call, 1, sc.index, "script", sc) for sc in obj.script_steps]
        else:
            steps += [(0, 1, i, "script", ScriptStep(index=i)) for i in range(len(obj.scripts))]
        steps.sort(key=lambda x: x[:3])
        start = len(self.tasks)
        branch_conds: dict[str, str] = {}  # base condition text -> condition task key
        branch_exits: list[dict] = []
        pending_branch: Optional[State] = None  # state before the current if/else group
        catch: Optional[State] = None  # running state inside a catch block
        for *_, kind, item in steps:
            control, condition = item.control, item.condition
            if control == "branch" and condition:
                base, outcome = self.split_not(condition)
                if pending_branch is None:
                    pending_branch = st
                if base not in branch_conds:
                    branch_conds[base] = self.condition_task(obj, item, kind, base, pending_branch, path)
                out = self.step(obj, kind, item, State([{"task_key": branch_conds[base], "outcome": outcome}]), path)
                branch_exits += out.deps
                continue
            if pending_branch is not None:  # close the if/else group: continue when no branch failed
                st = State(branch_exits + [{"task_key": k} for k in branch_conds.values()], "NONE_FAILED")
                branch_conds, branch_exits, pending_branch = {}, [], None
            if control == "error_handler":
                if catch is None:
                    tried = [t["task_key"] for t in self.tasks[start:]]
                    catch = State([{"task_key": k} for k in tried] or st.deps, "AT_LEAST_ONE_FAILED")
                catch = self.step(obj, kind, item, catch, path,
                                  notes=["error handler: runs when any earlier task of this workflow fails "
                                         "(approximation of the try/catch scope); the job still ends as failed"])
                continue  # the main path continues from the try tasks
            catch = None
            st = self.step(obj, kind, item, st, path)
        if pending_branch is not None:
            st = State(branch_exits + [{"task_key": k} for k in branch_conds.values()], "NONE_FAILED")
        return st

    def step(self, obj: EtlObject, kind: str, item: Any, st: State, path: tuple[str, ...],
             notes: Optional[list[str]] = None) -> State:
        if kind == "call":
            return self.call(item, st, path, f"call:{obj.name}#{item.order}", notes)
        return State([{"task_key": self.script_task(obj, item, st, path, notes)}])

    def loop_settings(self, control: Optional[str], condition: Optional[str]) -> tuple[dict, list[str], list[str]]:
        """(task settings, todos, notes) for a step inside a loop."""
        if control != "loop":
            return {}, [], []
        m = RETRY_LOOP.search(condition or "")
        if m:
            retries = max(int(m.group(1)) - 1, 0)
            return ({"max_retries": retries, "min_retry_interval_millis": 60000}, [],
                    [f"retry loop '{condition}' -> max_retries {retries}"])
        return {}, [f"loop not converted, the task runs once: {condition}"], []

    def call(self, call: CallStep, st: State, path: tuple[str, ...], cite: str, notes: Optional[list[str]] = None) -> State:
        target = self.estate.objects.get(call.target)
        extra, todos, loop_notes = self.loop_settings(call.control, call.condition)
        notes = list(notes or []) + loop_notes
        if target is None:
            path_nb = f"notebooks/missing/{call.target}.py"
            self.result.files[path_nb] = (f"{HEADER}\n# {call.target} is called by the legacy job but is not in the export.\n"
                                          f'raise NotImplementedError("TODO(deltabound): {call.target} is missing from the export")\n')
            key = self.add(task_key(*path, call.target), "missing", cite, st, self.notebook(path_nb) | extra,
                           todos=todos + [f"{call.target} is not in the export"], notes=notes)
            return State([{"task_key": key}])
        if target.kind == ObjectKind.DATAFLOW:
            nb = f"notebooks/dataflows/{target.name}.py"
            key = self.add(task_key(*path, target.name), "dataflow", cite, st, self.notebook(nb) | extra,
                           variables=self.dataflow_vars.get(target.name, []), todos=todos, notes=notes)
            return State([{"task_key": key}])
        if extra or todos:
            self.result.tasks.append(TaskResult(task_key=task_key(*path, target.name), kind="workflow", source=cite,
                                                todos=todos, notes=notes + [f"loop on workflow {target.name}: "
                                                                             "retry settings are not applied to its tasks"]))
        return self.walk(target, st, path + (target.name,))

    @staticmethod
    def here(obj: EtlObject, path: tuple[str, ...]) -> tuple[str, ...]:
        """Task-key prefix for steps that belong to `obj` itself (path already ends with it below the job)."""
        return path or (obj.name,)

    def split_not(self, cond: str) -> tuple[str, str]:
        m = re.fullmatch(r"NOT \((.*)\)", cond.strip(), re.S)
        return (m.group(1), "false") if m else (cond, "true")

    def condition_task(self, obj: EtlObject, item: Any, kind: str, base: str, st: State, path: tuple[str, ...]) -> str:
        pos = item.order if kind == "call" else f"s{item.index + 1}"
        cite = f"call:{obj.name}#{item.order}" if kind == "call" else f"script:{obj.name}#{item.index + 1}"
        m = SIMPLE_CONDITION.match(base.strip())
        if m:
            var, op, lit, text = m.group(1), m.group(2), m.group(3), m.group(4)
            left = (f"{{{{tasks.{self.var_source[var]}.values.{var}}}}}" if var in self.var_source
                    else f"{{{{job.parameters.{var}}}}}")
            self.params.add(var)
            body = {"condition_task": {"op": CONDITION_OPS[op], "left": left, "right": text if text is not None else lit}}
            return self.add(task_key(*self.here(obj, path), "if", str(pos)), "condition", cite, st, body,
                            notes=[f"if {base}"])
        # Anything else: evaluate the translated condition in a small notebook, then branch on its result.
        tr = translate(base)
        key = task_key(*self.here(obj, path), "eval_if", str(pos))
        nb = f"notebooks/conditions/{key}.py"
        todos = [f"condition: {x}" for x in tr.todos]
        params = self.param_cell(sorted(tr.variables))
        code = (f"{HEADER}\n# Generated by deltabound: evaluates `{base}` ({cite})"
                + CELL + RUNTIME + CELL + params + CELL
                + (f'raise NotImplementedError("TODO(deltabound): condition not translated: {base}")' if todos else
                   f'_result = spark.sql("""SELECT coalesce({tr.sql}, false) AS r""").first()["r"]\n'
                   f'dbutils.jobs.taskValues.set("result", "true" if _result else "false")') + "\n")
        self.result.files[nb] = code
        eval_key = self.add(key, "condition", cite, st, self.notebook(nb), variables=sorted(tr.variables), todos=todos,
                            notes=[f"if {base} (evaluated in a notebook)"])
        body = {"condition_task": {"op": "EQUAL_TO", "left": f"{{{{tasks.{eval_key}.values.result}}}}", "right": "true"}}
        return self.add(task_key(*self.here(obj, path), "if", str(pos)), "condition", cite,
                        State([{"task_key": eval_key}]), body)

    def script_task(self, obj: EtlObject, sc: ScriptStep, st: State, path: tuple[str, ...],
                    notes: Optional[list[str]] = None) -> str:
        index, script = sc.index + 1, obj.scripts[sc.index]
        cite = f"script:{obj.name}#{index}"
        extra, todos, loop_notes = self.loop_settings(sc.control, sc.condition)
        notes = list(notes or []) + loop_notes
        if not obj.script_steps:
            notes.append(f"script position is not in the IR (< 0.2.1): it runs before the calls of {obj.name}")
        lines, used, sets = [], set(), []
        for stmt in split_statements(script):
            if m := ASSIGNMENT.match(stmt):
                var, expr = m.group(1), m.group(2)
                tr = translate(expr)
                if tr.todos:
                    todos += [f"${var}: {x}" for x in tr.todos]
                    lines.append(f"# TODO(deltabound): {stmt}")
                    continue
                used |= tr.variables
                typ = variable_type(self.estate, var)
                lines += [f'_v = spark.sql("""SELECT CAST({tr.sql} AS {typ}) AS v""").first()["v"]',
                          f'_set_var("{var}", "{typ}", None if _v is None else str(_v))',
                          f'dbutils.jobs.taskValues.set("{var}", "" if _v is None else str(_v))']
                sets.append(var)
            elif m := PRINT.match(stmt):
                lines.append(f"print({m.group(1).replace(chr(39) * 2, chr(39))!r})")
            elif m := RAISE.match(stmt):
                tr = translate(m.group(1))
                if tr.todos:
                    todos += [f"raise_exception: {x}" for x in tr.todos]
                    lines.append(f"# TODO(deltabound): {stmt}")
                    continue
                used |= tr.variables
                lines.append(f'raise RuntimeError(spark.sql("""SELECT CAST({tr.sql} AS STRING) AS m""").first()["m"])')
            else:
                todos.append(f"statement not converted: {stmt[:80]}")
                lines.append(f"# TODO(deltabound): {stmt}")
        if todos:
            lines.append(f'raise NotImplementedError("TODO(deltabound): {cite} has statements that need manual conversion")')
        key = task_key(*self.here(obj, path), "script", str(index))
        nb = f"notebooks/scripts/{key}.py"
        self.result.files[nb] = (f"{HEADER}\n# Generated by deltabound from {cite}. Original script:\n"
                                 + "\n".join(f"#   {l}" for l in script.splitlines()) + CELL + RUNTIME + CELL
                                 + self.param_cell(sorted(used)) + CELL + "\n".join(lines) + "\n")
        key = self.add(key, "script", cite, st, self.notebook(nb) | extra, variables=sorted(used), todos=todos,
                       notes=notes)
        for v in sets:
            self.var_source[v] = key
        self.params |= set(sets)
        return key

    def param_cell(self, variables: list[str]) -> str:
        if not variables:
            return "# Parameters: none."
        lines = [f'dbutils.widgets.text("{v}", {variable_default(self.estate, v)!r})' for v in variables]
        lines += [f'_set_var("{v}", "{variable_type(self.estate, v)}", dbutils.widgets.get("{v}"))' for v in variables]
        return "\n".join(lines)


# ---------- YAML (no dependency: every scalar is JSON-quoted, which is valid YAML) ----------

def to_yaml(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:\n{to_yaml(v, indent + 1)}")
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                body = to_yaml(item, indent + 1)
                lines.append(f"{pad}- {body.lstrip()}")
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return pad + _scalar(value)


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "null"
    if isinstance(v, (dict, list)):
        return "{}" if isinstance(v, dict) else "[]"
    return json.dumps(str(v))


# ---------- entry point ----------

def convert_job(estate: Estate, job: str, cfg: ConvertConfig) -> ConversionResult:
    root = estate.objects.get(job)
    if root is None or root.kind != ObjectKind.JOB:
        raise KeyError(f"No job named {job}")
    result = ConversionResult(job=job)
    ddl: dict[str, str] = {}
    builder = JobBuilder(estate, root, cfg, result)
    note = f"IR {estate.ir_version}, deltabound {__version__} (IR {IR_VERSION})."
    dataflows = [n for n in graph.reached_in_order(estate, job) if estate.objects[n].kind == ObjectKind.DATAFLOW]
    for d in dataflows:
        nb, res, statements = convert_dataflow(estate, d, cfg, header_note=note)
        result.files[res.notebook] = nb
        result.dataflows.append(res)
        builder.dataflow_vars[d] = res.variables
        for table, statement in statements:
            if table not in ddl or ddl[table].startswith("--"):  # prefer a full definition
                ddl[table] = statement

    schemas = sorted({t.rsplit(".", 1)[0] for t in ddl if "." in t})
    setup = [f"{HEADER}\n# Generated by deltabound: create target schemas and tables (idempotent). Review types first."]
    setup += [f'spark.sql("CREATE SCHEMA IF NOT EXISTS {s}")' for s in schemas]
    setup += [f'spark.sql("""\n{s}\n""")' if not s.startswith("--") else f"# {s[3:]}" for s in ddl.values()]
    result.files["notebooks/00_setup_tables.py"] = CELL.join(setup[:1] + ["\n".join(setup[1:]) or "# nothing to create"]) + "\n"
    setup_key = builder.add("setup_tables", "setup", f"job:{job}", State(), builder.notebook("notebooks/00_setup_tables.py"))

    builder.walk(root, State([{"task_key": setup_key}]), ())

    params = sorted(builder.params | {v.name.lstrip("$") for v in root.variables})
    name = f"{cfg.bundle_prefix}_{job.lower()}"
    job_def: dict[str, Any] = {"name": job, "description": root.description or f"Converted from {job} by deltabound",
                               "parameters": [{"name": p, "default": variable_default(estate, p)} for p in params],
                               "tasks": builder.tasks}
    if cfg.compute == "job_cluster":
        job_def["job_clusters"] = [{"job_cluster_key": "main", "new_cluster": {
            "spark_version": "${var.spark_version}", "node_type_id": "${var.node_type_id}", "num_workers": "${var.num_workers}"}}]
    resource = {"resources": {"jobs": {job.lower(): job_def}}}
    bundle: dict[str, Any] = {"bundle": {"name": name}, "include": ["resources/*.yml"]}
    if cfg.compute == "job_cluster":
        bundle["variables"] = {"spark_version": {"default": "17.3.x-scala2.13"},
                               "node_type_id": {"description": "cloud-specific node type"},
                               "num_workers": {"default": 2}}
    bundle["targets"] = {"dev": {"mode": "development", "default": True}, "prod": {"mode": "production"}}
    banner = f"# Generated by deltabound {__version__} from legacy job {job}. Review conversion_report.md first.\n"
    result.files["databricks.yml"] = banner + to_yaml(bundle) + "\n"
    result.files[f"resources/{job.lower()}.job.yml"] = banner + to_yaml(resource) + "\n"
    from .report import render_report

    md, js = render_report(result)
    result.files["conversion_report.md"] = md
    result.files["conversion_report.json"] = js
    return result

"""Collect the IR context the LLM sees, with a citation id on every element.

Citation ids:
  df:<DATAFLOW>/<TRANSFORM>            a transform
  df:<DATAFLOW>/<TRANSFORM>.<COLUMN>   one column mapping
  call:<OBJECT>#<order>                a call step inside a job or workflow
  script:<OBJECT>#<n>                  the n-th script of an object (1-based)
  var:<OBJECT>/<NAME>                  a variable or parameter
  table:<QUALIFIED NAME> / file:<NAME> a table or file the job reads or writes (IR fact)
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Iterator

from .. import graph
from ..ir import Estate, EtlObject, ObjectKind, Transform

STRING_LITERAL = re.compile(r"'[^']*'")
NUMBER_LITERAL = re.compile(r"(?<![\w.$])\d+(?:\.\d+)?\b")

SOURCE_KINDS = {"table_source", "file_source"}
TARGET_KINDS = {"table_target", "file_target"}


def redact(text: str) -> str:
    """Replace string and number literals with placeholders."""
    return NUMBER_LITERAL.sub("<num>", STRING_LITERAL.sub("'<str>'", text))


def _clean(d: dict) -> dict:
    """Drop empty values so the prompt stays small."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def transform_id(df: str, tr: str) -> str:
    return f"df:{df}/{tr}"


def _table_name(t: Transform) -> str:
    if t.table is not None:
        return t.table.qualified
    return t.options.get("fileName") or t.name


def _fact_id(t: Transform) -> str:
    return f"table:{t.table.qualified}" if t.table is not None else f"file:{_table_name(t)}"


def dataflow_context(estate: Estate, dataflow: str, redact_literals: bool = False) -> dict:
    df = estate.objects[dataflow]
    r: Callable[[str], str] = redact if redact_literals else (lambda s: s)
    transforms = []
    for t in df.transforms:
        tid = transform_id(dataflow, t.name)
        transforms.append(_clean({
            "id": tid,
            "kind": t.kind if t.kind != "opaque" else f"opaque (unmodeled {t.raw_tag})",
            "inputs": t.inputs,
            "table": _table_name(t) if t.kind in SOURCE_KINDS | TARGET_KINDS or t.table else None,
            "where": r(t.where) if t.where else None,
            "joins": [_clean({"left": j.left, "right": j.right, "type": j.join_type,
                              "condition": r(j.condition) if j.condition else None}) for j in t.joins],
            "group_by": [r(g) for g in t.group_by],
            "order_by": [f"{r(s.expression)} {s.direction}" for s in t.order_by],
            "branches": [_clean({"name": b.name, "condition": r(b.condition) if b.condition else None,
                                 "default": b.is_default or None}) for b in t.branches],
            "lookups": [_clean({
                "function": lk.function,
                "table": lk.table.qualified if lk.table else "(unresolved at parse time)",
                "returns": lk.return_columns,
                "match": [f"{c} = {r(e)}" for c, e in lk.match_columns],
                "for_column": lk.output_column,
            }) for lk in t.lookups],
            "options": {k: r(v) for k, v in t.options.items()},
            "columns": [_clean({"id": f"{tid}.{c.column}", "column": c.column,
                                "expression": r(c.expression) if c.expression else None}) for c in t.columns],
        }))
    return _clean({"dataflow": dataflow, "description": df.description, "transforms": transforms})


def _walk(estate: Estate, obj: EtlObject, seen: frozenset[str]) -> list[dict]:
    nodes = []
    for c in sorted(obj.calls, key=lambda c: c.order):
        target = estate.objects.get(c.target)
        node = _clean({
            "id": f"call:{obj.name}#{c.order}",
            "target": c.target,
            "kind": target.kind.value if target else "missing from export",
            "condition": c.condition,
            "control": c.control,
        })
        if target is not None and target.name not in seen:
            node["calls"] = _walk(estate, target, seen | {target.name})
            if not node["calls"]:
                del node["calls"]
        nodes.append(node)
    return nodes


def reached_objects(estate: Estate, job: str) -> list[EtlObject]:
    """Job and everything it calls, in first-call (depth-first) order."""
    return [estate.objects[n] for n in graph.reached_in_order(estate, job)]


def job_facts(estate: Estate, job: str) -> list[dict]:
    """Tables and files the job reads and writes, straight from the IR (no LLM)."""
    reads: dict[str, dict] = {}
    writes: dict[str, dict] = {}
    for obj in reached_objects(estate, job):
        if obj.kind != ObjectKind.DATAFLOW:
            continue
        for t in obj.transforms:
            tid = transform_id(obj.name, t.name)
            if t.kind in SOURCE_KINDS:
                reads.setdefault(_fact_id(t), {"name": _table_name(t), "by": []})["by"].append(tid)
            elif t.kind in TARGET_KINDS:
                writes.setdefault(_fact_id(t), {"name": _table_name(t), "by": []})["by"].append(tid)
            for lk in t.lookups:
                if lk.table is not None:
                    reads.setdefault(f"table:{lk.table.qualified}",
                                     {"name": lk.table.qualified, "by": []})["by"].append(tid)
    facts = []
    for fid in sorted(set(reads) | set(writes)):
        role = ("intermediate" if fid in reads else "output") if fid in writes else "input"
        info = writes.get(fid) or reads[fid]
        facts.append(_clean({
            "id": fid,
            "name": info["name"],
            "role": role,
            "read_by": sorted(set(reads.get(fid, {}).get("by", []))),
            "written_by": sorted(set(writes.get(fid, {}).get("by", []))),
        }))
    return facts


def job_context(estate: Estate, job: str, redact_literals: bool = False) -> dict:
    """Call tree, variables, scripts and IR facts for one job (dataflow details go in their own context)."""
    if job not in estate.objects or estate.objects[job].kind != ObjectKind.JOB:
        raise KeyError(f"No job named {job}")
    r: Callable[[str], str] = redact if redact_literals else (lambda s: s)
    objs = reached_objects(estate, job)
    root = estate.objects[job]
    return _clean({
        "job": job,
        "description": root.description,
        "call_tree": _walk(estate, root, frozenset({job})),
        "variables": [_clean({"id": f"var:{o.name}/{v.name}", "scope": v.scope, "type": v.data_type,
                              "default": r(v.default) if v.default else None})
                      for o in objs for v in o.variables],
        "scripts": [{"id": f"script:{o.name}#{i}", "text": r(s)}
                    for o in objs for i, s in enumerate(o.scripts, 1)],
        "facts": job_facts(estate, job),
    })


def job_dataflows(estate: Estate, job: str) -> list[str]:
    return [o.name for o in reached_objects(estate, job) if o.kind == ObjectKind.DATAFLOW]


def _ids(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        if isinstance(value.get("id"), str):
            yield value["id"]
        for v in value.values():
            yield from _ids(v)
    elif isinstance(value, list):
        for v in value:
            yield from _ids(v)


def citation_ids(*contexts: dict) -> set[str]:
    return {i for ctx in contexts for i in _ids(ctx)}


def context_hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

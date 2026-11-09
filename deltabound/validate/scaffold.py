"""Draft a validation.toml for one job from the IR: steps, keys, ignored columns, filters.

Every guess is written as a comment next to the value, so a reviewer can see why.
"""
from __future__ import annotations

from typing import Optional

from .. import graph
from ..convert.config import ConvertConfig
from ..ir import Estate, ObjectKind, Transform
from .manifest import toml_value


def _chain(df_transforms: list[Transform], target: Transform) -> tuple[Optional[Transform], Optional[Transform], Optional[Transform]]:
    """(table comparison, history preserving, key generation) feeding a target, if any."""
    by = {t.name: t for t in df_transforms if t.kind != "table_target"}
    tc = hp = kg = None
    cur = by.get(target.inputs[0]) if len(target.inputs) == 1 else None
    if cur is not None and cur.kind == "key_generation":
        kg, cur = cur, by.get(cur.inputs[0]) if cur.inputs else None
    if cur is not None and cur.kind == "history_preserving":
        hp, cur = cur, by.get(cur.inputs[0]) if cur.inputs else None
    if cur is not None and cur.kind == "table_comparison":
        tc = cur
    return tc, hp, kg


def generated_keys(estate: Estate) -> dict[str, str]:
    """Qualified table -> surrogate key column filled by a key-generation transform."""
    out = {}
    for o in estate.by_kind(ObjectKind.DATAFLOW):
        for t in o.transforms:
            if t.kind == "key_generation" and t.generated_key:
                table = t.table.qualified if t.table else None
                if table:
                    out[table.lower()] = t.generated_key
    return out


def scaffold(estate: Estate, job: str, cfg: Optional[ConvertConfig] = None, business_date: str = "YYYY-MM-DD") -> str:
    cfg = cfg or ConvertConfig()
    if job not in estate.objects or estate.objects[job].kind != ObjectKind.JOB:
        raise KeyError(f"No job named {job}")
    surrogate = generated_keys(estate)
    lines = [
        f"# Validation manifest for {job}, drafted by `deltabound validate scaffold`.",
        "# Review every step: keys, ignored columns and filters are guesses from the IR (see comments).",
        "# Paths are relative to this file. --engine duckdb reads files; --engine spark also reads tables.",
        f"job = {toml_value(job)}",
        f"business_date = {toml_value(business_date)}",
        'estate = []  # legacy export files/folders, e.g. ["../samples/bods"]',
        "",
        "[defaults]  # decided 2026-11-09: exact integers, decimals within 0.005",
        "numeric_abs = 0.005", "numeric_rel = 0.0", "timestamp_seconds = 0", "trim = true",
        "case_sensitive = true", "empty_is_null = true  # BODS treats '' as NULL",
        "ignore_columns = []", "max_sample_rows = 20", "mask_columns = []", "row_count_abs = 0", "row_count_pct = 0.0",
    ]
    for name in graph.reached_in_order(estate, job):
        df = estate.objects[name]
        if df.kind != ObjectKind.DATAFLOW:
            continue
        for t in df.transforms:
            if t.kind != "table_target" or t.table is None:
                continue
            qualified = t.table.qualified
            table = t.table.table.lower()
            tc, hp, kg = _chain(df.transforms, t)
            keys, ignore, comments, flt = [], [], [], None
            if tc is not None and tc.key_columns:
                keys = list(tc.key_columns)
                comments.append(f"keys: business key of {tc.name}")
            else:
                comments.append("keys: none known; leave empty for a keyless comparison, or set the business key")
            if kg is not None and kg.generated_key:
                ignore.append(kg.generated_key)
                comments.append(f"ignored {kg.generated_key}: surrogate key from {kg.name} (identity column on Databricks)")
            if hp is not None:
                h, d = hp.history, cfg.scd
                vf, vt = (h and h.valid_from) or d.valid_from, (h and h.valid_to) or d.valid_to
                flag, cur = (h and h.current_flag) or d.current_flag, (h and h.current_value) or d.current_value
                ignore += [vf, vt]
                flt = f"{flag} = '{cur}'"
                comments.append(f"SCD2 ({hp.name}): compare current rows only; {vf}/{vt} depend on the load date")
            for col, sources in _lookup_sources(estate, name, t):
                for src in sources:
                    table_part, _, src_col = src.rpartition(".")
                    if surrogate.get(table_part.lower()) == src_col and col not in ignore:
                        ignore.append(col)
                        comments.append(f"ignored {col}: surrogate key looked up from {table_part}")
            mapped, _ = cfg.table(t.table)
            lines += ["", "[[steps]]"]
            lines += [f"# {c}" for c in comments]
            lines += [f"dataflow = {toml_value(name)}", f"target = {toml_value(qualified)}",
                      f"keys = {toml_value(keys)}", f"ignore_columns = {toml_value(ignore)}",
                      f"legacy = {toml_value({'path': f'legacy/{table}.csv', 'filter': flt})}",
                      f"databricks = {toml_value({'table': mapped, 'filter': flt})}",
                      f"# for --engine duckdb: databricks = {toml_value({'path': f'databricks/{table}.parquet', 'filter': flt})}"]
    return "\n".join(lines) + "\n"


def _lookup_sources(estate: Estate, dataflow: str, target: Transform) -> list[tuple[str, list[str]]]:
    """(target column, upstream column nodes) for target columns, via column lineage."""
    if target.table is None:
        return []
    import networkx as nx

    g = graph.column_lineage(estate, dataflow)
    prefix = target.table.qualified + "."
    return [(n[len(prefix):], sorted(nx.ancestors(g, n))) for n in g.nodes if n.startswith(prefix)]

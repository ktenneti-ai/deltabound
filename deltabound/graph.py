"""Analysis over an Estate: call tree, dead code, lineage, complexity, waves."""
from __future__ import annotations

import networkx as nx

from .ir import Estate, ObjectKind


def call_graph(estate: Estate) -> nx.DiGraph:
    g = nx.DiGraph()
    for obj in estate.objects.values():
        g.add_node(obj.name, kind=obj.kind.value)
    for obj in estate.objects.values():
        for c in obj.calls:
            if c.target not in g:
                g.add_node(c.target, kind="missing")
            g.add_edge(obj.name, c.target, condition=c.condition, order=c.order)
    return g


def tree_lines(estate: Estate, root: str) -> list[str]:
    """Indented text view of everything a job runs, with conditions."""
    lines: list[str] = []

    def walk(name: str, depth: int, cond: str | None, seen: frozenset[str]) -> None:
        obj = estate.objects.get(name)
        kind = obj.kind.value if obj else "MISSING"
        suffix = f"   [if {cond}]" if cond else ""
        lines.append(f"{'  ' * depth}{kind}: {name}{suffix}")
        if obj is None or name in seen:
            return
        for c in sorted(obj.calls, key=lambda c: c.order):
            walk(c.target, depth + 1, c.condition, seen | {name})

    walk(root, 0, None, frozenset())
    return lines


def unreachable(estate: Estate) -> list[str]:
    """Workflows and dataflows no job ever reaches: candidates to skip migrating."""
    g = call_graph(estate)
    reached: set[str] = set()
    for job in estate.by_kind(ObjectKind.JOB):
        reached |= {job.name} | nx.descendants(g, job.name)
    return sorted(o.name for o in estate.objects.values() if o.name not in reached)


def missing_references(estate: Estate) -> list[str]:
    g = call_graph(estate)
    return sorted(n for n, d in g.nodes(data=True) if d["kind"] == "missing")


def column_lineage(estate: Estate, dataflow: str) -> nx.DiGraph:
    """Column-level lineage inside one dataflow: 'transform.column' nodes."""
    df = estate.objects[dataflow]
    g = nx.DiGraph()
    for tr in df.transforms:
        for col in tr.columns:
            node = f"{tr.name}.{col.column}"
            g.add_node(node, expression=col.expression)
            for src in col.source_columns:
                g.add_edge(src, node)
        if tr.kind in ("table_target", "file_target") and len(tr.inputs) == 1:
            upstream = next((t for t in df.transforms if t.name == tr.inputs[0]), None)
            target = tr.table.qualified if tr.table else tr.name
            for col in upstream.columns if upstream else []:
                g.add_edge(f"{upstream.name}.{col.column}", f"{target}.{col.column}")
    return g


def trace_column(estate: Estate, dataflow: str, column_node: str) -> list[str]:
    g = column_lineage(estate, dataflow)
    if column_node not in g:
        return []
    return sorted(nx.ancestors(g, column_node))


COMPLEXITY_WEIGHTS = {
    "query": 1, "lookup": 2, "case": 2, "merge": 2, "validation": 2,
    "table_comparison": 3, "history_preserving": 4, "key_generation": 1,
    "map_operation": 2, "opaque": 5,
}


def complexity(estate: Estate, name: str) -> int:
    """Rough migration-effort score for a job: transforms, conditions, scripts, nesting."""
    g = call_graph(estate)
    nodes = {name} | (nx.descendants(g, name) if name in g else set())
    score = 0
    for n in nodes:
        obj = estate.objects.get(n)
        if obj is None:
            score += 5
            continue
        score += sum(COMPLEXITY_WEIGHTS.get(t.kind, 0) for t in obj.transforms)
        score += 3 * len(obj.scripts) + 2 * sum(1 for c in obj.calls if c.condition)
    depth = max((len(p) for p in nx.all_simple_paths(g, name, list(nodes - {name}))), default=1) if len(nodes) > 1 else 1
    return score + 2 * depth


def migration_waves(estate: Estate, size: int = 10) -> list[list[tuple[str, int]]]:
    """Jobs sorted easiest-first and grouped into waves."""
    scored = sorted(((j.name, complexity(estate, j.name)) for j in estate.by_kind(ObjectKind.JOB)), key=lambda x: x[1])
    return [scored[i : i + size] for i in range(0, len(scored), size)]

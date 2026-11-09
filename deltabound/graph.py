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


def reached_in_order(estate: Estate, root: str) -> list[str]:
    """`root` and every object it calls, in first-call (depth-first) order; missing objects skipped."""
    order: list[str] = []

    def visit(name: str) -> None:
        if name in order or name not in estate.objects:
            return
        order.append(name)
        for c in sorted(estate.objects[name].calls, key=lambda c: c.order):
            visit(c.target)

    visit(root)
    return order


def call_tree(estate: Estate, root: str) -> dict:
    """Nested call tree of `root`: {name, kind, condition, control, order, calls: [...]}.

    kind is "missing" for objects not in the export. A call that closes a cycle
    is marked cycle=True and not expanded.
    """

    def node(name: str, cond: str | None, control: str | None, order: int, seen: frozenset[str]) -> dict:
        obj = estate.objects.get(name)
        out: dict = {"name": name, "kind": obj.kind.value if obj else "missing",
                     "condition": cond, "control": control, "order": order, "calls": []}
        if obj is None:
            return out
        if name in seen:
            out["cycle"] = True
            return out
        out["calls"] = [node(c.target, c.condition, c.control, c.order, seen | {name})
                        for c in sorted(obj.calls, key=lambda c: c.order)]
        return out

    return node(root, None, None, 0, frozenset())


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


# Transforms that pass their input columns through unchanged when they do not
# declare columns of their own (row routing, comparison, key/flag handling).
PASS_THROUGH_KINDS = {
    "case", "merge", "validation", "table_comparison", "history_preserving",
    "key_generation", "map_operation", "lookup",
}
TARGET_KINDS = {"table_target", "file_target"}


def column_lineage(estate: Estate, dataflow: str) -> nx.DiGraph:
    """Column-level lineage inside one dataflow: 'transform.column' nodes.

    Targets are named by their qualified table. Lineage passes through
    PASS_THROUGH_KINDS transforms and Case branch outputs; it stops at opaque
    transforms, whose behaviour is unknown.
    """
    df = estate.objects[dataflow]
    by_name = {t.name: t for t in df.transforms}
    branch_owner = {b.name: t.name for t in df.transforms for b in t.branches}

    def out_cols(output: str, seen: frozenset[str]) -> list[str]:
        """Columns available at a transform output or a Case branch."""
        tr = by_name.get(branch_owner.get(output, output))
        if tr is None or tr.name in seen:
            return []
        if tr.columns:
            return [c.column for c in tr.columns]
        if tr.kind not in PASS_THROUGH_KINDS:
            return []
        cols = [c for i in tr.inputs for c in out_cols(i, seen | {tr.name})]
        cols += [r for lk in tr.lookups if lk.output_column is None for r in lk.return_columns]
        return list(dict.fromkeys(cols))

    g = nx.DiGraph()
    for tr in df.transforms:
        for col in tr.columns:
            node = f"{tr.name}.{col.column}"
            g.add_node(node, expression=col.expression)
            for src in col.source_columns:
                g.add_edge(src, node)
        if tr.columns or tr.kind not in PASS_THROUGH_KINDS | TARGET_KINDS:
            continue
        out = (tr.table.qualified if tr.table else tr.name) if tr.kind in TARGET_KINDS else tr.name
        for i in tr.inputs:
            for c in out_cols(i, frozenset()):
                g.add_edge(f"{i}.{c}", f"{out}.{c}")
        for lk in tr.lookups:
            if lk.output_column is None and lk.table is not None:
                for r in lk.return_columns:
                    g.add_edge(f"{lk.table.qualified}.{r}", f"{out}.{r}")
        for b in tr.branches:
            for c in out_cols(tr.name, frozenset()):
                g.add_edge(f"{tr.name}.{c}", f"{b.name}.{c}")
    return g


def output_columns(estate: Estate, dataflow: str, output: str,
                   _seen: frozenset[str] = frozenset()) -> list[tuple[str, str | None]] | None:
    """(column, legacy data type) available at a transform output or Case branch.

    Follows transforms that pass columns through unchanged; None when unknown
    (opaque transforms, sources without declared columns, cycles).
    """
    df = estate.objects[dataflow]
    owner = {b.name: t.name for t in df.transforms for b in t.branches}.get(output, output)
    t = next((x for x in df.transforms if x.name == owner and x.kind not in TARGET_KINDS), None)
    if t is None or t.name in _seen:
        return None
    if t.columns:
        return [(c.column, c.data_type) for c in t.columns]
    if t.kind in PASS_THROUGH_KINDS - {"lookup"} and t.inputs:
        return output_columns(estate, dataflow, t.inputs[0], _seen | {t.name})
    return None


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


def call_depth(estate: Estate, name: str, g: nx.DiGraph | None = None) -> int:
    """Objects on the longest call path starting at `name` (1 = calls nothing).

    Memoized depth-first search: linear in the size of the call graph. A call
    cycle (should not happen in BODS) is cut where it closes.
    """
    g = call_graph(estate) if g is None else g
    if name not in g:
        return 1
    memo: dict[str, int] = {}

    def depth(n: str, stack: frozenset[str]) -> int:
        if n in memo:
            return memo[n]
        d = 1 + max((depth(s, stack | {n}) for s in g.successors(n) if s not in stack | {n}), default=0)
        memo[n] = d
        return d

    return depth(name, frozenset())


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
    return score + 2 * call_depth(estate, name, g)


def migration_waves(estate: Estate, size: int = 10) -> list[list[tuple[str, int]]]:
    """Jobs sorted easiest-first and grouped into waves."""
    scored = sorted(((j.name, complexity(estate, j.name)) for j in estate.by_kind(ObjectKind.JOB)), key=lambda x: x[1])
    return [scored[i : i + size] for i in range(0, len(scored), size)]

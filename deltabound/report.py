"""Markdown estate report: counts, coverage, dead code, complexity, waves.

Reads only the IR and graph analysis, never a parser.
"""
from __future__ import annotations

from collections import Counter

import networkx as nx

from . import graph
from .ir import IR_VERSION, Estate, ObjectKind


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def job_stats(estate: Estate, g: nx.DiGraph | None = None) -> list[dict]:
    """Per job: complexity score, dataflows reached and call depth, hardest first."""
    g = graph.call_graph(estate) if g is None else g
    stats = []
    for job in estate.by_kind(ObjectKind.JOB):
        reached = nx.descendants(g, job.name)
        stats.append({
            "job": job.name,
            "score": graph.complexity(estate, job.name),
            "dataflows": sum(1 for n in reached if g.nodes[n]["kind"] == ObjectKind.DATAFLOW.value),
            "depth": graph.call_depth(estate, job.name, g),
        })
    return sorted(stats, key=lambda s: (-s["score"], s["job"]))


def render_markdown(estate: Estate, top: int = 5, wave_size: int = 10) -> str:
    g = graph.call_graph(estate)
    transforms = [t for o in estate.objects.values() for t in o.transforms]
    opaque = [t for t in transforms if t.kind == "opaque"]
    coverage = 100 * (1 - len(opaque) / len(transforms)) if transforms else 100.0
    out: list[str] = [
        "# DeltaBound estate report",
        "",
        f"Source: **{estate.source_tool}** {estate.source_version or ''}  ",
        f"IR version: {estate.ir_version} (written by deltabound IR {IR_VERSION})",
        "",
        "## Counts",
        "",
    ]
    out += _table(["Object", "Count"], [
        *[[f"{k.value.capitalize()}s", len(estate.by_kind(k))] for k in ObjectKind],
        ["Transforms", len(transforms)],
        ["Scripts", sum(len(o.scripts) for o in estate.objects.values())],
        ["Conditional calls", sum(1 for o in estate.objects.values() for c in o.calls if c.condition)],
    ])

    out += ["", "## Parser coverage", "",
            f"**{coverage:.0f}%** of transforms modeled ({len(transforms) - len(opaque)} of {len(transforms)}; "
            f"{len(opaque)} opaque).", ""]
    if opaque:
        out += _table(["Unmodeled transform type", "Count"],
                      [[f"`{tag}`", n] for tag, n in sorted(Counter(t.raw_tag for t in opaque).items())])
        out.append("")
    if estate.unparsed:
        out += [f"Items the parser could not fully model ({len(estate.unparsed)}):", ""]
        out += [f"- `{u}`" for u in estate.unparsed]
    else:
        out.append("Nothing was left unparsed.")

    dead = graph.unreachable(estate)
    missing = graph.missing_references(estate)
    out += ["", "## Dead code", "", "Objects no job ever reaches. Candidates to skip migrating.", ""]
    out += _table(["Object", "Kind"], [[n, estate.objects[n].kind.value] for n in dead]) if dead else ["(none)"]
    out += ["", "Objects called but not in the export. Export them or confirm they are obsolete.", ""]
    out += _table(["Missing object", "Called by"],
                  [[n, ", ".join(sorted(g.predecessors(n)))] for n in missing]) if missing else ["(none)"]

    stats = job_stats(estate, g)
    out += ["", f"## Most complex jobs (top {min(top, len(stats))})", ""]
    out += _table(["Rank", "Job", "Score", "Dataflows", "Call depth"],
                  [[i, s["job"], s["score"], s["dataflows"], s["depth"]] for i, s in enumerate(stats[:top], 1)])

    out += ["", "## Migration waves", "", f"Jobs easiest first, {wave_size} per wave.", ""]
    for i, wave in enumerate(graph.migration_waves(estate, wave_size), 1):
        out += [f"### Wave {i}", ""]
        out += _table(["Job", "Score"], [[name, score] for name, score in wave])
        out.append("")
    return "\n".join(out).rstrip() + "\n"

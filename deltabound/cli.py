"""Command line: deltabound <command> <export files...>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import graph
from .ir import ObjectKind
from .parsers import bods


def _load(paths: list[str]):
    files: list[Path] = []
    for p in map(Path, paths):
        files += sorted(p.rglob("*.xml")) if p.is_dir() else [p]
    return bods.parse_files(files)


def cmd_summary(a) -> None:
    e = _load(a.paths)
    transforms = [t for o in e.objects.values() for t in o.transforms]
    opaque = sum(1 for t in transforms if t.kind == "opaque")
    print(f"Source: {e.source_tool} {e.source_version or ''}")
    for k in ObjectKind:
        print(f"  {k.value + 's':<11}{len(e.by_kind(k))}")
    print(f"  transforms {len(transforms)}  ({opaque} opaque)")
    modeled = 100 * (1 - opaque / len(transforms)) if transforms else 100
    print(f"Coverage: {modeled:.0f}% of transforms modeled")
    for issue in e.unparsed:
        print(f"  ! {issue}")


def cmd_tree(a) -> None:
    e = _load(a.paths)
    jobs = [a.job] if a.job else [j.name for j in e.by_kind(ObjectKind.JOB)]
    for j in jobs:
        print("\n".join(graph.tree_lines(e, j)), end="\n\n")


def cmd_deadcode(a) -> None:
    e = _load(a.paths)
    print("Never called by any job:", *graph.unreachable(e) or ["(none)"], sep="\n  ")
    print("Called but not in export:", *graph.missing_references(e) or ["(none)"], sep="\n  ")


def cmd_lineage(a) -> None:
    e = _load(a.paths)
    print(f"{a.column} <- ", *graph.trace_column(e, a.dataflow, a.column) or ["(no upstream found)"], sep="\n  ")


def cmd_waves(a) -> None:
    e = _load(a.paths)
    for i, wave in enumerate(graph.migration_waves(e, a.size), 1):
        print(f"Wave {i}:")
        for name, score in wave:
            print(f"  {score:>4}  {name}")


def cmd_export(a) -> None:
    e = _load(a.paths)
    Path(a.out).write_text(e.model_dump_json(indent=2))
    print(f"Wrote {a.out}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="deltabound", description="Understand legacy ETL estates.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("paths", nargs="+", help="export files or folders")
        s.set_defaults(fn=fn)
        return s

    add("summary", cmd_summary, "counts and parser coverage")
    add("tree", cmd_tree, "nested call tree per job").add_argument("--job")
    add("deadcode", cmd_deadcode, "unreachable and missing objects")
    s = add("lineage", cmd_lineage, "trace a column to its sources")
    s.add_argument("--dataflow", required=True)
    s.add_argument("--column", required=True, help="e.g. DS_DWH.STG.STG_ORDERS.NET_AMOUNT")
    add("waves", cmd_waves, "migration waves, easiest first").add_argument("--size", type=int, default=10)
    add("export", cmd_export, "write the IR as JSON").add_argument("--out", default="estate.json")

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

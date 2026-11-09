"""Command line: deltabound <command> <export files...>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import graph, report
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


def cmd_report(a) -> None:
    e = _load(a.paths)
    Path(a.out).write_text(report.render_markdown(e, top=a.top, wave_size=a.wave_size))
    print(f"Wrote {a.out}")


def cmd_convert(a) -> None:
    from .convert import ConvertError, convert_job, load_convert_config

    e = _load(a.paths)
    jobs = sorted(j.name for j in e.by_kind(ObjectKind.JOB)) if a.all else [a.job]
    unknown = [j for j in jobs if j not in e.objects or e.objects[j].kind != ObjectKind.JOB]
    if unknown:
        sys.exit(f"Not a job in this export: {', '.join(unknown)}")
    try:
        cfg = load_convert_config(Path(a.config) if a.config else None)
    except ConvertError as err:
        sys.exit(f"convert: {err}")
    out = Path(a.out)
    for j in jobs:
        r = convert_job(e, j, cfg)
        base = out / j if a.all else out
        for rel, text in r.files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        transforms = [t for d in r.dataflows for t in d.transforms]
        done = sum(1 for t in transforms if t.status == "converted")
        print(f"{j}: {done}/{len(transforms)} transforms converted, {r.todo_count} blocking TODO(s) -> {base}/conversion_report.md")


def schema_json() -> str:
    """JSON Schema of the IR as serialised (model_dump always writes every field, defaults included)."""
    from .ir import Estate

    schema = Estate.model_json_schema(mode="serialization")
    for obj in [schema, *schema.get("$defs", {}).values()]:
        if "properties" in obj:
            obj["required"] = sorted(obj["properties"])
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def cmd_schema(a) -> None:
    Path(a.out).write_text(schema_json())
    print(f"Wrote {a.out}")


def cmd_explain(a) -> None:
    from .explain import Explainer, ExplainError, explainable_jobs, load_config
    from .explain.render import render_text

    e = _load(a.paths)
    jobs = explainable_jobs(e) if a.all else [a.job]
    unknown = [j for j in jobs if j not in e.objects or e.objects[j].kind != ObjectKind.JOB]
    if unknown:
        sys.exit(f"Not a job in this export: {', '.join(unknown)}")
    try:
        cfg = load_config(Path(a.config) if a.config else None)
        ex = Explainer(e, cfg, dry_run=a.dry_run)
        if a.dry_run:
            for j in jobs:
                ex.job(j, a.refresh)
            print(f"Dry run: prompts written to {cfg.cache_dir}/prompts/ (nothing was sent)")
            return
        bundle = ex.bundle(jobs, a.refresh)
    except ExplainError as err:
        sys.exit(f"explain: {err}")
    if a.bundle:
        Path(a.bundle).write_text(bundle.model_dump_json(indent=2))
    if not a.all:
        print(render_text(bundle.jobs[a.job]))
    s = ex.stats
    print(f"{len(bundle.jobs)} job(s) explained: {s['llm_calls']} model call(s), {s['cache_hits']} cache hit(s), "
          f"{s['retries']} citation retry(ies).{' Wrote ' + a.bundle if a.bundle else ''}")


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
    s = add("report", cmd_report, "write a markdown estate report")
    s.add_argument("--out", default="estate_report.md")
    s.add_argument("--top", type=int, default=5, help="number of most complex jobs to list")
    s.add_argument("--wave-size", type=int, default=10)

    s = add("explain", cmd_explain, "plain-English job summaries with citations (calls an LLM)")
    which = s.add_mutually_exclusive_group(required=True)
    which.add_argument("--job")
    which.add_argument("--all", action="store_true", help="explain every job")
    s.add_argument("--refresh", action="store_true", help="ignore the cache")
    s.add_argument("--dry-run", action="store_true", help="write the prompts to disk, send nothing")
    s.add_argument("--bundle", help="write all explanations to one JSON file (for the viewer)")
    s.add_argument("--config", help="settings file (default: ./deltabound.toml if present)")

    s = add("convert", cmd_convert, "convert a job to Databricks notebooks + Asset Bundle")
    which = s.add_mutually_exclusive_group(required=True)
    which.add_argument("--job")
    which.add_argument("--all", action="store_true", help="convert every job (one bundle per job)")
    s.add_argument("--out", required=True, help="output folder")
    s.add_argument("--config", help="settings file (default: ./deltabound.toml if present)")

    sc = sub.add_parser("schema", help="write the IR JSON Schema (used by the viewer's TypeScript types)")
    sc.add_argument("--out", default="ir.schema.json")
    sc.set_defaults(fn=cmd_schema)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

"""Command line: deltabound <command> <source folder | export files...>"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from . import graph, report
from .ir import ObjectKind
from .parsers import bods


def _project_manifest(paths: list[str]) -> Path | None:
    """deltabound.toml when the only path given is a source folder (or its manifest)."""
    from .project import find_manifest

    return find_manifest(Path(paths[0])) if len(paths) == 1 else None


def _load(paths: list[str]):
    from .project import ProjectError, load_project

    if _project_manifest(paths) is not None:
        try:
            return load_project(Path(paths[0])).load_estate()
        except ProjectError as e:
            sys.exit(f"deltabound: {e}")
    files: list[Path] = []
    for p in map(Path, paths):
        files += sorted([*p.rglob("*.xml"), *p.rglob("*.dtsx")]) if p.is_dir() else [p]
    ssis_files = [f for f in files if f.suffix.lower() == ".dtsx"]
    if ssis_files and len(ssis_files) != len(files):
        sys.exit("deltabound: these paths mix SSIS packages (.dtsx) and other exports; load one tool at a time")
    if ssis_files:
        from .parsers import ssis

        return ssis.parse_files(ssis_files)
    return bods.parse_files(files)


def _config(a) -> Path | None:
    """--config, else the source folder's deltabound.toml, else ./deltabound.toml (handled by loaders)."""
    if getattr(a, "config", None):
        return Path(a.config)
    paths = getattr(a, "paths", None)
    return _project_manifest(paths) if paths else None


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
        cfg = load_convert_config(_config(a))
    except ConvertError as err:
        sys.exit(f"convert: {err}")
    out = Path(a.out)
    if out.exists() and not out.is_dir():
        sys.exit(f"convert: --out {out} exists and is not a folder")
    for j in jobs:
        try:
            r = convert_job(e, j, cfg)
        except ConvertError as err:
            sys.exit(f"convert: {err}")
        base = out / j if a.all else out
        for rel, text in r.files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        transforms = [t for d in r.dataflows for t in d.transforms]
        done = sum(1 for t in transforms if t.status == "converted")
        print(f"{j}: {done}/{len(transforms)} transforms converted, {r.todo_count} blocking TODO(s) -> {base}/conversion_report.md")


def cmd_validate_scaffold(a) -> None:
    from .convert import load_convert_config
    from .validate import scaffold

    e = _load(a.paths)
    if a.job not in e.objects or e.objects[a.job].kind != ObjectKind.JOB:
        sys.exit(f"Not a job in this export: {a.job}")
    text = scaffold(e, a.job, load_convert_config(_config(a)), a.business_date)
    Path(a.out).write_text(text)
    print(f"Wrote {a.out} ({text.count('[[steps]]')} steps). Review keys, ignored columns and paths before running.")


def cmd_validate_run(a) -> None:
    from .project import ProjectError, load_project

    manifest = _project_manifest([a.manifest])
    if manifest is not None:
        try:
            project = load_project(manifest)
        except ProjectError as e:
            sys.exit(f"validate: {e}")
        if not project.validate_manifests:
            sys.exit(f"validate: {manifest} lists no [validate] manifests")
        worst = 0
        for rel in project.validate_manifests:
            sub = argparse.Namespace(**vars(a))
            sub.manifest = str(project.base / rel)
            sub.out = str(Path(a.out) / Path(rel).parent.name)
            try:
                _validate_one(sub)
            except SystemExit as e:
                worst = max(worst, int(e.code or 0)) if isinstance(e.code, int) or e.code is None else 2
                if not isinstance(e.code, int) and e.code is not None:
                    print(e.code, file=sys.stderr)
        if worst:
            sys.exit(worst)
        return
    _validate_one(a)


def _validate_one(a) -> None:
    from .validate import (DuckDBEngine, ValidateError, dumps, load_manifest, render_html, render_markdown,
                           run_validation)

    try:
        m = load_manifest(Path(a.manifest), Path(a.waivers) if a.waivers else None)
        sources = a.estate or [str(m.base / p) for p in m.estate]
        if not sources:
            sys.exit("validate: no legacy export given (set estate = [...] in the manifest or pass --estate)")
        e = _load(sources)
        if a.engine == "spark":
            from pyspark.sql import SparkSession

            from .validate import SparkEngine

            engine = SparkEngine(SparkSession.builder.getOrCreate())
        else:
            engine = DuckDBEngine()
        report = json.loads(Path(a.conversion_report).read_text()) if a.conversion_report else None
        run = run_validation(e, m, engine, conversion_report=report)
    except ValidateError as err:
        sys.exit(f"validate: {err}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(dumps(run))
    (out / "report.md").write_text(render_markdown(run))
    (out / "report.html").write_text(render_html(run))
    from .validate.report import LABEL, headline

    print(f"{LABEL[run.status]}: {headline(run)}. Report: {out / 'report.html'}")
    if run.status == "fail":
        sys.exit(1)


def cmd_viewer(a) -> None:
    from .project import ProjectError, find_manifest, load_project
    from .serve import ServeError, serve

    files: list[Path] = []
    for raw in a.paths or []:
        p = Path(raw)
        if find_manifest(p) is not None:
            try:
                project = load_project(p)
                files += project.export_files()
            except ProjectError as e:
                sys.exit(f"viewer: {e}")
            bundle = project.base / "explanations.json"
            if bundle.exists():
                files.append(bundle)
        elif p.is_dir():
            files += sorted(f for pat in ("*.xml", "*.dtsx", "*.json", "*.zip") for f in p.rglob(pat))
        elif p.exists():
            files.append(p)
        else:
            sys.exit(f"viewer: {p} not found")
    try:
        server, link = serve(files, port=a.port, open_browser=not a.no_browser)
    except ServeError as e:
        sys.exit(f"viewer: {e}")
    shared = f"Preloading {len(files)} file(s). " if files else ""
    print(f"DeltaBound viewer:\n  {link}\n{shared}Serving on this machine only. Press Ctrl+C to stop.", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


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
    from .explain.governance import approve
    from .explain.render import render_text

    e = _load(a.paths)
    jobs = explainable_jobs(e) if a.all else [a.job]
    unknown = [j for j in jobs if j not in e.objects or e.objects[j].kind != ObjectKind.JOB]
    if unknown:
        sys.exit(f"Not a job in this export: {', '.join(unknown)}")
    try:
        cfg = load_config(_config(a))
        ex = Explainer(e, cfg, dry_run=a.dry_run)
        if a.dry_run:
            for j in jobs:
                ex.job(j, a.refresh)
            print(f"Dry run: prompts written to {cfg.cache_dir}/prompts/ (nothing was sent)")
            return
        plan = ex.plan(jobs, a.refresh)
        if plan.calls and cfg.provider != "fake":
            print(plan.describe())
            problems = plan.violations(cfg.limits)
            if problems:
                ex.audit.write("refused", token=plan.token, reason="over limits", problems=problems)
                sys.exit("explain: refused, the plan is over the limits in [explain.limits]: " + "; ".join(problems)
                         + ". Explain fewer jobs, lower max_tokens, or have the owner raise the limits.")
            if a.plan:
                print(f"\nNothing was sent. To run exactly this plan: --approve {plan.token}")
                return
            if a.approve:
                if a.approve != plan.token:
                    ex.audit.write("refused", token=plan.token, reason="approval token mismatch", given=a.approve)
                    sys.exit(f"explain: --approve {a.approve} does not match this plan ({plan.token}); the plan "
                             "changed since it was approved. Review it again with --plan.")
            elif sys.stdin.isatty():
                answer = input("Type APPROVE to send these requests (anything else cancels): ").strip()
                if answer != "APPROVE":
                    ex.audit.write("refused", token=plan.token, reason="not approved at the prompt")
                    sys.exit("explain: cancelled, nothing was sent.")
            else:
                ex.audit.write("refused", token=plan.token, reason="no approval (non-interactive)")
                sys.exit("explain: needs human approval. Review with --plan, then rerun with --approve "
                         f"{plan.token}. Nothing was sent.")
            ex.approval = approve(plan, a.approver)
        elif a.plan:
            print(f"Nothing to send: {plan.cached} answer(s) come from the cache." if not plan.calls
                  else "The fake provider sends nothing outside this machine; no approval needed.")
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
    s.add_argument("--plan", action="store_true", help="show what would be sent and its worst-case cost; send nothing")
    s.add_argument("--approve", metavar="TOKEN", help="approve exactly the plan with this token (non-interactive)")
    s.add_argument("--approver", help="name recorded in the audit log (default: your login name)")
    s.add_argument("--bundle", help="write all explanations to one JSON file (for the viewer)")
    s.add_argument("--config", help="settings file (default: ./deltabound.toml if present)")

    s = add("convert", cmd_convert, "convert a job to Databricks notebooks + Asset Bundle")
    which = s.add_mutually_exclusive_group(required=True)
    which.add_argument("--job")
    which.add_argument("--all", action="store_true", help="convert every job (one bundle per job)")
    s.add_argument("--out", required=True, help="output folder")
    s.add_argument("--config", help="settings file (default: ./deltabound.toml if present)")

    v = sub.add_parser("validate", help="compare legacy outputs with the converted pipeline, step by step")
    vsub = v.add_subparsers(dest="vcmd", required=True)
    vs = vsub.add_parser("scaffold", help="draft validation.toml for a job from the IR")
    vs.add_argument("paths", nargs="+", help="export files or folders")
    vs.add_argument("--job", required=True)
    vs.add_argument("--out", default="validation.toml")
    vs.add_argument("--business-date", default="YYYY-MM-DD")
    vs.add_argument("--config", help="deltabound.toml with [convert.datastores] for Databricks table names")
    vs.set_defaults(fn=cmd_validate_scaffold)
    vr = vsub.add_parser("run", help="run the comparisons and write result.json, report.md, report.html")
    vr.add_argument("manifest")
    vr.add_argument("--estate", nargs="+", help="legacy export files/folders (default: estate in the manifest)")
    vr.add_argument("--engine", choices=["duckdb", "spark"], default="duckdb")
    vr.add_argument("--out", default="validation_out")
    vr.add_argument("--waivers", help="extra waivers TOML ([[waivers]] entries)")
    vr.add_argument("--conversion-report", help="conversion_report.json from deltabound convert")
    vr.set_defaults(fn=cmd_validate_run)

    vw = sub.add_parser("viewer", help="open the web viewer on a local URL (optionally preloading a source)")
    vw.add_argument("paths", nargs="*", help="source folder, export files or folders to preload")
    vw.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (0 picks a free one)")
    vw.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")
    vw.set_defaults(fn=cmd_viewer)

    sc = sub.add_parser("schema", help="write the IR JSON Schema (used by the viewer's TypeScript types)")
    sc.add_argument("--out", default="ir.schema.json")
    sc.set_defaults(fn=cmd_schema)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

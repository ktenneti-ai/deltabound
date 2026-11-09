"""Entry points for the web viewer, which runs this package in the browser (Pyodide).

The viewer hands over raw file contents; everything here works on bytes, never
paths. Like cli.py this is a thin layer: format detection, then parsers and graph.
Results are plain JSON-ready dicts.
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Optional
from xml.etree import ElementTree as ET

import networkx as nx

from . import graph, report
from .ir import IR_VERSION, Estate, ObjectKind
from .parsers import bods

MAX_ZIP_DEPTH = 2
JUNK = ("__MACOSX/", ".DS_Store")

# Assumed markers, to tell users what a file is even when it is not supported yet.
SSIS_NAMESPACE = "www.microsoft.com/SqlServer/Dts"
ATL_SUFFIX = ".atl"

_state: dict = {"estate": None}


def _entry(file: str, fmt: str, status: str, message: str = "") -> dict:
    return {"file": file, "format": fmt, "status": status, "message": message}


def _expand(files: list[tuple[str, bytes]], report_: list[dict], depth: int = 0) -> list[tuple[str, bytes]]:
    """Unpack zip archives (up to MAX_ZIP_DEPTH levels); other files pass through."""
    out: list[tuple[str, bytes]] = []
    for name, data in files:
        if not data.startswith(b"PK\x03\x04"):
            out.append((name, data))
            continue
        if depth >= MAX_ZIP_DEPTH:
            report_.append(_entry(name, "zip", "error", "zip nested too deeply"))
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                inner = [(f"{name}/{i.filename}", z.read(i)) for i in z.infolist()
                         if not i.is_dir() and not any(j in i.filename for j in JUNK)]
        except zipfile.BadZipFile as e:
            report_.append(_entry(name, "zip", "error", f"not a valid zip file: {e}"))
            continue
        report_.append(_entry(name, "zip", "ok", f"{len(inner)} file(s) inside"))
        out += _expand(inner, report_, depth + 1)
    return out


def detect(name: str, data: bytes) -> tuple[str, str]:
    """(format, detail). Formats: estate_json, explanations_json, bods_xml, ssis, atl, unknown."""
    head = data.lstrip()[:1]
    if head == b"{":
        try:
            doc = json.loads(data)
        except ValueError as e:
            return "unknown", f"invalid JSON: {e}"
        # Check the explicit format marker first: an explanations bundle also carries ir_version.
        if isinstance(doc, dict) and doc.get("format") == "deltabound-explanations":
            return "explanations_json", ""
        if isinstance(doc, dict) and "ir_version" in doc and "objects" in doc:
            return "estate_json", str(doc["ir_version"])
        return "unknown", "JSON file that is neither an estate export nor an explanations bundle"
    if head == b"<":
        try:
            _, root = next(ET.iterparse(io.BytesIO(data), events=("start",)))
        except (ET.ParseError, StopIteration) as e:
            return "unknown", f"invalid XML: {e}"
        ns, _, local = root.tag.rpartition("}")
        if local == bods.TAG_ROOT:
            return "bods_xml", ""
        if SSIS_NAMESPACE in ns or name.lower().endswith(".dtsx"):
            return "ssis", ""
        return "unknown", f"XML with unrecognised root <{local}>"
    if name.lower().endswith(ATL_SUFFIX):
        return "atl", ""
    if name.lower().endswith(".toml"):
        return "source_manifest", ""
    return "unknown", "not JSON, XML or zip"


def load(files: list[tuple[str, bytes]]) -> dict:
    """Load any mix of supported files. Bad files are reported and skipped, never fatal."""
    rep: list[dict] = []
    bods_docs, ssis_docs, estate_docs, explanation_docs = [], [], [], []
    for name, data in _expand(files, rep):
        fmt, detail = detect(name, data)
        if fmt == "bods_xml":
            bods_docs.append((name, data))
        elif fmt == "estate_json":
            estate_docs.append((name, data, detail))
        elif fmt == "explanations_json":
            explanation_docs.append((name, data))
        elif fmt == "ssis":
            ssis_docs.append((name, data))
        elif fmt == "source_manifest":
            rep.append(_entry(name, "source_manifest", "skipped",
                              "source manifest: its settings are used by the command line; the viewer reads the export files"))
        elif fmt == "atl":
            rep.append(_entry(name, "atl", "unsupported",
                              "BODS ATL text exports are not supported yet; export the repository as XML."))
        else:
            rep.append(_entry(name, "unknown", "error",
                              f"{detail}. Supported: estate.json, explanations.json, BODS .xml exports, SSIS .dtsx packages, .zip of these."))

    estate: Optional[Estate] = None
    raw_docs = []
    if bods_docs and ssis_docs:  # one estate per tool: keep BODS, report the rest
        for name, _ in ssis_docs:
            rep.append(_entry(name, "ssis", "skipped", "SAP Data Services exports were also loaded; load one tool at a time"))
        ssis_docs = []
    if bods_docs or ssis_docs:
        from .parsers import ssis

        parser, fmt, docs = (bods, "bods_xml", bods_docs) if bods_docs else (ssis, "ssis", ssis_docs)
        estate = Estate(source_tool=parser.SOURCE_TOOL)
        parsed = 0
        for name, data in docs:
            try:
                parser.parse_document(name, data, estate)
                rep.append(_entry(name, fmt, "ok"))
                parsed += 1
            except ET.ParseError as e:
                rep.append(_entry(name, fmt, "error", f"malformed XML: {e}"))
        if not parsed:
            estate = None
        raw_docs = docs
        for name, _, _ in estate_docs:
            rep.append(_entry(name, "estate_json", "skipped",
                              "raw export files were also loaded; load one estate at a time"))
    if not raw_docs:
        for name, data, version in estate_docs:
            if estate is not None:
                rep.append(_entry(name, "estate_json", "skipped", "another estate.json was already loaded"))
                continue
            try:
                estate = Estate.model_validate_json(data)
            except ValueError as e:
                rep.append(_entry(name, "estate_json", "error", f"not a valid IR {version} file: {str(e)[:200]}"))
                continue
            note = "" if version.split(".")[:2] == IR_VERSION.split(".")[:2] else \
                f"IR {version} file read by IR {IR_VERSION} code"
            rep.append(_entry(name, "estate_json", "ok", note))

    explanations = None
    for name, data in explanation_docs:
        if explanations is not None:
            rep.append(_entry(name, "explanations_json", "skipped", "only one explanations bundle is used"))
            continue
        explanations = json.loads(data)
        rep.append(_entry(name, "explanations_json", "ok", f"{len(explanations.get('jobs', {}))} job(s)"))

    if estate is not None:  # loading only an explanations bundle keeps the current estate
        _state["estate"] = estate
    return {
        "ok": estate is not None,
        "load_report": rep,
        "estate": estate.model_dump(mode="json") if estate else None,
        "analysis": analysis(estate) if estate else None,
        "explanations": explanations,
    }


def analysis(estate: Estate) -> dict:
    g = graph.call_graph(estate)
    stats = {s["job"]: s for s in report.job_stats(estate, g)}
    for i, wave in enumerate(graph.migration_waves(estate, 10), 1):
        for name, _ in wave:
            stats[name]["wave"] = i
    jobs = [j.name for j in estate.by_kind(ObjectKind.JOB)]
    used_by: dict[str, list[str]] = {}
    for j in jobs:
        for n in nx.descendants(g, j):
            used_by.setdefault(n, []).append(j)
    transforms = [t for o in estate.objects.values() for t in o.transforms]
    opaque = sum(1 for t in transforms if t.kind == "opaque")
    return {
        "counts": {k.value: len(estate.by_kind(k)) for k in ObjectKind} | {"transforms": len(transforms),
                                                                          "opaque": opaque},
        "coverage": round(100 * (1 - opaque / len(transforms)), 1) if transforms else 100.0,
        "jobs": sorted(stats.values(), key=lambda s: (-s["score"], s["job"])),
        "trees": {o.name: graph.call_tree(estate, o.name)
                  for o in estate.objects.values() if o.kind != ObjectKind.DATAFLOW},
        "unreachable": graph.unreachable(estate),
        "missing": {m: sorted(g.predecessors(m)) for m in graph.missing_references(estate)},
        "used_by_jobs": {k: sorted(v) for k, v in used_by.items()},
    }


def lineage(dataflow: str) -> dict:
    """Column lineage edges of one dataflow, and which graph box owns each column node.

    Owners: "transform:<name>" (a transform, or the Case that owns a branch output)
    or "lookup:<qualified table>" (a reference table read by a lookup).
    """
    estate: Optional[Estate] = _state["estate"]
    if estate is None or dataflow not in estate.objects:
        return {"edges": [], "owner": {}}
    df = estate.objects[dataflow]
    g = graph.column_lineage(estate, dataflow)
    names = {t.name for t in df.transforms}
    branch_owner = {b.name: t.name for t in df.transforms for b in t.branches}
    target_by_table = {t.table.qualified: t.name for t in df.transforms
                       if t.table is not None and t.kind in graph.TARGET_KINDS}
    owner: dict[str, str] = {}
    for node in g.nodes:
        prefix = node.rsplit(".", 1)[0]
        if prefix in names:
            owner[node] = f"transform:{prefix}"
        elif prefix in branch_owner:
            owner[node] = f"transform:{branch_owner[prefix]}"
        elif prefix in target_by_table:
            owner[node] = f"transform:{target_by_table[prefix]}"
        elif "." in prefix:
            owner[node] = f"lookup:{prefix}"
    return {"edges": [[a, b] for a, b in g.edges], "owner": owner}


def load_json(files: list[tuple[str, bytes]]) -> str:
    """load() serialised, for the JavaScript side."""
    return json.dumps(load(files))


def lineage_json(dataflow: str) -> str:
    return json.dumps(lineage(dataflow))

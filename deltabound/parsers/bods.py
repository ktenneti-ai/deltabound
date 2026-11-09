"""Parser for SAP Data Services (BODS) XML repository exports.

IMPORTANT: the element and attribute names below follow the general shape of
BODS XML exports (DataIntegratorExport, DIJob, DIWorkflow, DIDataflow,
DICallStep, ...), but they were written without access to a real export.
Validate against real files (from a design partner or an SAP trial system,
never a client's) and adjust TAG_* constants. Anything not recognised is kept
as an "opaque" transform and counted in Estate.unparsed, so coverage stays
measurable and nothing is silently dropped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

from ..ir import (
    CallStep,
    ColumnMapping,
    Estate,
    EtlObject,
    ObjectKind,
    TableRef,
    Transform,
    Variable,
)

TAG_ROOT = "DataIntegratorExport"
TAG_KIND = {"DIJob": ObjectKind.JOB, "DIWorkflow": ObjectKind.WORKFLOW, "DIDataflow": ObjectKind.DATAFLOW}
TAG_CALL = "DICallStep"
TAG_CONDITIONALS = {"DIIf", "DIWhileStep", "DITryCatch"}
TAG_VARIABLE = {"DIVariable", "DIParameter"}
TAG_SCRIPT = "DIScript"

TRANSFORM_KINDS = {
    "DIDatabaseTableSource": "table_source",
    "DIDatabaseTableTarget": "table_target",
    "DIFileSource": "file_source",
    "DIFileTarget": "file_target",
    "DIQuery": "query",
    "DITableComparison": "table_comparison",
    "DIHistoryPreserving": "history_preserving",
    "DIKeyGeneration": "key_generation",
    "DICase": "case",
    "DIMerge": "merge",
    "DIValidation": "validation",
    "DILookup": "lookup",
    "DIMapOperation": "map_operation",
}

COL_REF = re.compile(r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\b")


def parse_files(paths: Iterable[Path | str]) -> Estate:
    estate = Estate(source_tool="sap_data_services")
    for p in paths:
        _parse_file(Path(p), estate)
    return estate


def _parse_file(path: Path, estate: Estate) -> None:
    root = ET.parse(path).getroot()
    if root.tag != TAG_ROOT:
        estate.unparsed.append(f"{path.name}: unexpected root <{root.tag}>")
    estate.source_version = estate.source_version or root.get("productVersion")
    for el in root:
        kind = TAG_KIND.get(el.tag)
        if kind is None:
            estate.unparsed.append(f"{path.name}: top-level <{el.tag}>")
            continue
        obj = _parse_object(el, kind, estate)
        estate.objects[obj.name] = obj


def _parse_object(el: ET.Element, kind: ObjectKind, estate: Estate) -> EtlObject:
    obj = EtlObject(name=el.get("name", "?"), kind=kind, description=el.get("description"))

    for v in el.iter():
        if v.tag in TAG_VARIABLE:
            obj.variables.append(
                Variable(
                    name=v.get("name", "?"),
                    scope=v.get("scope", "global" if kind == ObjectKind.JOB else "local"),
                    data_type=v.get("dataType"),
                    default=v.get("defaultValue"),
                )
            )
        elif v.tag == TAG_SCRIPT:
            obj.scripts.append((v.text or v.get("text") or "").strip())

    order = 0
    for call, condition in _walk_calls(el, None):
        order += 1
        target_kind = {"Workflow": ObjectKind.WORKFLOW, "Dataflow": ObjectKind.DATAFLOW}.get(
            call.get("calledObjectType", "")
        )
        obj.calls.append(
            CallStep(target=call.get("name", "?"), target_kind=target_kind, condition=condition, order=order)
        )

    if kind == ObjectKind.DATAFLOW:
        container = el.find("DITransforms")
        for t in (container if container is not None else []):
            obj.transforms.append(_parse_transform(t, estate, obj.name))
    return obj


def _walk_calls(el: ET.Element, condition: Optional[str]):
    """Yield (call element, enclosing condition) in document order."""
    for child in el:
        if child.tag == TAG_CALL:
            yield child, condition
        elif child.tag in TAG_CONDITIONALS:
            cond = child.get("condition") or child.get("expr") or child.tag
            for branch in child:
                label = {"DIThen": cond, "DIElse": f"NOT ({cond})"}.get(branch.tag, cond)
                yield from _walk_calls(branch, label if condition is None else f"{condition} AND {label}")
        else:
            yield from _walk_calls(child, condition)


def _parse_transform(t: ET.Element, estate: Estate, df_name: str) -> Transform:
    kind = TRANSFORM_KINDS.get(t.tag)
    if kind is None:
        estate.unparsed.append(f"{df_name}: transform <{t.tag}>")
        kind = "opaque"
    tr = Transform(name=t.get("name") or t.get("tableName") or t.tag, kind=kind, raw_tag=t.tag)
    tr.inputs = [iv.get("name", "?") for iv in t.findall("DIInputView")]
    if t.get("tableName"):
        tr.table = TableRef(datastore=t.get("datastoreName"), owner=t.get("ownerName"), table=t.get("tableName"))
        if kind == "table_source":
            tr.name = t.get("tableName")
    for col in t.iter("DIElement"):
        expr = None
        for attr in col.findall("DIAttribute"):
            if attr.get("name") == "ui_mapping_text":
                expr = attr.get("value")
        tr.columns.append(
            ColumnMapping(
                column=col.get("name", "?"),
                data_type=col.get("type"),
                expression=expr,
                source_columns=[f"{a}.{b}" for a, b in COL_REF.findall(expr or "")],
            )
        )
    where = t.find("DIWhereClause")
    if where is not None:
        tr.where = (where.get("expr") or where.text or "").strip()
    return tr

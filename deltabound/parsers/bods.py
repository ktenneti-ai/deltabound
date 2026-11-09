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
from typing import Iterable, Iterator, Optional
from xml.etree import ElementTree as ET

from ..ir import (
    Branch,
    CallStep,
    ColumnMapping,
    Estate,
    EtlObject,
    Join,
    LookupRef,
    ObjectKind,
    SortKey,
    TableRef,
    Transform,
    Variable,
)

TAG_ROOT = "DataIntegratorExport"
TAG_KIND = {"DIJob": ObjectKind.JOB, "DIWorkflow": ObjectKind.WORKFLOW, "DIDataflow": ObjectKind.DATAFLOW}
TAG_CALL = "DICallStep"
TAG_VARIABLE = {"DIVariable", "DIParameter"}
TAG_PARAMETER = "DIParameter"
TAG_SCRIPT = "DIScript"

# Control flow. UNVERIFIED: in real exports try/catch may be two sibling
# objects rather than one DITryCatch element with DITry/DICatch children.
TAG_IF = "DIIf"
TAG_THEN = "DIThen"
TAG_ELSE = "DIElse"
TAG_WHILE = "DIWhileStep"
TAG_TRY_CATCH = "DITryCatch"
TAG_CATCH = "DICatch"

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

# Transform internals. UNVERIFIED: names assumed, adjust against real exports.
TAG_INPUT = "DIInputView"
TAG_COLUMN = "DIElement"
TAG_COLUMN_ATTR = "DIAttribute"
MAPPING_ATTR = "ui_mapping_text"
TAG_WHERE = "DIWhereClause"
TAG_JOINS = "DIJoins"
TAG_JOIN_PAIR = "DIJoinPair"  # leftSchema, rightSchema, joinType, expr
TAG_GROUP_BY = "DIGroupBy"
TAG_ORDER_BY = "DIOrderBy"
TAG_EXPRESSION = "DIExpression"  # expr, sortType (ASC|DESC) inside DIOrderBy
TAG_CASE_BRANCH = "DICaseBranch"  # name, expr, isDefault
TAG_LOOKUP_CONDITION = "DILookupCondition"  # lookupColumn, inputExpr (DILookup transform)
TAG_LOOKUP_RETURN = "DILookupReturn"  # column

# Attributes mapped to IR fields; every other transform attribute goes to Transform.options.
CONSUMED_ATTRS = {"name", "tableName", "datastoreName", "ownerName"}

JOIN_TYPES = {
    "inner": "inner", "left": "left_outer", "left_outer": "left_outer",
    "right": "right_outer", "right_outer": "right_outer",
    "full": "full_outer", "full_outer": "full_outer", "cross": "cross",
}

# Lookup functions inside mapping expressions. Assumed argument layouts:
#   lookup_ext([DS.OWNER.TABLE, 'cache', 'policy'], [RET_COL, ...], [default, ...],
#              [LKP_COL, '=', input_expr, ...], ...)
#   lookup(DS.OWNER.TABLE, RET_COL, default, 'cache', LKP_COL, input_expr[, LKP_COL, input_expr ...])
LOOKUP_CALL = re.compile(r"\b(lookup_ext|lookup)\s*\(", re.IGNORECASE)
TABLE_NAME = re.compile(r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*){1,2}")
COL_REF = re.compile(r"(?<![\w$.])([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\b(?!\.)")
STRING_LITERAL = re.compile(r"'[^']*'")


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
        if obj.name in estate.objects:
            estate.unparsed.append(
                f"{path.name}: duplicate {kind.value} {obj.name}; first definition kept, this one ignored"
            )
            continue
        estate.objects[obj.name] = obj


def _parse_object(el: ET.Element, kind: ObjectKind, estate: Estate) -> EtlObject:
    obj = EtlObject(name=el.get("name", "?"), kind=kind, description=el.get("description"))

    for v in el.iter():
        if v.tag in TAG_VARIABLE:
            default_scope = "parameter" if v.tag == TAG_PARAMETER else ("global" if kind == ObjectKind.JOB else "local")
            obj.variables.append(
                Variable(
                    name=v.get("name", "?"),
                    scope=v.get("scope", default_scope),
                    data_type=v.get("dataType"),
                    default=v.get("defaultValue"),
                )
            )
        elif v.tag == TAG_SCRIPT:
            obj.scripts.append((v.text or v.get("text") or "").strip())

    for order, (call, condition, control) in enumerate(_walk_calls(el, None, None), 1):
        target_kind = {"Workflow": ObjectKind.WORKFLOW, "Dataflow": ObjectKind.DATAFLOW}.get(
            call.get("calledObjectType", "")
        )
        obj.calls.append(
            CallStep(target=call.get("name", "?"), target_kind=target_kind,
                     condition=condition, control=control, order=order)
        )

    if kind == ObjectKind.DATAFLOW:
        container = el.find("DITransforms")
        for t in (container if container is not None else []):
            obj.transforms.append(_parse_transform(t, estate, obj.name))
    return obj


def _and(outer: Optional[str], inner: str) -> str:
    return inner if outer is None else f"{outer} AND {inner}"


def _walk_calls(
    el: ET.Element, condition: Optional[str], control: Optional[str]
) -> Iterator[tuple[ET.Element, Optional[str], Optional[str]]]:
    """Yield (call element, enclosing condition, innermost control construct) in document order."""
    for child in el:
        if child.tag == TAG_CALL:
            yield child, condition, control
        elif child.tag == TAG_IF:
            cond = child.get("condition") or child.get("expr") or "?"
            for branch in child:
                label = f"NOT ({cond})" if branch.tag == TAG_ELSE else cond
                yield from _walk_calls(branch, _and(condition, label), "branch")
        elif child.tag == TAG_WHILE:
            cond = child.get("condition") or child.get("expr") or "?"
            yield from _walk_calls(child, _and(condition, f"while {cond}"), "loop")
        elif child.tag == TAG_TRY_CATCH:
            for branch in child:
                if branch.tag == TAG_CATCH:
                    label = f"on error ({branch.get('exceptionGroup', 'ALL')})"
                    yield from _walk_calls(branch, _and(condition, label), "error_handler")
                else:  # the try block runs unconditionally
                    yield from _walk_calls(branch, condition, control)
        else:
            yield from _walk_calls(child, condition, control)


def _parse_transform(t: ET.Element, estate: Estate, df_name: str) -> Transform:
    kind = TRANSFORM_KINDS.get(t.tag)
    if kind is None:
        estate.unparsed.append(f"{df_name}: transform <{t.tag}>")
        kind = "opaque"
    tr = Transform(name=t.get("name") or t.get("tableName") or t.tag, kind=kind, raw_tag=t.tag)
    tr.inputs = [iv.get("name", "?") for iv in t.findall(TAG_INPUT)]
    tr.options = {k: v for k, v in t.attrib.items() if k not in CONSUMED_ATTRS}
    if t.get("tableName"):
        tr.table = TableRef(datastore=t.get("datastoreName"), owner=t.get("ownerName"), table=t.get("tableName"))
        if kind == "table_source":
            tr.name = t.get("tableName")

    for col in t.iter(TAG_COLUMN):
        expr = None
        for attr in col.findall(TAG_COLUMN_ATTR):
            if attr.get("name") == MAPPING_ATTR:
                expr = attr.get("value")
        name = col.get("name", "?")
        lookups = _parse_lookup_calls(expr or "", name, estate, f"{df_name}.{tr.name}.{name}")
        tr.lookups += lookups
        tr.columns.append(
            ColumnMapping(column=name, data_type=col.get("type"), expression=expr,
                          source_columns=_column_sources(expr or "", lookups))
        )

    where = t.find(TAG_WHERE)
    if where is not None:
        tr.where = (where.get("expr") or where.text or "").strip()
    tr.joins = [
        Join(left=j.get("leftSchema", "?"), right=j.get("rightSchema", "?"),
             join_type=_join_type(j.get("joinType")), condition=j.get("expr"))
        for j in t.findall(f"{TAG_JOINS}/{TAG_JOIN_PAIR}")
    ]
    tr.group_by = [_expr(x) for x in t.findall(f"{TAG_GROUP_BY}/{TAG_EXPRESSION}")]
    tr.order_by = [
        SortKey(expression=_expr(x), direction=(x.get("sortType") or "asc").lower())
        for x in t.findall(f"{TAG_ORDER_BY}/{TAG_EXPRESSION}")
    ]
    tr.branches = [
        Branch(name=b.get("name", "?"), condition=b.get("expr"),
               is_default=(b.get("isDefault", "false").lower() == "true"))
        for b in t.findall(TAG_CASE_BRANCH)
    ]
    if kind == "lookup":
        tr.lookups.append(
            LookupRef(
                function="lookup_transform",
                table=tr.table,
                return_columns=[r.get("column", "?") for r in t.findall(TAG_LOOKUP_RETURN)],
                match_columns=[(c.get("lookupColumn", "?"), c.get("inputExpr", "?"))
                               for c in t.findall(TAG_LOOKUP_CONDITION)],
            )
        )
    return tr


def _expr(x: ET.Element) -> str:
    return (x.get("expr") or x.text or "").strip()


def _join_type(raw: Optional[str]) -> str:
    key = (raw or "inner").strip().lower().replace(" join", "").replace(" ", "_")
    return JOIN_TYPES.get(key, key)


# ---------- lookup function calls ----------

def _find_calls(expr: str) -> list[tuple[str, int, int, Optional[str]]]:
    """(function, start, end, argument text or None if parentheses are unbalanced)."""
    found: list[tuple[str, int, int, Optional[str]]] = []
    pos = 0
    while (m := LOOKUP_CALL.search(expr, pos)) is not None:
        depth, in_quote, end = 0, False, None
        for j in range(m.end() - 1, len(expr)):
            ch = expr[j]
            if ch == "'":
                in_quote = not in_quote
            elif not in_quote and ch in "([":
                depth += 1
            elif not in_quote and ch in ")]":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end is None:
            found.append((m.group(1).lower(), m.start(), len(expr), None))
            break
        found.append((m.group(1).lower(), m.start(), end, expr[m.end():end - 1]))
        pos = end
    return found


def _split_top(text: str) -> list[str]:
    """Split on commas that are not inside brackets, parentheses or quotes."""
    parts, depth, in_quote, cur = [], 0, False, []
    for ch in text:
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote and ch in "([":
            depth += 1
        elif not in_quote and ch in ")]":
            depth -= 1
        if ch == "," and depth == 0 and not in_quote:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _unbracket(text: str) -> str:
    text = text.strip()
    return text[1:-1] if text.startswith("[") and text.endswith("]") else text


def _table_ref(text: str) -> Optional[TableRef]:
    text = text.strip()
    if not TABLE_NAME.fullmatch(text):
        return None
    parts = text.split(".")
    if len(parts) == 3:
        return TableRef(datastore=parts[0], owner=parts[1], table=parts[2])
    return TableRef(datastore=parts[0], table=parts[1])


def _parse_lookup_calls(expr: str, column: str, estate: Estate, where: str) -> list[LookupRef]:
    refs: list[LookupRef] = []
    for function, start, end, args in _find_calls(expr):
        raw = expr[start:end]
        ref = LookupRef(function=function, output_column=column, raw=raw)
        problem = None
        if args is None:
            problem = "unbalanced brackets"
        else:
            parts = _split_top(args)
            if function == "lookup_ext" and len(parts) >= 4:
                table_text = (_split_top(_unbracket(parts[0])) or [""])[0]
                ref.return_columns = _split_top(_unbracket(parts[1]))
                cond = _split_top(_unbracket(parts[3]))
                if len(cond) % 3:
                    problem = "condition list is not (column, operator, expression) triples"
                ref.match_columns = [(cond[i], cond[i + 2]) for i in range(0, len(cond) - 2, 3)]
            elif function == "lookup" and len(parts) >= 6:
                table_text = parts[0]
                ref.return_columns = [parts[1]]
                ref.match_columns = [(parts[i], parts[i + 1]) for i in range(4, len(parts) - 1, 2)]
            else:
                table_text = ""
                problem = f"unexpected number of arguments ({len(parts)})"
            ref.table = _table_ref(table_text)
            if ref.table is None and problem is None:
                problem = f"table '{table_text}' cannot be resolved statically"
        if problem:
            shown = raw if len(raw) <= 80 else raw[:77] + "..."
            estate.unparsed.append(f"{where}: {function} call not fully parsed ({problem}): {shown}")
        refs.append(ref)
    return refs


def _refs(text: str) -> list[str]:
    return [f"{a}.{b}" for a, b in COL_REF.findall(STRING_LITERAL.sub("''", text))]


def _column_sources(expr: str, lookups: list[LookupRef]) -> list[str]:
    """Input columns an expression reads, with lookup calls resolved to their tables."""
    rest = expr
    for _, start, end, _ in reversed(_find_calls(expr)):
        rest = rest[:start] + " " + rest[end:]
    sources = _refs(rest)
    for lk in lookups:
        if lk.table is not None:
            sources += [f"{lk.table.qualified}.{c}" for c in lk.return_columns]
        for _, input_expr in lk.match_columns:
            sources += _refs(input_expr)
    return list(dict.fromkeys(sources))

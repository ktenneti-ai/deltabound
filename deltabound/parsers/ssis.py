"""Parser for SQL Server Integration Services packages (.dtsx, package format 8 / SSIS 2012+).

IMPORTANT: element, attribute and componentClassID names below follow the documented shape
of .dtsx files but have not been checked against packages from a real project. Keep them in
the constants so they are easy to correct. Unknown executables and components are kept
(opaque, or task type "other") and listed in Estate.unparsed; nothing is dropped silently.

Mapping to the IR:
  package              -> job                    (named after the package)
  Sequence/For/Foreach -> workflow               (named PACKAGE\\path); loops: control="loop"
  Data Flow Task       -> dataflow               (named PACKAGE\\path)
  Execute SQL/Script/… -> task (task_type)       (named PACKAGE\\path)
  Execute Package Task -> call to the child package's job
  precedence constraint-> CallStep.depends_on/trigger (+ condition for expressions)
  OnError handler      -> calls with control="error_handler"
  connection manager   -> Estate.datastores (secrets removed)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

from ..ir import (
    Branch,
    CallStep,
    ColumnMapping,
    Datastore,
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

SOURCE_TOOL = "ssis"
DTS = "www.microsoft.com/SqlServer/Dts"
SQLTASK = "www.microsoft.com/sqlserver/dts/tasks/sqltask"

PACKAGE_TYPES = {"Microsoft.Package", "SSIS.Package.3", "MSDTS.Package.1"}
CONTAINER_TYPES = {"STOCK:SEQUENCE": None, "STOCK:FORLOOP": "for", "STOCK:FOREACHLOOP": "foreach"}
DATAFLOW_TYPES = {"Microsoft.Pipeline", "SSIS.Pipeline.3", "{5918251B-2970-45A4-AB5F-01C3C588FE5A}"}
EXECUTE_PACKAGE_TYPES = {"Microsoft.ExecutePackageTask", "SSIS.ExecutePackageTask.3"}
TASK_TYPES = {
    "Microsoft.ExecuteSQLTask": "sql", "Microsoft.ScriptTask": "script", "Microsoft.FileSystemTask": "file",
    "Microsoft.SendMailTask": "mail", "Microsoft.ExecuteProcess": "process", "Microsoft.FtpTask": "file",
    "Microsoft.BulkInsertTask": "sql", "Microsoft.ExpressionTask": "script",
}
CONSTRAINT_TRIGGER = {"0": "success", "1": "failure", "2": "completion"}

COMPONENT_KINDS = {
    "Microsoft.OLEDBSource": "table_source", "Microsoft.ADONETSource": "table_source",
    "Microsoft.OLEDBDestination": "table_target", "Microsoft.ADONETDestination": "table_target",
    "Microsoft.FlatFileSource": "file_source", "Microsoft.FlatFileDestination": "file_target",
    "Microsoft.DerivedColumn": "derived_column", "Microsoft.ConditionalSplit": "case", "Microsoft.Lookup": "lookup",
    "Microsoft.UnionAll": "merge", "Microsoft.MergeJoin": "join", "Microsoft.Aggregate": "aggregate",
    "Microsoft.Sort": "sort", "Microsoft.DataConvert": "data_conversion", "Microsoft.Multicast": "multicast",
    "Microsoft.RowCount": "row_count", "Microsoft.SCD": "scd", "Microsoft.OLEDBCommand": "sql_command",
}
# Components that output every input column they do not redefine.
PASS_THROUGH = {"derived_column", "case", "lookup", "sort", "data_conversion", "multicast", "row_count", "sql_command"}
JOIN_TYPES = {"0": "full_outer", "1": "left_outer", "2": "inner"}
AGGREGATIONS = {"0": None, "1": "COUNT", "2": "COUNT", "3": "COUNT_DISTINCT", "4": "SUM", "5": "AVG", "6": "MIN", "7": "MAX"}
SECRET_KEYS = re.compile(r"^(password|pwd|accountkey|sas|secret|token)$", re.I)
DATA_TYPES = {"i1": "tinyint", "i2": "smallint", "i4": "int", "i8": "bigint", "ui1": "tinyint", "ui2": "int",
              "ui4": "bigint", "ui8": "bigint", "r4": "real", "r8": "double", "bool": "boolean", "dbDate": "date",
              "date": "date", "dbTimeStamp": "datetime", "dbTimeStamp2": "datetime", "dbTime": "time",
              "guid": "varchar(38)"}
COLUMN_REF = re.compile(r"\[([^\[\]]+)\]")


def d(name: str) -> str:
    return f"{{{DTS}}}{name}"


def a(el: ET.Element, name: str) -> Optional[str]:
    return el.get(d(name))


def parse_files(paths: Iterable[Path | str]) -> Estate:
    estate = Estate(source_tool=SOURCE_TOOL)
    for p in paths:
        p = Path(p)
        parse_document(p.name, p.read_bytes(), estate)
    return estate


def parse_document(name: str, data: bytes, estate: Estate) -> None:
    """Parse one .dtsx file (bytes) into `estate`. Raises ET.ParseError for malformed XML."""
    root = ET.fromstring(data)
    if root.tag != d("Executable") or a(root, "ExecutableType") not in PACKAGE_TYPES:
        estate.unparsed.append(f"{name}: not an SSIS package (root <{root.tag}>)")
        return
    _Package(name, root, estate).parse()


class _Package:
    def __init__(self, file_name: str, root: ET.Element, estate: Estate):
        self.file, self.root, self.estate = file_name, root, estate
        self.name = a(root, "ObjectName") or Path(file_name).stem
        self.conn_names: dict[str, str] = {}

    def path_name(self, ref: str) -> str:
        """refId 'Package\\SEQ Load\\DFT X' -> 'PKG_NAME\\SEQ Load\\DFT X';
        'Package.EventHandlers[OnError]\\T' -> 'PKG_NAME\\OnError\\T'."""
        ref = re.sub(r"^Package\.EventHandlers\[([^\]]+)\]", r"Package\\\1", ref)
        return self.name + ref[len("Package"):] if ref.startswith("Package") else f"{self.name}\\{ref}"

    def parse(self) -> None:
        version = next((p.text for p in self.root.findall(d("Property")) if a(p, "Name") == "PackageFormatVersion"), None)
        if version:
            self.estate.source_version = self.estate.source_version or f"PackageFormatVersion {version}"
        self.connections()
        job = EtlObject(name=self.name, kind=ObjectKind.JOB, description=f"SSIS package {self.file}")
        job.variables = self.variables(self.root, "package")
        self.container(self.root, job)
        for h in self.root.findall(f"{d('EventHandlers')}/{d('EventHandler')}"):
            event = a(h, "EventName") or "event"
            wf = EtlObject(name=f"{self.name}\\{event}", kind=ObjectKind.WORKFLOW, description=f"{event} event handler")
            self.container(h, wf)
            self.add(wf)
            job.calls.append(CallStep(target=wf.name, target_kind=ObjectKind.WORKFLOW, order=len(job.calls) + 1,
                                      condition=f"on error ({event} event)", control="error_handler",
                                      depends_on=[], trigger="failure"))
        self.add(job)

    def add(self, obj: EtlObject) -> None:
        if obj.name in self.estate.objects:
            self.estate.unparsed.append(f"{self.file}: duplicate {obj.kind.value} {obj.name}; first definition kept")
            return
        self.estate.objects[obj.name] = obj

    # ---------- package level ----------

    def connections(self) -> None:
        for cm in self.root.findall(f"{d('ConnectionManagers')}/{d('ConnectionManager')}"):
            name = a(cm, "ObjectName") or "?"
            ref = a(cm, "refId")
            if ref:
                self.conn_names[ref] = name
            inner = cm.find(f"{d('ObjectData')}/{d('ConnectionManager')}")
            cs = a(inner, "ConnectionString") if inner is not None else None
            props: dict[str, str] = {}
            if cs and "=" in cs:
                for part in cs.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if not SECRET_KEYS.match(k.strip().replace(" ", "")):
                            props[k.strip()] = v.strip()
            elif cs:
                props["path"] = cs
            if name not in self.estate.datastores:
                self.estate.datastores[name] = Datastore(name=name, type=a(cm, "CreationName"), properties=props)

    def variables(self, el: ET.Element, scope: str) -> list[Variable]:
        out = []
        for v in el.findall(f"{d('Variables')}/{d('Variable')}"):
            if a(v, "Namespace") == "System":
                continue
            value = v.find(d("VariableValue"))
            out.append(Variable(name=f"{a(v, 'Namespace') or 'User'}::{a(v, 'ObjectName')}", scope=scope,
                                data_type=a(value, "DataType") if value is not None else None,
                                default=value.text if value is not None else None,
                                expression=a(v, "Expression") if (a(v, "EvaluateAsExpression") or "").lower() == "true" else None))
        return out

    # ---------- control flow ----------

    def container(self, el: ET.Element, obj: EtlObject) -> None:
        children = el.findall(f"{d('Executables')}/{d('Executable')}")
        order = {a(c, "refId"): i for i, c in enumerate(children, 1)}
        incoming: dict[str, list[ET.Element]] = {}
        for pc in el.findall(f"{d('PrecedenceConstraints')}/{d('PrecedenceConstraint')}"):
            incoming.setdefault(a(pc, "To") or "", []).append(pc)
        for i, child in enumerate(children, 1):
            ref = a(child, "refId") or f"Package\\{a(child, 'ObjectName')}"
            step = self.executable(child, ref)
            if step is None:
                continue
            step.order = i
            preds = incoming.get(ref, [])
            step.depends_on = sorted(order[a(p, "From")] for p in preds if a(p, "From") in order)
            missing = [a(p, "From") for p in preds if a(p, "From") not in order]
            if missing:
                self.estate.unparsed.append(f"{self.file}: precedence constraint from unknown {missing[0]}")
            if preds:
                triggers = {CONSTRAINT_TRIGGER.get(a(p, "Value") or "0", "success") for p in preds}
                step.trigger = "failure" if "failure" in triggers else ("completion" if triggers == {"completion"} else "success")
                exprs = [a(p, "Expression") for p in preds if a(p, "Expression") and (a(p, "EvalOp") or "") in ("1", "3", "4")]
                if step.trigger == "failure":
                    names = ", ".join(self.path_name(a(p, "From") or "?").rsplit("\\", 1)[-1] for p in preds
                                      if CONSTRAINT_TRIGGER.get(a(p, "Value") or "0") == "failure")
                    step.condition, step.control = f"on failure of {names}", "error_handler"
                elif exprs:
                    joiner = " AND " if all((a(p, "LogicalAnd") or "True").lower() == "true" for p in preds) else " OR "
                    step.condition, step.control = joiner.join(exprs), "branch"
            obj.calls.append(step)

    def executable(self, el: ET.Element, ref: str) -> Optional[CallStep]:
        typ = a(el, "ExecutableType") or "?"
        name = self.path_name(ref)
        if typ in EXECUTE_PACKAGE_TYPES:
            pkg = el.findtext(f"{d('ObjectData')}/ExecutePackageTask/PackageName") or "?"
            return CallStep(target=Path(pkg.replace("\\", "/")).stem, target_kind=ObjectKind.JOB)
        if typ in DATAFLOW_TYPES:
            self.add(self.dataflow(el, name))
            return CallStep(target=name, target_kind=ObjectKind.DATAFLOW)
        if typ in CONTAINER_TYPES:
            wf = EtlObject(name=name, kind=ObjectKind.WORKFLOW, description=a(el, "ObjectName"))
            wf.variables = self.variables(el, "local")
            self.container(el, wf)
            self.add(wf)
            step = CallStep(target=name, target_kind=ObjectKind.WORKFLOW)
            loop = CONTAINER_TYPES[typ]
            if loop:
                step.control, step.condition = "loop", self.loop_condition(el, loop)
            return step
        task_type = TASK_TYPES.get(typ)
        if task_type is None:
            self.estate.unparsed.append(f"{self.file}: executable <{typ}> {a(el, 'ObjectName')}")
            task_type = "other"
        task = EtlObject(name=name, kind=ObjectKind.TASK, task_type=task_type, description=a(el, "ObjectName"))
        if task_type == "sql":
            data = el.find(f"{d('ObjectData')}/{{{SQLTASK}}}SqlTaskData")
            sql = data.get(f"{{{SQLTASK}}}SqlStatementSource") if data is not None else None
            if sql:
                task.scripts.append(sql)
        elif task_type == "script":
            task.scripts.append("(script task: its code is compiled into the package, not exported as text)")
        task.variables = self.variables(el, "local")
        self.add(task)
        return CallStep(target=name, target_kind=ObjectKind.TASK)

    def loop_condition(self, el: ET.Element, loop: str) -> str:
        if loop == "for":
            return f"for {a(el, 'InitExpression') or ''}; {a(el, 'EvalExpression') or '?'}; {a(el, 'AssignExpression') or ''}".strip()
        enum = el.find(d("ForEachEnumerator"))
        if enum is None:
            return "foreach (enumerator not exported)"
        props = {k: v for p in enum.iter("FEFEProperty") for k, v in p.attrib.items()}
        where = " ".join(f"{k}={v}" for k, v in props.items())
        return f"foreach {a(enum, 'ObjectName') or a(enum, 'CreationName')}" + (f" ({where})" if where else "")

    # ---------- data flow ----------

    def dataflow(self, el: ET.Element, name: str) -> EtlObject:
        df = EtlObject(name=name, kind=ObjectKind.DATAFLOW, description=a(el, "ObjectName"))
        pipeline = el.find(f"{d('ObjectData')}/pipeline")
        if pipeline is None:
            self.estate.unparsed.append(f"{name}: data flow without a pipeline")
            return df
        comps = pipeline.findall("components/component")
        outputs: dict[str, tuple[str, str, ET.Element]] = {}  # output refId -> (component, output name, element)
        inputs: dict[str, tuple[str, str]] = {}  # input refId -> (component, input name)
        lineage: dict[str, str] = {}  # lineageId -> column name
        for c in comps:
            cname = c.get("name", "?")
            for o in c.findall("outputs/output"):
                outputs[o.get("refId", "")] = (cname, o.get("name", ""), o)
                for oc in o.findall("outputColumns/outputColumn"):
                    lineage[oc.get("lineageId", "")] = oc.get("name", "")
            for i in c.findall("inputs/input"):
                inputs[i.get("refId", "")] = (cname, i.get("name", ""))
        kinds = {c.get("name", "?"): COMPONENT_KINDS.get(c.get("componentClassID", ""), "opaque") for c in comps}
        feeds: dict[str, list[tuple[str, str]]] = {}  # component -> [(input name, source output name)]
        for p in pipeline.findall("paths/path"):
            src, dst = outputs.get(p.get("startId", "")), inputs.get(p.get("endId", ""))
            if src is None or dst is None:
                self.estate.unparsed.append(f"{name}: path {p.get('refId')} with unknown ends")
                continue
            feeds.setdefault(dst[0], []).append((dst[1], self.output_ref(kinds[src[0]], src[0], src[1], src[2])))
        for c in comps:
            df.transforms.append(self.component(c, kinds[c.get("name", "?")], feeds, lineage, df.name))
        sorts = {t.name: t for t in df.transforms if t.kind == "sort"}
        for t in df.transforms:
            if t.kind == "join":
                self.finish_join(t, sorts)
        return df

    @staticmethod
    def is_branch(kind: str, output: ET.Element) -> bool:
        return kind in ("case", "lookup") or (output.get("isErrorOut") or "").lower() == "true"

    def output_ref(self, kind: str, comp: str, out: str, element: ET.Element) -> str:
        return f"{comp}.{out}" if self.is_branch(kind, element) else comp

    def component(self, c: ET.Element, kind: str, feeds: dict, lineage: dict[str, str], df_name: str) -> Transform:
        name = c.get("name", "?")
        cls = c.get("componentClassID", "?")
        props = {p.get("name", ""): (p.text or "") for p in c.findall("properties/property")}
        t = Transform(name=name, kind=kind, raw_tag=cls)
        if kind == "opaque":
            self.estate.unparsed.append(f"{df_name}: component <{cls}> {name}")
        sources = sorted(feeds.get(name, []), key=lambda x: (0 if "Left" in x[0] else 1 if "Right" in x[0] else 2, x[0]))
        t.inputs = [s for _, s in sources]
        first = t.inputs[0] if t.inputs else None
        conn = c.find("connections/connection")
        datastore = self.conn_names.get(conn.get("connectionManagerID", ""), None) if conn is not None else None
        t.passes_through = kind in PASS_THROUGH
        # Options: properties not mapped to IR fields.
        mapped = {"OpenRowset", "SqlCommand", "AccessMode", "JoinType", "NumKeyColumns"}
        t.options = {k: v for k, v in props.items() if k not in mapped and v != ""}

        if kind in ("table_source", "table_target", "sql_command"):
            access = props.get("AccessMode", "0")
            if props.get("OpenRowset") and access in ("0", "3", "1"):
                t.table = self.table(props["OpenRowset"], datastore)
            if props.get("SqlCommand") and (access == "2" or kind == "sql_command"):
                t.sql = props["SqlCommand"]
        if kind in ("file_source", "file_target") and datastore:
            path = self.estate.datastores.get(datastore)
            if path and path.properties.get("path"):
                t.options["fileName"] = path.properties["path"]

        main_outputs = [o for o in c.findall("outputs/output") if not self.is_branch(kind, o)]
        for o in c.findall("outputs/output"):
            if self.is_branch(kind, o):
                oprops = {p.get("name", ""): (p.text or "") for p in o.findall("properties/property")}
                default = (o.get("isDefaultOut") or "").lower() == "true"
                if (o.get("isErrorOut") or "").lower() == "true":
                    cond = "error rows"
                elif kind == "lookup":
                    cond = "no match" if "No Match" in o.get("name", "") else "match"
                else:
                    cond = None if default else (oprops.get("FriendlyExpression") or oprops.get("Expression"))
                t.branches.append(Branch(name=f"{name}.{o.get('name')}", condition=cond, is_default=default))

        if kind in ("table_source", "file_source"):
            for o in main_outputs:
                t.columns += [self.column(oc) for oc in o.findall("outputColumns/outputColumn")]
        elif kind in ("derived_column", "data_conversion"):
            for o in main_outputs + [o for o in c.findall("outputs/output") if kind == "derived_column" and o.get("isErrorOut") != "true"]:
                for oc in o.findall("outputColumns/outputColumn"):
                    cprops = {p.get("name", ""): (p.text or "") for p in oc.findall("properties/property")}
                    m = self.column(oc)
                    if kind == "derived_column":
                        m.expression = cprops.get("FriendlyExpression") or cprops.get("Expression")
                        m.source_columns = [f"{first}.{x}" for x in COLUMN_REF.findall(m.expression or "")
                                            if first and not x.startswith("User::") and not x.startswith("System::")]
                    else:
                        src = lineage.get(cprops.get("SourceInputColumnLineageID", "").strip("#{}"), "")
                        m.expression = f"CAST([{src}] AS {m.data_type})" if src else None
                        m.source_columns = [f"{first}.{src}"] if src and first else []
                    if m.column not in {x.column for x in t.columns}:
                        t.columns.append(m)
        elif kind == "lookup":
            match = [(ic.get("cachedName", "?"), next((p.text for p in ic.findall("properties/property")
                                                       if p.get("name") == "JoinToReferenceColumn"), "?"))
                     for ic in c.findall("inputs/input/inputColumns/inputColumn")]
            returns = []
            for oc in c.findall("outputs/output/outputColumns/outputColumn"):
                m = self.column(oc)
                ref = next((p.text for p in oc.findall("properties/property") if p.get("name") == "CopyFromReferenceColumn"),
                           m.column)
                returns.append(ref)
                table = self.lookup_table(props.get("SqlCommand", ""), datastore)
                m.expression = f"lookup {table.qualified if table else '?'}.{ref}"
                m.source_columns = [f"{table.qualified}.{ref}"] if table else []
                t.columns.append(m)
            table = self.lookup_table(props.get("SqlCommand", ""), datastore)
            t.lookups.append(LookupRef(function="lookup_transform", table=table, return_columns=returns,
                                       match_columns=[(ref_col, f"{first}.{col}") for col, ref_col in match],
                                       default_values=["NULL"] * len(returns), raw=props.get("SqlCommand") or None))
            if table is None:
                self.estate.unparsed.append(f"{df_name}: lookup {name} reference table not found in its query")
        elif kind == "merge":
            for o in main_outputs:
                for oc in o.findall("outputColumns/outputColumn"):
                    m = self.column(oc)
                    m.expression = f"[{m.column}]"
                    m.source_columns = [f"{i}.{m.column}" for i in t.inputs]
                    t.columns.append(m)
        elif kind == "join":
            refs = {ic.get("refId", ""): (i.get("name", ""), ic.get("cachedName", ""))
                    for i in c.findall("inputs/input") for ic in i.findall("inputColumns/inputColumn")}
            side = {"Left": t.inputs[0] if t.inputs else "?", "Right": t.inputs[1] if len(t.inputs) > 1 else "?"}
            for o in main_outputs:
                for oc in o.findall("outputColumns/outputColumn"):
                    m = self.column(oc)
                    cprops = {p.get("name", ""): (p.text or "") for p in oc.findall("properties/property")}
                    in_name, cached = refs.get(cprops.get("InputColumnID", ""), ("", m.column))
                    src = side["Left"] if "Left" in in_name else side["Right"]
                    m.expression, m.source_columns = f"{src}.{cached}", [f"{src}.{cached}"]
                    t.columns.append(m)
            t.options["_join_type"] = JOIN_TYPES.get(props.get("JoinType", "2"), "inner")
            t.options["_key_count"] = props.get("NumKeyColumns", "1")
        elif kind == "aggregate":
            for o in main_outputs:
                for oc in o.findall("outputColumns/outputColumn"):
                    m = self.column(oc)
                    cprops = {p.get("name", ""): (p.text or "") for p in oc.findall("properties/property")}
                    fn = AGGREGATIONS.get(cprops.get("AggregationType", "0"))
                    src = cprops.get("SourceColumn", m.column)
                    if fn is None:
                        m.expression = f"[{src}]"
                        t.group_by.append(f"[{src}]")
                    elif cprops.get("AggregationType") == "2":
                        m.expression = "COUNT(*)"
                    else:
                        m.expression = f"{fn}([{src}])"
                    m.source_columns = [f"{first}.{src}"] if first and cprops.get("AggregationType") != "2" else []
                    t.columns.append(m)
        elif kind == "sort":
            keys = []
            for ic in c.findall("inputs/input/inputColumns/inputColumn"):
                pos = next((p.text for p in ic.findall("properties/property") if p.get("name") == "NewSortKeyPosition"), "0")
                if pos and int(pos) != 0:
                    keys.append((abs(int(pos)), SortKey(expression=ic.get("cachedName", "?"), direction="desc" if int(pos) < 0 else "asc")))
            t.order_by = [k for _, k in sorted(keys, key=lambda x: x[0])]
        elif kind == "scd":
            self.estate.unparsed.append(f"{df_name}: Slowly Changing Dimension wizard {name} kept without its rules")
        return t

    def finish_join(self, t: Transform, sorts: dict[str, Transform]) -> None:
        join_type = t.options.pop("_join_type", "inner")
        n = int(t.options.pop("_key_count", "1") or 1)
        if len(t.inputs) != 2:
            self.estate.unparsed.append(f"merge join {t.name} needs two inputs")
            return
        left, right = t.inputs
        lk = [k.expression for k in (sorts[left].order_by if left in sorts else [])][:n]
        rk = [k.expression for k in (sorts[right].order_by if right in sorts else [])][:n]
        cond = " AND ".join(f"{left}.{x} = {right}.{y}" for x, y in zip(lk, rk)) or None
        if cond is None:
            self.estate.unparsed.append(f"merge join {t.name}: join keys not found on sorted inputs")
        t.joins = [Join(left=left, right=right, join_type=join_type, condition=cond)]

    def column(self, oc: ET.Element) -> ColumnMapping:
        dt = oc.get("dataType", "")
        if dt in ("numeric", "decimal", "cy"):
            typ = f"decimal({oc.get('precision') or 19},{oc.get('scale') or (4 if dt == 'cy' else 0)})"
        elif dt in ("str", "wstr", "text", "ntext"):
            typ = f"varchar({oc.get('length')})" if oc.get("length") else "varchar"
        else:
            typ = DATA_TYPES.get(dt, dt or None)
        return ColumnMapping(column=oc.get("name", "?"), data_type=typ)

    def table(self, open_rowset: str, datastore: Optional[str]) -> TableRef:
        parts = [p.strip("[]\" ") for p in re.split(r"\.(?![^\[]*\])", open_rowset.strip())]
        if len(parts) >= 2:
            return TableRef(datastore=datastore, owner=parts[-2], table=parts[-1])
        return TableRef(datastore=datastore, table=parts[-1])

    def lookup_table(self, sql: str, datastore: Optional[str]) -> Optional[TableRef]:
        m = re.search(r"\bfrom\s+([\w\[\]\.\"]+)", sql, re.I)
        return self.table(m.group(1), datastore) if m else None

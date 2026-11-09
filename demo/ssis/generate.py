"""Generate the SYNTHETIC SSIS demo: Halden Outfitters (a fictional company).

Writes exports/*.dtsx in the SSIS 2012+ package format (PackageFormatVersion 8), shaped like
real packages but unverified against SSIS itself. Run: .venv/bin/python demo/ssis/generate.py
"""
from pathlib import Path
from xml.etree import ElementTree as ET

DTS = "www.microsoft.com/SqlServer/Dts"
SQLTASK = "www.microsoft.com/sqlserver/dts/tasks/sqltask"
ET.register_namespace("DTS", DTS)
ET.register_namespace("SQLTask", SQLTASK)
OUT = Path(__file__).parent / "exports"


def d(name: str) -> str:
    return f"{{{DTS}}}{name}"


def el(tag, attrs=None, children=(), text=None, ns=DTS):
    e = ET.Element(f"{{{ns}}}{tag}" if ns else tag,
                   {(d(k) if ns == DTS and ":" not in k and not k.startswith("{") else k): str(v)
                    for k, v in (attrs or {}).items() if v is not None})
    for c in children:
        e.append(c)
    if text is not None:
        e.text = text
    return e


def plain(tag, attrs=None, children=(), text=None):
    """Pipeline XML has no namespace."""
    return el(tag, attrs, children, text, ns=None)


# ---------- package parts ----------

def package(name, conns, variables, executables, constraints=(), handlers=()):
    kids = [el("Property", {"Name": "PackageFormatVersion"}, text="8"),
            el("ConnectionManagers", children=conns), el("Variables", children=variables),
            el("Executables", children=executables)]
    if constraints:
        kids.append(el("PrecedenceConstraints", children=constraints))
    if handlers:
        kids.append(el("EventHandlers", children=handlers))
    return el("Executable", {"refId": "Package", "ObjectName": name, "ExecutableType": "Microsoft.Package",
                             "CreationName": "Microsoft.Package"}, kids)


def conn(name, kind, cs):
    return el("ConnectionManager", {"refId": f"Package.ConnectionManagers[{name}]", "ObjectName": name,
                                    "CreationName": kind},
              [el("ObjectData", children=[el("ConnectionManager", {"ConnectionString": cs})])])


def var(name, value, dtype=8, expression=None):
    return el("Variable", {"Namespace": "User", "ObjectName": name, "EvaluateAsExpression": "True" if expression else None,
                           "Expression": expression}, [el("VariableValue", {"DataType": dtype}, text=value)])


def exe(path, typ, children=(), **attrs):
    return el("Executable", {"refId": path, "ObjectName": path.rsplit("\\", 1)[-1], "ExecutableType": typ, **attrs},
              list(children))


def container(path, typ, executables, constraints=(), extra=(), **attrs):
    kids = list(extra) + [el("Executables", children=executables)]
    if constraints:
        kids.append(el("PrecedenceConstraints", children=constraints))
    return exe(path, typ, kids, **attrs)


def sql_task(path, sql, connection="DWH"):
    data = ET.Element(f"{{{SQLTASK}}}SqlTaskData", {f"{{{SQLTASK}}}Connection": f"Package.ConnectionManagers[{connection}]",
                                                    f"{{{SQLTASK}}}SqlStatementSource": sql})
    return exe(path, "Microsoft.ExecuteSQLTask", [el("ObjectData", children=[data])])


def exec_package(path, package_file):
    return exe(path, "Microsoft.ExecutePackageTask",
               [el("ObjectData", children=[plain("ExecutePackageTask", children=[plain("PackageName", text=package_file)])])])


def pc(frm, to, value=0, expression=None):
    """value: 0 success, 1 failure, 2 completion; EvalOp 3 = expression and constraint."""
    return el("PrecedenceConstraint", {"From": frm, "To": to, "Value": value, "EvalOp": 3 if expression else 2,
                                       "Expression": expression, "LogicalAnd": "True"})


def handler(event, executables):
    return el("EventHandler", {"EventName": event, "refId": f"Package.EventHandlers[{event}]"},
              [el("Executables", children=executables)])


# ---------- data flow ----------

def props(**kw):
    return plain("properties", children=[plain("property", {"name": k}, text=str(v)) for k, v in kw.items()])


def col(comp, output, name, dtype, length=None, precision=None, scale=None, **properties):
    lineage = f"{comp}.Outputs[{output}].Columns[{name}]"
    kids = [props(**properties)] if properties else []
    return plain("outputColumn", {"refId": lineage, "name": name, "lineageId": lineage, "dataType": dtype,
                                  "length": length, "precision": precision, "scale": scale}, kids)


def output(comp, name, columns=(), error=False, default=False, **properties):
    kids = [plain("outputColumns", children=list(columns))]
    if properties:
        kids.insert(0, props(**properties))
    return plain("output", {"refId": f"{comp}.Outputs[{name}]", "name": name,
                            "isErrorOut": "true" if error else None, "isDefaultOut": "true" if default else None}, kids)


def inp(comp, name="Input", columns=()):
    return plain("input", {"refId": f"{comp}.Inputs[{name}]", "name": name},
                 [plain("inputColumns", children=list(columns))] if columns else [])


def in_col(comp, input_name, cached, **properties):
    return plain("inputColumn", {"refId": f"{comp}.Inputs[{input_name}].Columns[{cached}]", "cachedName": cached},
                 [props(**properties)] if properties else [])


def component(df, name, cls, inputs=(), outputs=(), connection=None, **properties):
    ref = f"{df}\\{name}"
    kids = [props(**properties)] if properties else []
    if connection:
        kids.append(plain("connections", children=[plain("connection", {"name": "Connection",
                                                                          "connectionManagerID": f"Package.ConnectionManagers[{connection}]"})]))
    kids += [plain("inputs", children=[i(ref) for i in inputs]), plain("outputs", children=[o(ref) for o in outputs])]
    return plain("component", {"refId": ref, "name": name, "componentClassID": cls}, kids)


def path(df, frm, out, to, input_name="Input"):
    return plain("path", {"refId": f"{df}.Paths[{frm}.{out}]", "startId": f"{df}\\{frm}.Outputs[{out}]",
                          "endId": f"{df}\\{to}.Inputs[{input_name}]"})


def dataflow(df, components, paths):
    pipeline = plain("pipeline", {"version": "1"}, [plain("components", children=components),
                                                    plain("paths", children=paths)])
    return exe(df, "Microsoft.Pipeline", [el("ObjectData", children=[pipeline])])


def write(name, root):
    ET.indent(root, space="  ")
    text = ET.tostring(root, encoding="unicode")
    (OUT / f"{name}.dtsx").write_text('<?xml version="1.0"?>\n<!-- SYNTHETIC SSIS package for development only. '
                                      'Halden Outfitters is a fictional company. -->\n' + text + "\n")


# ---------- MASTER_NIGHTLY ----------

def master():
    P = "Package"
    return package("MASTER_NIGHTLY",
                   [conn("DWH", "OLEDB", "Data Source=sql-dwh01;Initial Catalog=HaldenDWH;Provider=SQLNCLI11.1;"
                                         "User ID=etl_user;Password=Example-Not-A-Real-Secret1;")],
                   [var("RunId", "0", dtype=3)],
                   [sql_task(f"{P}\\SQL Log Start", "INSERT INTO ctl.RunLog (Package, StartedAt) VALUES ('MASTER_NIGHTLY', GETDATE())"),
                    container(f"{P}\\SEQ Load", "STOCK:SEQUENCE", [
                        exec_package(f"{P}\\SEQ Load\\EPT Sales", "PKG_SALES.dtsx"),
                        exec_package(f"{P}\\SEQ Load\\EPT Finance", "PKG_FINANCE.dtsx"),
                    ]),  # no constraints inside: both child packages run in parallel
                    sql_task(f"{P}\\SQL Log End", "UPDATE ctl.RunLog SET EndedAt = GETDATE() WHERE Package = 'MASTER_NIGHTLY'"),
                    exe(f"{P}\\MAIL Failure", "Microsoft.SendMailTask")],
                   [pc(f"{P}\\SQL Log Start", f"{P}\\SEQ Load"),
                    pc(f"{P}\\SEQ Load", f"{P}\\SQL Log End"),
                    pc(f"{P}\\SEQ Load", f"{P}\\MAIL Failure", value=1)],
                   [handler("OnError", [sql_task("Package.EventHandlers[OnError]\\SQL Log Error",
                                                  "UPDATE ctl.RunLog SET Failed = 1 WHERE Package = 'MASTER_NIGHTLY'")])])


# ---------- PKG_SALES ----------

def sales():
    P = "Package"
    DF = f"{P}\\DFT Sales Lines"
    sales_df = dataflow(DF, [
        component(DF, "OLE_SRC Order Lines", "Microsoft.OLEDBSource", outputs=[lambda r: output(r, "OLE DB Source Output", [
            col(r, "OLE DB Source Output", "OrderID", "i4"), col(r, "OLE DB Source Output", "LineNo", "i4"),
            col(r, "OLE DB Source Output", "OrderDate", "dbDate"), col(r, "OLE DB Source Output", "CustomerID", "wstr", length=10),
            col(r, "OLE DB Source Output", "Qty", "numeric", precision=13, scale=3),
            col(r, "OLE DB Source Output", "UnitPrice", "numeric", precision=15, scale=2),
            col(r, "OLE DB Source Output", "Channel", "wstr", length=10)])],
            connection="ERP", AccessMode=2,
            SqlCommand="SELECT OrderID, LineNo, OrderDate, CustomerID, Qty, UnitPrice, Channel FROM sales.OrderLines WHERE ModifiedDate >= ?"),
        component(DF, "DER Amounts", "Microsoft.DerivedColumn", inputs=[lambda r: inp(r, "Derived Column Input")],
                  outputs=[lambda r: output(r, "Derived Column Output", [
                      col(r, "Derived Column Output", "LineAmount", "numeric", precision=15, scale=2, FriendlyExpression="[Qty] * [UnitPrice]"),
                      col(r, "Derived Column Output", "LoadDate", "dbDate", FriendlyExpression="(DT_DBDATE)@[User::LoadDate]")]),
                      lambda r: output(r, "Derived Column Error Output", error=True)]),
        component(DF, "LKP Customer", "Microsoft.Lookup", connection="DWH", NoMatchBehavior=1,
                  SqlCommand="SELECT CustomerKey, CustomerID FROM dim.Customer",
                  inputs=[lambda r: inp(r, "Lookup Input", [in_col(r, "Lookup Input", "CustomerID", JoinToReferenceColumn="CustomerID")])],
                  outputs=[lambda r: output(r, "Lookup Match Output", [
                      col(r, "Lookup Match Output", "CustomerKey", "i4", CopyFromReferenceColumn="CustomerKey")]),
                      lambda r: output(r, "Lookup No Match Output")]),
        component(DF, "CSPL Channel", "Microsoft.ConditionalSplit", inputs=[lambda r: inp(r, "Conditional Split Input")],
                  outputs=[lambda r: output(r, "Store", FriendlyExpression='[Channel] == "STORE"'),
                           lambda r: output(r, "Web", FriendlyExpression='[Channel] == "WEB"'),
                           lambda r: output(r, "Other", default=True)]),
        component(DF, "DER Web Fee", "Microsoft.DerivedColumn", inputs=[lambda r: inp(r, "Derived Column Input")],
                  outputs=[lambda r: output(r, "Derived Column Output", [
                      col(r, "Derived Column Output", "LineAmount", "numeric", precision=15, scale=2,
                          FriendlyExpression="[LineAmount] * 1.02")])]),
        component(DF, "UALL Channels", "Microsoft.UnionAll",
                  inputs=[lambda r: inp(r, "Union All Input 1"), lambda r: inp(r, "Union All Input 2"),
                          lambda r: inp(r, "Union All Input 3")],
                  outputs=[lambda r: output(r, "Union All Output 1", [
                      col(r, "Union All Output 1", n, t) for n, t in (("OrderID", "i4"), ("LineNo", "i4"), ("OrderDate", "dbDate"),
                                                                     ("CustomerKey", "i4"), ("LineAmount", "numeric"), ("LoadDate", "dbDate"))])]),
        component(DF, "OLE_DST Sales Lines", "Microsoft.OLEDBDestination", inputs=[lambda r: inp(r, "OLE DB Destination Input")],
                  connection="DWH", AccessMode=3, OpenRowset="[stg].[SalesLines]"),
        component(DF, "OLE_DST Unknown Customers", "Microsoft.OLEDBDestination",
                  inputs=[lambda r: inp(r, "OLE DB Destination Input")], connection="DWH", AccessMode=3,
                  OpenRowset="[stg].[SalesLinesUnknownCustomer]"),
    ], [
        path(DF, "OLE_SRC Order Lines", "OLE DB Source Output", "DER Amounts", "Derived Column Input"),
        path(DF, "DER Amounts", "Derived Column Output", "LKP Customer", "Lookup Input"),
        path(DF, "LKP Customer", "Lookup Match Output", "CSPL Channel", "Conditional Split Input"),
        path(DF, "LKP Customer", "Lookup No Match Output", "OLE_DST Unknown Customers", "OLE DB Destination Input"),
        path(DF, "CSPL Channel", "Store", "UALL Channels", "Union All Input 1"),
        path(DF, "CSPL Channel", "Web", "DER Web Fee", "Derived Column Input"),
        path(DF, "DER Web Fee", "Derived Column Output", "UALL Channels", "Union All Input 2"),
        path(DF, "CSPL Channel", "Other", "UALL Channels", "Union All Input 3"),
        path(DF, "UALL Channels", "Union All Output 1", "OLE_DST Sales Lines", "OLE DB Destination Input"),
    ])

    POSDF = f"{P}\\FEL POS Files\\DFT Load POS File"
    pos_df = dataflow(POSDF, [
        component(POSDF, "FF_SRC POS File", "Microsoft.FlatFileSource", connection="POS_FILE",
                  outputs=[lambda r: output(r, "Flat File Source Output", [
                      col(r, "Flat File Source Output", "StoreCode", "str", length=4),
                      col(r, "Flat File Source Output", "Sku", "str", length=18),
                      col(r, "Flat File Source Output", "Qty", "str", length=12)])]),
        component(POSDF, "DCNV Types", "Microsoft.DataConvert", inputs=[lambda r: inp(r, "Data Conversion Input")],
                  outputs=[lambda r: output(r, "Data Conversion Output", [
                      col(r, "Data Conversion Output", "QtyNumeric", "numeric", precision=13, scale=3,
                          SourceInputColumnLineageID=f"#{{{POSDF}\\FF_SRC POS File.Outputs[Flat File Source Output].Columns[Qty]}}")])]),
        component(POSDF, "OLE_DST POS Lines", "Microsoft.OLEDBDestination", inputs=[lambda r: inp(r, "OLE DB Destination Input")],
                  connection="DWH", AccessMode=3, OpenRowset="[stg].[PosLines]"),
    ], [path(POSDF, "FF_SRC POS File", "Flat File Source Output", "DCNV Types", "Data Conversion Input"),
        path(POSDF, "DCNV Types", "Data Conversion Output", "OLE_DST POS Lines", "OLE DB Destination Input")])
    foreach = container(f"{P}\\FEL POS Files", "STOCK:FOREACHLOOP", [pos_df], extra=[
        el("ForEachEnumerator", {"ObjectName": "Foreach File Enumerator", "CreationName": "Microsoft.ForEachFileEnumerator"},
           [el("ObjectData", children=[plain("ForEachFileEnumeratorProperties", children=[
               plain("FEFEProperty", {"Folder": "\\\\fileshare\\pos\\inbound"}), plain("FEFEProperty", {"FileSpec": "*.csv"})])])])])

    SUMDF = f"{P}\\DFT Sales Summary"
    summary = dataflow(SUMDF, [
        component(SUMDF, "OLE_SRC Staged Lines", "Microsoft.OLEDBSource", connection="DWH", AccessMode=0,
                  OpenRowset="[stg].[SalesLines]",
                  outputs=[lambda r: output(r, "OLE DB Source Output", [
                      col(r, "OLE DB Source Output", "OrderDate", "dbDate"), col(r, "OLE DB Source Output", "CustomerKey", "i4"),
                      col(r, "OLE DB Source Output", "LineAmount", "numeric", precision=15, scale=2)])]),
        component(SUMDF, "SRT Customer Date", "Microsoft.Sort", inputs=[lambda r: inp(r, "Sort Input", [
            in_col(r, "Sort Input", "CustomerKey", NewSortKeyPosition=1), in_col(r, "Sort Input", "OrderDate", NewSortKeyPosition=-2)])],
            outputs=[lambda r: output(r, "Sort Output")], EliminateDuplicates="false"),
        component(SUMDF, "AGG Daily", "Microsoft.Aggregate", inputs=[lambda r: inp(r, "Aggregate Input 1")],
                  outputs=[lambda r: output(r, "Aggregate Output 1", [
                      col(r, "Aggregate Output 1", "OrderDate", "dbDate", AggregationType=0, SourceColumn="OrderDate"),
                      col(r, "Aggregate Output 1", "CustomerKey", "i4", AggregationType=0, SourceColumn="CustomerKey"),
                      col(r, "Aggregate Output 1", "TotalAmount", "numeric", precision=17, scale=2, AggregationType=4,
                          SourceColumn="LineAmount"),
                      col(r, "Aggregate Output 1", "Lines", "i8", AggregationType=2)])]),
        component(SUMDF, "OLE_DST Daily Sales", "Microsoft.OLEDBDestination", inputs=[lambda r: inp(r, "OLE DB Destination Input")],
                  connection="DWH", AccessMode=3, OpenRowset="[mart].[DailySales]"),
    ], [path(SUMDF, "OLE_SRC Staged Lines", "OLE DB Source Output", "SRT Customer Date", "Sort Input"),
        path(SUMDF, "SRT Customer Date", "Sort Output", "AGG Daily", "Aggregate Input 1"),
        path(SUMDF, "AGG Daily", "Aggregate Output 1", "OLE_DST Daily Sales", "OLE DB Destination Input")])

    return package("PKG_SALES",
                   [conn("ERP", "OLEDB", "Data Source=sql-erp01;Initial Catalog=HaldenERP;Provider=SQLNCLI11.1;Integrated Security=SSPI;"),
                    conn("DWH", "OLEDB", "Data Source=sql-dwh01;Initial Catalog=HaldenDWH;Provider=SQLNCLI11.1;Integrated Security=SSPI;"),
                    conn("POS_FILE", "FLATFILE", "\\\\fileshare\\pos\\inbound\\current.csv")],
                   [var("LoadDate", "2026-10-31", dtype=7, expression="DATEADD(\"day\", -1, GETDATE())"),
                    var("FullLoad", "False", dtype=11)],
                   [sql_task(f"{P}\\SQL Truncate Staging", "TRUNCATE TABLE stg.SalesLines"),
                    sales_df, foreach, summary,
                    exe(f"{P}\\SCR Notify Planners", "Microsoft.ScriptTask")],
                   [pc(f"{P}\\SQL Truncate Staging", DF),
                    pc(f"{P}\\SQL Truncate Staging", f"{P}\\FEL POS Files"),
                    pc(DF, SUMDF), pc(f"{P}\\FEL POS Files", SUMDF),
                    pc(SUMDF, f"{P}\\SCR Notify Planners", value=2)])


# ---------- PKG_FINANCE ----------

def finance():
    P = "Package"
    DF = f"{P}\\DFT GL Join"
    gl = dataflow(DF, [
        component(DF, "OLE_SRC GL Header", "Microsoft.OLEDBSource", connection="ERP", AccessMode=0, OpenRowset="[fin].[GLHeader]",
                  outputs=[lambda r: output(r, "OLE DB Source Output", [
                      col(r, "OLE DB Source Output", "DocNo", "wstr", length=10), col(r, "OLE DB Source Output", "PostingDate", "dbDate")])]),
        component(DF, "OLE_SRC GL Line", "Microsoft.OLEDBSource", connection="ERP", AccessMode=0, OpenRowset="[fin].[GLLine]",
                  outputs=[lambda r: output(r, "OLE DB Source Output", [
                      col(r, "OLE DB Source Output", "DocNo", "wstr", length=10), col(r, "OLE DB Source Output", "Account", "wstr", length=10),
                      col(r, "OLE DB Source Output", "Amount", "numeric", precision=15, scale=2)])]),
        component(DF, "SRT Header", "Microsoft.Sort", inputs=[lambda r: inp(r, "Sort Input", [in_col(r, "Sort Input", "DocNo", NewSortKeyPosition=1)])],
                  outputs=[lambda r: output(r, "Sort Output")]),
        component(DF, "SRT Line", "Microsoft.Sort", inputs=[lambda r: inp(r, "Sort Input", [in_col(r, "Sort Input", "DocNo", NewSortKeyPosition=1)])],
                  outputs=[lambda r: output(r, "Sort Output")]),
        component(DF, "MJN GL", "Microsoft.MergeJoin", JoinType=2, NumKeyColumns=1,
                  inputs=[lambda r: inp(r, "Merge Join Left Input", [in_col(r, "Merge Join Left Input", "DocNo"),
                                                                     in_col(r, "Merge Join Left Input", "PostingDate")]),
                          lambda r: inp(r, "Merge Join Right Input", [in_col(r, "Merge Join Right Input", "Account"),
                                                                      in_col(r, "Merge Join Right Input", "Amount")])],
                  outputs=[lambda r: output(r, "Merge Join Output", [
                      col(r, "Merge Join Output", "DocNo", "wstr", length=10, InputColumnID=f"{r}.Inputs[Merge Join Left Input].Columns[DocNo]"),
                      col(r, "Merge Join Output", "PostingDate", "dbDate", InputColumnID=f"{r}.Inputs[Merge Join Left Input].Columns[PostingDate]"),
                      col(r, "Merge Join Output", "Account", "wstr", length=10, InputColumnID=f"{r}.Inputs[Merge Join Right Input].Columns[Account]"),
                      col(r, "Merge Join Output", "Amount", "numeric", precision=15, scale=2,
                          InputColumnID=f"{r}.Inputs[Merge Join Right Input].Columns[Amount]")])]),
        component(DF, "SC FX Revalue", "Microsoft.ScriptComponentHost", inputs=[lambda r: inp(r, "Input 0")],
                  outputs=[lambda r: output(r, "Output 0")]),
        component(DF, "OLE_DST GL", "Microsoft.OLEDBDestination", inputs=[lambda r: inp(r, "OLE DB Destination Input")],
                  connection="DWH", AccessMode=3, OpenRowset="[mart].[GL]"),
    ], [path(DF, "OLE_SRC GL Header", "OLE DB Source Output", "SRT Header", "Sort Input"),
        path(DF, "OLE_SRC GL Line", "OLE DB Source Output", "SRT Line", "Sort Input"),
        path(DF, "SRT Header", "Sort Output", "MJN GL", "Merge Join Left Input"),
        path(DF, "SRT Line", "Sort Output", "MJN GL", "Merge Join Right Input"),
        path(DF, "MJN GL", "Merge Join Output", "SC FX Revalue", "Input 0"),
        path(DF, "SC FX Revalue", "Output 0", "OLE_DST GL", "OLE DB Destination Input")])
    return package("PKG_FINANCE",
                   [conn("ERP", "OLEDB", "Data Source=sql-erp01;Initial Catalog=HaldenERP;Provider=SQLNCLI11.1;Integrated Security=SSPI;"),
                    conn("DWH", "OLEDB", "Data Source=sql-dwh01;Initial Catalog=HaldenDWH;Provider=SQLNCLI11.1;Integrated Security=SSPI;")],
                   [var("PeriodClose", "False", dtype=11)],
                   [gl, sql_task(f"{P}\\SQL Close Period", "EXEC fin.ClosePeriod")],
                   [pc(DF, f"{P}\\SQL Close Period", expression="@[User::PeriodClose] == TRUE")])


# ---------- PKG_OLD_EXPORT: calls a package that is not in the export ----------

def old_export():
    P = "Package"
    return package("PKG_OLD_EXPORT", [], [],
                   [exec_package(f"{P}\\EPT Archive", "PKG_ARCHIVE.dtsx"),
                    sql_task(f"{P}\\SQL Mark Exported", "UPDATE ctl.Exports SET Done = 1")],
                   [pc(f"{P}\\EPT Archive", f"{P}\\SQL Mark Exported")])


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("MASTER_NIGHTLY", master), ("PKG_SALES", sales), ("PKG_FINANCE", finance),
                        ("PKG_OLD_EXPORT", old_export)):
        write(name, build())
    print(f"wrote {OUT}")

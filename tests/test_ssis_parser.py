"""SSIS (.dtsx) parser on the synthetic Halden Outfitters demo, and the rest of DeltaBound on its IR."""
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from deltabound import cli, graph, report
from deltabound.ir import Estate, ObjectKind
from deltabound.parsers import ssis

ROOT = Path(__file__).parent.parent
EXPORTS = ROOT / "demo" / "ssis" / "exports"


@lru_cache(maxsize=1)
def estate() -> Estate:
    return ssis.parse_files(sorted(EXPORTS.glob("*.dtsx")))


def obj(name: str):
    return estate().objects[name]


def call(parent: str, target_suffix: str):
    return next(c for c in obj(parent).calls if c.target.endswith(target_suffix))


def tr(df: str, name: str):
    return next(t for t in obj(df).transforms if t.name == name)


# ---------- control flow ----------

def test_counts_and_kinds():
    e = estate()
    assert e.source_tool == "ssis" and e.source_version == "PackageFormatVersion 8"
    counts = {k.value: len(e.by_kind(k)) for k in ObjectKind}
    assert counts == {"job": 4, "workflow": 3, "dataflow": 4, "task": 8}
    assert {o.name for o in e.by_kind(ObjectKind.JOB)} == {"MASTER_NIGHTLY", "PKG_SALES", "PKG_FINANCE", "PKG_OLD_EXPORT"}


def test_tasks_keep_their_type_and_sql():
    t = obj("PKG_SALES\\SQL Truncate Staging")
    assert (t.kind, t.task_type, t.scripts) == (ObjectKind.TASK, "sql", ["TRUNCATE TABLE stg.SalesLines"])
    assert obj("PKG_SALES\\SCR Notify Planners").task_type == "script"
    assert obj("MASTER_NIGHTLY\\MAIL Failure").task_type == "mail"


def test_precedence_constraints():
    assert call("PKG_SALES", "DFT Sales Summary").depends_on == [2, 3]  # waits for both branches
    assert call("PKG_SALES", "DFT Sales Lines").trigger == "success"
    assert call("PKG_SALES", "SCR Notify Planners").trigger == "completion"
    assert call("PKG_SALES", "SQL Truncate Staging").depends_on == []  # starts at once
    sales, finance = call("MASTER_NIGHTLY\\SEQ Load", "PKG_SALES"), call("MASTER_NIGHTLY\\SEQ Load", "PKG_FINANCE")
    assert sales.depends_on == finance.depends_on == []  # parallel child packages
    assert sales.target_kind == ObjectKind.JOB


def test_failure_path_expression_branch_and_event_handler():
    mail = call("MASTER_NIGHTLY", "MAIL Failure")
    assert (mail.trigger, mail.control, mail.condition) == ("failure", "error_handler", "on failure of SEQ Load")
    close = call("PKG_FINANCE", "SQL Close Period")
    assert (close.control, close.condition) == ("branch", "@[User::PeriodClose] == TRUE")
    handler = call("MASTER_NIGHTLY", "OnError")
    assert handler.control == "error_handler" and handler.target == "MASTER_NIGHTLY\\OnError"
    assert obj("MASTER_NIGHTLY\\OnError").calls[0].target == "MASTER_NIGHTLY\\OnError\\SQL Log Error"


def test_foreach_loop():
    loop = call("PKG_SALES", "FEL POS Files")
    assert loop.control == "loop"
    assert loop.condition == "foreach Foreach File Enumerator (Folder=\\\\fileshare\\pos\\inbound FileSpec=*.csv)"


def test_variables_with_expressions():
    v = {x.name: x for x in obj("PKG_SALES").variables}
    assert v["User::LoadDate"].expression == 'DATEADD("day", -1, GETDATE())'
    assert v["User::FullLoad"].expression is None and v["User::LoadDate"].scope == "package"


def test_connection_managers_without_secrets():
    ds = estate().datastores
    assert ds["DWH"].properties["Initial Catalog"] == "HaldenDWH" and ds["DWH"].type == "OLEDB"
    assert ds["POS_FILE"].properties == {"path": "\\\\fileshare\\pos\\inbound\\current.csv"}
    dumped = estate().model_dump_json()
    assert "Example-Not-A-Real-Secret1" not in dumped and "Password" not in dumped


def test_missing_package_and_no_dead_packages():
    assert graph.missing_references(estate()) == ["PKG_ARCHIVE"]
    assert graph.unreachable(estate()) == []  # every package can be scheduled on its own


# ---------- data flow ----------

DF = "PKG_SALES\\DFT Sales Lines"


def test_sql_command_source():
    src = tr(DF, "OLE_SRC Order Lines")
    assert src.kind == "table_source" and src.table is None and src.sql.startswith("SELECT OrderID")
    assert [c.column for c in src.columns][:3] == ["OrderID", "LineNo", "OrderDate"]
    assert {c.column: c.data_type for c in src.columns}["UnitPrice"] == "decimal(15,2)"


def test_destination_tables_use_the_connection_manager():
    dst = tr(DF, "OLE_DST Sales Lines")
    assert dst.table.qualified == "DWH.stg.SalesLines" and dst.inputs == ["UALL Channels"]


def test_derived_column_passes_through():
    der = tr(DF, "DER Amounts")
    assert der.passes_through and der.kind == "derived_column"
    line = next(c for c in der.columns if c.column == "LineAmount")
    assert line.expression == "[Qty] * [UnitPrice]"
    assert line.source_columns == ["OLE_SRC Order Lines.Qty", "OLE_SRC Order Lines.UnitPrice"]


def test_conditional_split_branches_and_inputs():
    split = tr(DF, "CSPL Channel")
    assert [(b.name, b.condition, b.is_default) for b in split.branches] == [
        ("CSPL Channel.Store", '[Channel] == "STORE"', False),
        ("CSPL Channel.Web", '[Channel] == "WEB"', False),
        ("CSPL Channel.Other", None, True)]
    assert split.inputs == ["LKP Customer.Lookup Match Output"]
    assert tr(DF, "UALL Channels").inputs == ["CSPL Channel.Store", "DER Web Fee", "CSPL Channel.Other"]  # port order


def test_lookup_with_no_match_output():
    lkp = tr(DF, "LKP Customer")
    lk = lkp.lookups[0]
    assert (lk.table.qualified, lk.return_columns, lk.match_columns) == (
        "DWH.dim.Customer", ["CustomerKey"], [("CustomerID", "DER Amounts.CustomerID")])
    assert [b.name for b in lkp.branches] == ["LKP Customer.Lookup Match Output", "LKP Customer.Lookup No Match Output"]
    assert tr(DF, "OLE_DST Unknown Customers").inputs == ["LKP Customer.Lookup No Match Output"]


def test_sort_aggregate_and_merge_join():
    s = tr("PKG_SALES\\DFT Sales Summary", "SRT Customer Date")
    assert [(k.expression, k.direction) for k in s.order_by] == [("CustomerKey", "asc"), ("OrderDate", "desc")]
    agg = tr("PKG_SALES\\DFT Sales Summary", "AGG Daily")
    assert agg.group_by == ["[OrderDate]", "[CustomerKey]"]
    assert {c.column: c.expression for c in agg.columns} == {
        "OrderDate": "[OrderDate]", "CustomerKey": "[CustomerKey]", "TotalAmount": "SUM([LineAmount])", "Lines": "COUNT(*)"}
    mj = tr("PKG_FINANCE\\DFT GL Join", "MJN GL")
    assert mj.inputs == ["SRT Header", "SRT Line"]
    assert [(j.join_type, j.condition) for j in mj.joins] == [("inner", "SRT Header.DocNo = SRT Line.DocNo")]
    assert {c.column: c.expression for c in mj.columns}["Amount"] == "SRT Line.Amount"


def test_script_component_is_opaque_and_reported():
    sc = tr("PKG_FINANCE\\DFT GL Join", "SC FX Revalue")
    assert sc.kind == "opaque" and sc.raw_tag == "Microsoft.ScriptComponentHost"
    assert estate().unparsed == ["PKG_FINANCE\\DFT GL Join: component <Microsoft.ScriptComponentHost> SC FX Revalue"]


# ---------- lineage on SSIS ----------

def test_lineage_through_split_union_lookup_and_derived_columns():
    ups = set(graph.trace_column(estate(), DF, "DWH.stg.SalesLines.LineAmount"))
    assert {"DER Web Fee.LineAmount", "CSPL Channel.Web.LineAmount", "LKP Customer.Lookup Match Output.LineAmount",
            "DER Amounts.LineAmount", "OLE_SRC Order Lines.Qty", "OLE_SRC Order Lines.UnitPrice"} <= ups


def test_lineage_carries_untouched_columns_and_lookup_returns():
    ups = set(graph.trace_column(estate(), DF, "DWH.stg.SalesLines.CustomerKey"))
    assert "DWH.dim.Customer.CustomerKey" in ups  # from the lookup reference table
    pos = graph.trace_column(estate(), "PKG_SALES\\FEL POS Files\\DFT Load POS File", "DWH.stg.PosLines.StoreCode")
    assert "FF_SRC POS File.StoreCode" in pos  # passed through Data Conversion unchanged


def test_lineage_stops_at_opaque_component():
    assert graph.trace_column(estate(), "PKG_FINANCE\\DFT GL Join", "DWH.mart.GL.Amount") == []


# ---------- the rest of DeltaBound on an SSIS estate ----------

def test_report_works_unchanged():
    md = report.render_markdown(estate())
    assert "| Tasks | 8 |" in md and "| PKG_ARCHIVE | MASTER_NIGHTLY" not in md and "PKG_ARCHIVE" in md


def test_tree_labels():
    lines = graph.tree_lines(estate(), "MASTER_NIGHTLY")
    assert "  task: MASTER_NIGHTLY\\MAIL Failure   [on failure of SEQ Load]" in lines
    assert any("[foreach Foreach File Enumerator" in l for l in lines)


def test_explain_context_and_validate_scaffold_handle_ssis(tmp_path):
    from deltabound.explain import ExplainConfig, Explainer, FakeProvider
    from deltabound.validate import scaffold

    ex = Explainer(estate(), ExplainConfig(provider="fake", cache_dir=str(tmp_path)), provider=FakeProvider())
    exp = ex.job("PKG_FINANCE")
    assert exp.inputs and all(c.citations for c in exp.inputs + exp.outputs)
    text = scaffold(estate(), "PKG_SALES")
    assert 'target = "DWH.stg.SalesLines"' in text


def test_convert_refuses_ssis(tmp_path):
    with pytest.raises(SystemExit, match="ssis conversion is planned"):
        cli.main(["convert", str(ROOT / "demo" / "ssis"), "--job", "PKG_SALES", "--out", str(tmp_path)])


def test_cli_picks_parser_by_file_type_and_refuses_mixes(capsys):
    cli.main(["summary", str(EXPORTS)])
    assert "tasks      8" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="mix SSIS packages"):
        cli.main(["summary", str(EXPORTS / "PKG_SALES.dtsx"), str(ROOT / "samples" / "bods_mini" / "retail_demo.xml")])


# ---------- robustness ----------

PKG = b'<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ExecutableType="Microsoft.Package" DTS:ObjectName="P">%s</DTS:Executable>'


def test_unknown_executable_is_kept_and_reported():
    e = Estate(source_tool="ssis")
    ssis.parse_document("p.dtsx", PKG % b'<DTS:Executables><DTS:Executable DTS:refId="Package\\X" DTS:ObjectName="X" '
                                     b'DTS:ExecutableType="Vendor.CustomTask"/></DTS:Executables>', e)
    assert e.objects["P\\X"].task_type == "other"
    assert e.unparsed == ["p.dtsx: executable <Vendor.CustomTask> X"]


def test_non_package_and_malformed_files():
    e = Estate(source_tool="ssis")
    ssis.parse_document("x.dtsx", b"<root/>", e)
    assert e.unparsed == ["x.dtsx: not an SSIS package (root <root>)"]
    with pytest.raises(ET.ParseError):
        ssis.parse_document("bad.dtsx", b"<DTS:Executable", e)


def test_ir_0_2_1_json_still_loads():
    old = Estate.model_validate_json((ROOT / "samples" / "ir" / "estate_v0.2.1.json").read_text())
    assert old.ir_version == "0.2.1" and old.datastores == {}
    assert old.objects["WF_STAGE_SALES"].calls[0].depends_on is None

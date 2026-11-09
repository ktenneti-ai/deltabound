from pathlib import Path

from deltabound import graph
from deltabound.ir import ObjectKind
from deltabound.parsers import bods

SAMPLE = Path(__file__).parent.parent / "samples" / "bods_mini" / "retail_demo.xml"


def estate():
    return bods.parse_files([SAMPLE])


def test_counts():
    e = estate()
    assert len(e.by_kind(ObjectKind.JOB)) == 2
    assert len(e.by_kind(ObjectKind.WORKFLOW)) == 4
    assert len(e.by_kind(ObjectKind.DATAFLOW)) == 7


def test_conditional_calls_are_labelled():
    wf = estate().objects["WF_STAGE_SALES"]
    conds = {c.target: c.condition for c in wf.calls}
    assert conds["DF_STG_ORDERS_FULL"] == "$G_FULL_LOAD = 'Y'"
    assert conds["DF_STG_ORDERS_DELTA"] == "NOT ($G_FULL_LOAD = 'Y')"
    assert conds["DF_STG_ORDER_ITEMS"] is None


def test_nested_tree():
    lines = graph.tree_lines(estate(), "JOB_SALES_DAILY")
    assert any("DF_DIM_PRODUCT" in l and l.startswith("      ") for l in lines)


def test_dead_code():
    assert graph.unreachable(estate()) == ["DF_STG_RETURNS", "WF_OLD_RETURNS_2019"]


def test_unknown_transform_is_opaque_not_dropped():
    e = estate()
    kinds = {t.name: t.kind for t in e.objects["DF_FACT_SALES"].transforms}
    assert kinds["PY_FX_CONVERT"] == "opaque"
    assert any("DICustomPythonTransform" in u for u in e.unparsed)


def test_column_lineage():
    ups = graph.trace_column(estate(), "DF_STG_ORDERS_DELTA", "DS_DWH.STG.STG_ORDERS.NET_AMOUNT")
    assert "VBAK.NETWR" in ups


def test_shared_dataflow_and_waves():
    e = estate()
    waves = graph.migration_waves(e, size=10)
    names = [n for n, _ in waves[0]]
    assert names[0] == "JOB_CUSTOMER_SYNC"  # simpler job comes first

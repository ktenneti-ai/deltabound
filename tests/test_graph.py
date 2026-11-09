"""Graph analysis: lineage through pass-through transforms, call depth, complexity."""
import time
from functools import lru_cache
from pathlib import Path

from deltabound import graph
from deltabound.ir import CallStep, Estate, EtlObject, ObjectKind
from deltabound.parsers import bods

SAMPLES = Path(__file__).parent.parent / "samples"


@lru_cache(maxsize=1)
def estate() -> Estate:
    return bods.parse_files(sorted((SAMPLES / "bods").glob("*.xml")))


# ---------- 6a: lineage through pass-through transforms ----------

def test_lineage_through_scd_chain():
    ups = graph.trace_column(estate(), "DF_DIM_CUSTOMER", "DS_DWH.MART.DIM_CUSTOMER.CUSTOMER_NAME")
    assert {"KG_CUST.CUSTOMER_NAME", "HP_CUST.CUSTOMER_NAME", "TC_CUST.CUSTOMER_NAME",
            "Q_CUST.CUSTOMER_NAME", "STG_CUSTOMER.CUSTOMER_NAME"} <= set(ups)


def test_lineage_through_case_branches_and_merge():
    ups = set(graph.trace_column(estate(), "DF_FACT_POS_SALES", "DS_DWH.MART.FACT_POS_SALES.AMOUNT"))
    assert {"MRG_CHANNELS.AMOUNT", "Q_STORE.AMOUNT", "Q_KIOSK.AMOUNT", "CH_STORE.AMOUNT",
            "CASE_CHANNEL.AMOUNT", "Q_POS_IN.AMOUNT", "STG_POS_TXN.AMOUNT"} <= ups


def test_lineage_through_lookup_transform():
    e = estate()
    key = graph.trace_column(e, "DF_FACT_LOYALTY_POINTS", "DS_DWH.MART.FACT_LOYALTY_POINTS.MEMBER_KEY")
    assert "DS_DWH.MART.DIM_LOYALTY_MEMBER.MEMBER_KEY" in key
    points = graph.trace_column(e, "DF_FACT_LOYALTY_POINTS", "DS_DWH.MART.FACT_LOYALTY_POINTS.POINTS")
    assert "STG_LOYALTY_TXN.POINTS" in points


def test_lineage_through_lookup_function():
    ups = graph.trace_column(estate(), "DF_FACT_SALES", "DS_DWH.MART.FACT_SALES.PRODUCT_KEY")
    assert {"DS_DWH.MART.DIM_PRODUCT.PRODUCT_KEY", "STG_ORDER_ITEMS.PRODUCT_ID"} <= set(ups)


def test_lineage_stops_at_opaque_transform():
    g = graph.column_lineage(estate(), "DF_ECOM_SCORE")
    assert not any(n.startswith("DS_DWH.MART.ECOM_BASKET_SCORE.") for n in g.nodes)


def test_mini_sample_lineage_unchanged():
    mini = bods.parse_files([SAMPLES / "bods_mini" / "retail_demo.xml"])
    ups = graph.trace_column(mini, "DF_STG_ORDERS_DELTA", "DS_DWH.STG.STG_ORDERS.NET_AMOUNT")
    assert ups == ["Q_ORDERS.NET_AMOUNT", "VBAK.NETWR"]


# ---------- 6b: call depth ----------

def test_call_depth():
    e = estate()
    assert graph.call_depth(e, "JOB_SALES_DAILY") == 6
    assert graph.call_depth(e, "DF_FACT_SALES") == 1
    assert graph.call_depth(e, "NOT_THERE") == 1


def test_complexity_scores_unchanged_on_mini_sample():
    mini = bods.parse_files([SAMPLES / "bods_mini" / "retail_demo.xml"])
    assert graph.complexity(mini, "JOB_CUSTOMER_SYNC") == 13
    assert graph.complexity(mini, "JOB_SALES_DAILY") == 36


def _layered(layers: int, width: int) -> Estate:
    """Every workflow calls every workflow in the next layer: width**layers paths."""
    objs = {
        f"WF_{l}_{w}": EtlObject(
            name=f"WF_{l}_{w}", kind=ObjectKind.WORKFLOW,
            calls=[CallStep(target=f"WF_{l + 1}_{k}") for k in range(width)] if l < layers - 1 else [],
        )
        for l in range(layers) for w in range(width)
    }
    objs["JOB"] = EtlObject(name="JOB", kind=ObjectKind.JOB, calls=[CallStep(target=f"WF_0_{k}") for k in range(width)])
    return Estate(source_tool="synthetic", objects=objs)


def test_complexity_is_fast_on_path_explosion():
    e = _layered(layers=20, width=6)  # 6**20 call paths
    start = time.perf_counter()
    assert graph.call_depth(e, "JOB") == 21
    graph.complexity(e, "JOB")
    assert time.perf_counter() - start < 1.0


def test_call_cycle_does_not_hang():
    e = Estate(source_tool="synthetic", objects={
        "JOB": EtlObject(name="JOB", kind=ObjectKind.JOB, calls=[CallStep(target="WF_A")]),
        "WF_A": EtlObject(name="WF_A", kind=ObjectKind.WORKFLOW, calls=[CallStep(target="WF_B")]),
        "WF_B": EtlObject(name="WF_B", kind=ObjectKind.WORKFLOW, calls=[CallStep(target="WF_A")]),
    })
    assert graph.call_depth(e, "JOB") == 3

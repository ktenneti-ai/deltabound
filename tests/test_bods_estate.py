"""Tests on the multi-file synthetic Fernhill Retail estate in samples/bods/."""
from collections import Counter
from functools import lru_cache
from pathlib import Path

import networkx as nx

from deltabound import graph
from deltabound.ir import Estate, ObjectKind
from deltabound.parsers import bods

ESTATE_DIR = Path(__file__).parent.parent / "samples" / "bods"


@lru_cache(maxsize=1)
def _estate() -> Estate:
    return bods.parse_files(sorted(ESTATE_DIR.glob("*.xml")))


def transform(df: str, name: str):
    return next(t for t in _estate().objects[df].transforms if t.name == name)


# ---------- estate shape ----------

def test_estate_spans_several_files():
    assert len(list(ESTATE_DIR.glob("*.xml"))) >= 5


def test_counts():
    e = _estate()
    assert len(e.by_kind(ObjectKind.JOB)) == 15
    assert len(e.by_kind(ObjectKind.WORKFLOW)) == 30
    assert len(e.by_kind(ObjectKind.DATAFLOW)) == 60


def test_deep_nesting():
    lines = graph.tree_lines(_estate(), "JOB_SALES_DAILY")
    deepest = max(len(l) - len(l.lstrip(" ")) for l in lines) // 2
    assert deepest >= 4
    assert "          dataflow: DF_STG_CUSTOMER" in lines


def test_every_known_transform_kind_is_present():
    tags = {t.raw_tag for o in _estate().objects.values() for t in o.transforms}
    assert set(bods.TRANSFORM_KINDS) <= tags


def test_unknown_transforms_are_opaque_and_reported():
    e = _estate()
    opaque = {t.raw_tag for o in e.objects.values() for t in o.transforms if t.kind == "opaque"}
    assert opaque == {"DIPivot", "DIDataTransfer", "DICustomPythonTransform", "DIXMLPipeline",
                      "DISQLTransform", "DIDateGeneration"}
    for tag in opaque:
        assert any(f"<{tag}>" in u for u in e.unparsed)


def test_global_and_local_variables():
    e = _estate()
    job_vars = {v.name: v.scope for v in e.objects["JOB_SALES_DAILY"].variables}
    assert job_vars["$G_FULL_LOAD"] == "global"
    wf_vars = {v.name: v for v in e.objects["WF_LOAD_FX_RATES"].variables}
    assert wf_vars["$L_RETRY"].scope == "local"
    assert wf_vars["$L_RETRY"].default == "0"


def test_scripts_are_collected():
    scripts = _estate().objects["WF_POS_INTRADAY"].scripts
    assert any("$L_STORE_IDX = $L_STORE_IDX + 1;" in s for s in scripts)


def test_dataflows_shared_by_several_jobs():
    e = _estate()
    g = graph.call_graph(e)
    reached_by = Counter(n for j in e.by_kind(ObjectKind.JOB) for n in nx.descendants(g, j.name))
    assert reached_by["DF_DIM_CUSTOMER"] >= 3
    assert reached_by["DF_STG_FX_RATES"] >= 3


def test_dead_code():
    assert graph.unreachable(_estate()) == [
        "DF_LEGACY_POS_SALES", "DF_STG_RETURNS_2019", "DF_UNUSED_PRICE_COPY",
        "WF_ARCHIVE_2018", "WF_LEGACY_POS_LOAD", "WF_OLD_RETURNS_2019",
    ]


def test_missing_reference():
    assert graph.missing_references(_estate()) == ["WF_PURGE_AUDIT_LOG"]


def test_readme_lineage_example_still_works():
    ups = graph.trace_column(_estate(), "DF_STG_ORDERS_DELTA", "DS_DWH.STG.STG_ORDERS.NET_AMOUNT")
    assert "VBAK.NETWR" in ups

"""Tests on the multi-file synthetic Fernhill Retail estate in demo/sap_bods/exports/."""
from collections import Counter
from functools import lru_cache
from pathlib import Path

import networkx as nx

from deltabound import graph
from deltabound.ir import Estate, ObjectKind
from deltabound.parsers import bods

ESTATE_DIR = Path(__file__).parent.parent / "demo" / "sap_bods" / "exports"


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


# ---------- control flow (IR 0.2.0 CallStep.control) ----------

def test_if_else_labels_and_control():
    calls = {c.target: c for c in _estate().objects["WF_STAGE_SALES"].calls}
    assert calls["DF_STG_ORDERS_FULL"].condition == "$G_FULL_LOAD = 'Y'"
    assert calls["DF_STG_ORDERS_DELTA"].condition == "NOT ($G_FULL_LOAD = 'Y')"
    assert calls["DF_STG_ORDERS_DELTA"].control == "branch"
    assert calls["DF_STG_ORDER_ITEMS"].condition is None and calls["DF_STG_ORDER_ITEMS"].control is None


def test_while_loop_calls():
    calls = {c.target: c for c in _estate().objects["WF_POS_INTRADAY"].calls}
    assert calls["DF_STG_POS_TRANSACTIONS"].control == "loop"
    assert calls["DF_STG_POS_TRANSACTIONS"].condition == "while $L_STORE_IDX <= $L_STORE_COUNT"
    assert calls["DF_FACT_POS_SALES"].control is None


def test_try_block_is_unconditional_and_catch_is_error_handler():
    calls = {c.target: c for c in _estate().objects["WF_SALES_DAILY_MAIN"].calls}
    assert calls["WF_STAGE_SALES"].condition is None and calls["WF_STAGE_SALES"].control is None
    assert calls["DF_CTL_JOB_ERROR"].control == "error_handler"
    assert calls["DF_CTL_JOB_ERROR"].condition == "on error (ALL)"


def test_try_catch_inside_loop_keeps_loop_condition():
    calls = {c.target: c for c in _estate().objects["WF_LOAD_FX_RATES"].calls}
    assert calls["DF_STG_FX_RATES"].control == "loop"
    assert calls["DF_STG_FX_RATES"].condition == "while $L_RETRY < 3 AND $L_DONE = 'N'"


def test_parameters_get_parameter_scope():
    v = {v.name: v for v in _estate().objects["WF_ECOM_ORDERS"].variables}
    assert v["$P_SOURCE_DIR"].scope == "parameter"


def test_tree_shows_error_handler_condition():
    lines = graph.tree_lines(_estate(), "JOB_SALES_DAILY")
    assert "    dataflow: DF_CTL_JOB_ERROR   [on error (ALL)]" in lines
    assert not any("DITryCatch" in l for l in lines)


# ---------- query details ----------

def test_query_joins():
    q = transform("DF_FACT_ECOM_SALES", "Q_ECOM")
    assert [(j.left, j.right, j.join_type) for j in q.joins] == [
        ("STG_ECOM_ORDERS", "STG_ECOM_ORDER_LINES", "inner"),
        ("STG_ECOM_ORDERS", "STG_FX_RATES", "left_outer"),
    ]
    assert q.joins[0].condition == "STG_ECOM_ORDERS.WEB_ORDER_ID = STG_ECOM_ORDER_LINES.WEB_ORDER_ID"


def test_group_by_and_order_by():
    q = transform("DF_AGG_SALES_DAILY", "Q_AGG_DAY")
    assert q.group_by == ["FACT_SALES.ORDER_DATE", "FACT_SALES.PRODUCT_KEY"]
    assert [(s.expression, s.direction) for s in q.order_by] == [
        ("FACT_SALES.ORDER_DATE", "asc"), ("sum(FACT_SALES.LINE_AMOUNT)", "desc"),
    ]


def test_case_branches():
    case = transform("DF_FACT_POS_SALES", "CASE_CHANNEL")
    assert [(b.name, b.condition, b.is_default) for b in case.branches] == [
        ("CH_STORE", "Q_POS_IN.CHANNEL = 'STORE'", False),
        ("CH_KIOSK", "Q_POS_IN.CHANNEL = 'KIOSK'", False),
        ("CH_OTHER", None, True),
    ]


def test_merge_keeps_all_inputs():
    assert transform("DF_FACT_POS_SALES", "MRG_CHANNELS").inputs == ["Q_STORE", "Q_KIOSK", "Q_OTHER"]
    assert transform("DF_CALC_REORDER", "MRG_REORDER").inputs == ["Q_CRIT", "Q_LOW"]


def test_tool_specific_attributes_go_to_options():
    assert transform("DF_DIM_CUSTOMER", "TC_CUST").options == {"comparisonMethod": "ROW_BY_ROW_SELECT"}
    assert transform("DF_FACT_LOYALTY_POINTS", "LKP_MEMBER_KEY").options == {}  # cachePolicy -> LookupRef.cache
    tgt = next(t for t in _estate().objects["DF_STG_ORDERS_FULL"].transforms if t.kind == "table_target")
    assert tgt.options["loadMode"] == "truncate"


# ---------- lookups ----------

def test_lookup_ext_call():
    q = transform("DF_FACT_SALES", "Q_JOIN")
    lk = next(l for l in q.lookups if l.output_column == "PRODUCT_KEY")
    assert lk.function == "lookup_ext"
    assert lk.table.qualified == "DS_DWH.MART.DIM_PRODUCT"
    assert lk.return_columns == ["PRODUCT_KEY"]
    assert lk.match_columns == [("PRODUCT_ID", "STG_ORDER_ITEMS.PRODUCT_ID")]
    assert lk.raw.startswith("lookup_ext([DS_DWH.MART.DIM_PRODUCT")
    col = next(c for c in q.columns if c.column == "PRODUCT_KEY")
    assert col.source_columns == ["DS_DWH.MART.DIM_PRODUCT.PRODUCT_KEY", "STG_ORDER_ITEMS.PRODUCT_ID"]


def test_lookup_ext_with_several_match_columns():
    lk = transform("DF_FACT_MARGIN", "Q_MARGIN").lookups[0]
    assert lk.match_columns == [("FROM_CCY", "PRODUCT_COST.CURRENCY"), ("TO_CCY", "'EUR'")]


def test_old_style_lookup_call():
    lk = transform("DF_FACT_STOCK_DAILY", "Q_STOCK").lookups[1]
    assert lk.function == "lookup"
    assert lk.table.qualified == "DS_DWH.MART.DIM_STORE"
    assert lk.return_columns == ["STORE_KEY"]
    assert lk.match_columns == [("STORE_CODE", "STG_STOCK.STORE_CODE")]


def test_lookup_transform():
    lk = transform("DF_FACT_LOYALTY_POINTS", "LKP_MEMBER_KEY").lookups
    assert len(lk) == 1
    assert lk[0].function == "lookup_transform"
    assert lk[0].table.qualified == "DS_DWH.MART.DIM_LOYALTY_MEMBER"
    assert lk[0].return_columns == ["MEMBER_KEY"]
    assert lk[0].match_columns == [("MEMBER_ID", "Q_TXN.MEMBER_ID")]


def test_unresolvable_lookup_is_kept_and_reported():
    q = transform("DF_AGG_MARGIN_BY_CATEGORY", "Q_CAT")
    lk = next(l for l in q.lookups if l.output_column == "MARGIN_USD")
    assert lk.table is None and "$G_FX_TABLE" in lk.raw
    assert any("Q_CAT.MARGIN_USD" in u and "cannot be resolved" in u for u in _estate().unparsed)


def test_lookup_table_names_are_not_mistaken_for_columns():
    sources = {s for o in _estate().objects.values() for t in o.transforms for c in t.columns for s in c.source_columns}
    assert not sources & {"DS_DWH.MART", "MART.DIM_PRODUCT", "MART.DIM_STORE", "DS_DWH.STG", "STG.STG_FX_RATES"}


# ---------- robustness ----------

def test_duplicate_objects_are_reported_not_overwritten():
    mini = Path(__file__).parent.parent / "samples" / "bods_mini" / "retail_demo.xml"
    e = bods.parse_files([mini, mini])
    assert len(e.by_kind(ObjectKind.JOB)) == 2
    dupes = [u for u in e.unparsed if "duplicate" in u]
    assert len(dupes) == 13
    assert any("duplicate job JOB_SALES_DAILY" in u for u in dupes)


def test_ir_0_1_0_json_still_loads():
    old = Path(__file__).parent.parent / "samples" / "ir" / "estate_v0.1.0.json"
    e = Estate.model_validate_json(old.read_text())
    assert e.ir_version == "0.1.0"
    assert e.objects["WF_STAGE_SALES"].calls[0].control is None
    assert e.objects["DF_FACT_SALES"].transforms[0].joins == []
    assert graph.unreachable(e) == ["DF_STG_RETURNS", "WF_OLD_RETURNS_2019"]


def test_export_round_trip():
    e = _estate()
    again = Estate.model_validate_json(e.model_dump_json())
    assert again == e


# ---------- IR 0.2.1: details needed for conversion ----------

def test_lookup_defaults_policy_and_cache():
    lk = next(l for l in transform("DF_FACT_SALES", "Q_JOIN").lookups if l.output_column == "PRODUCT_KEY")
    assert (lk.default_values, lk.policy, lk.cache) == (["-1"], "max", "PRE_LOAD_CACHE")
    old = transform("DF_FACT_STOCK_DAILY", "Q_STOCK").lookups[1]
    assert (old.default_values, old.policy, old.cache) == (["-1"], None, "PRE_LOAD_CACHE")
    cat = next(l for l in transform("DF_AGG_MARGIN_BY_CATEGORY", "Q_CAT").lookups if l.output_column == "CATEGORY")
    assert cat.default_values == ["'UNKNOWN'"]
    tr = transform("DF_FACT_LOYALTY_POINTS", "LKP_MEMBER_KEY").lookups[0]
    assert (tr.cache, tr.default_values, tr.policy) == ("PRE_LOAD_CACHE", ["NULL"], None)


def test_table_comparison_keys():
    tc = transform("DF_DIM_CUSTOMER", "TC_CUST")
    assert tc.key_columns == ["CUSTOMER_ID"]
    assert tc.compare_columns == ["CUSTOMER_NAME", "COUNTRY", "CITY"]
    assert transform("DF_DIM_CALENDAR_HOLIDAYS", "TC_HOL").key_columns == ["HOLIDAY_DATE", "COUNTRY"]
    assert transform("DF_DIM_PRODUCT", "TC_PROD").compare_columns == []


def test_history_and_generated_key():
    hp = transform("DF_DIM_CUSTOMER", "HP_CUST")
    assert (hp.history.valid_from, hp.history.valid_to, hp.history.current_flag, hp.history.current_value) == \
        ("VALID_FROM", "VALID_TO", "IS_CURRENT", "1")
    assert transform("DF_DIM_LOYALTY_MEMBER", "HP_MEMBER").history.current_flag is None
    assert transform("DF_DIM_CUSTOMER", "KG_CUST").generated_key == "CUSTOMER_KEY"
    assert "keyColumn" not in transform("DF_DIM_CUSTOMER", "KG_CUST").options


def test_ir_0_2_0_json_still_loads():
    old = Path(__file__).parent.parent / "samples" / "ir" / "estate_v0.2.0.json"
    e = Estate.model_validate_json(old.read_text())
    assert e.ir_version == "0.2.0"
    t = e.objects["DF_FACT_SALES"].transforms[0]
    assert t.key_columns == [] and t.history is None and t.generated_key is None


def test_transform_names_are_unique_within_each_dataflow():
    for o in _estate().objects.values():
        names = [t.name for t in o.transforms]
        assert len(names) == len(set(names)), o.name
    purge = [(t.name, t.kind) for t in _estate().objects["DF_CTL_PURGE_STAGING"].transforms]
    assert ("STG_POS_TXN", "table_source") in purge and ("STG_POS_TXN_TARGET", "table_target") in purge


def test_script_positions():
    o = _estate().objects
    main = o["WF_SALES_DAILY_MAIN"].script_steps
    assert [(s.index, s.after_call, s.control) for s in main] == [(0, 3, "error_handler"), (1, 4, "error_handler")]
    assert [(s.after_call, s.control) for s in o["WF_JOB_END"].script_steps] == [(1, None)]
    fx = o["WF_LOAD_FX_RATES"].script_steps
    assert [(s.after_call, s.control) for s in fx] == [(1, "loop"), (1, "error_handler"), (2, "branch")]
    assert fx[2].condition == "$L_DONE = 'N'"
    assert all(len(x.scripts) == len(x.script_steps) for x in o.values())
    # Call numbering is unchanged by scripts.
    assert [c.order for c in o["WF_SALES_DAILY_MAIN"].calls] == [1, 2, 3, 4, 5]

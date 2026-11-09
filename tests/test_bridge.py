"""deltabound.bridge (the viewer's Python side), call_tree, parse_document, viewer artifacts."""
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

from deltabound import bridge, cli, graph
from deltabound.ir import Estate
from deltabound.parsers import bods

ROOT = Path(__file__).parent.parent
SAMPLES = ROOT / "samples"
BODS = sorted((SAMPLES / "bods").glob("*.xml"))


def files(*paths: Path) -> list[tuple[str, bytes]]:
    return [(p.name, p.read_bytes()) for p in paths]


def zipped(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def statuses(result: dict) -> dict[str, tuple[str, str]]:
    return {e["file"]: (e["format"], e["status"]) for e in result["load_report"]}


# ---------- loading ----------

def test_load_bods_xml():
    r = bridge.load(files(*BODS))
    assert r["ok"] and r["explanations"] is None
    assert r["analysis"]["counts"] == {"job": 15, "workflow": 30, "dataflow": 60, "transforms": 226, "opaque": 6}
    assert all(e["status"] == "ok" for e in r["load_report"])
    json.dumps(r)  # JSON-serialisable for the browser


def test_bytes_and_paths_parse_the_same():
    e = Estate(source_tool=bods.SOURCE_TOOL)
    for name, data in files(*BODS):
        bods.parse_document(name, data, e)
    assert e == bods.parse_files(BODS)


def test_load_estate_json_including_old_ir_versions():
    current = bods.parse_files(BODS).model_dump_json().encode()
    r = bridge.load([("estate.json", current)])
    assert r["ok"] and r["analysis"]["counts"]["job"] == 15
    old = bridge.load(files(SAMPLES / "ir" / "estate_v0.1.0.json"))
    assert old["ok"] and "IR 0.1.0 file read by IR 0.2.0 code" in old["load_report"][0]["message"]


def test_zip_with_folder_and_junk():
    data = zipped({"export/sales.xml": (SAMPLES / "bods" / "sales.xml").read_bytes(),
                   "export/shared.xml": (SAMPLES / "bods" / "shared.xml").read_bytes(),
                   "__MACOSX/export/._sales.xml": b"junk", "export/.DS_Store": b"junk"})
    r = bridge.load([("estate.zip", data)])
    st = statuses(r)
    assert st["estate.zip"] == ("zip", "ok")
    assert st["estate.zip/export/sales.xml"] == ("bods_xml", "ok")
    assert len(st) == 3 and r["ok"]


def test_nested_zip_limit_and_bad_zip():
    inner = zipped({"a.xml": (SAMPLES / "bods" / "sales.xml").read_bytes()})
    deep = zipped({"l2.zip": zipped({"l3.zip": inner})})
    st = statuses(bridge.load([("deep.zip", deep), ("broken.zip", b"PK\x03\x04garbage")]))
    assert st["deep.zip/l2.zip/l3.zip"] == ("zip", "error")
    assert st["broken.zip"] == ("zip", "error")


def test_bad_files_are_reported_not_fatal():
    r = bridge.load(files(SAMPLES / "bods" / "sales.xml") + [
        ("broken.xml", b"<DataIntegratorExport><DIJob name='X'>"),
        ("other.xml", b"<catalog/>"),
        ("notes.txt", b"hello"),
        ("data.json", b'{"rows": []}'),
        ("bad.json", b"{not json"),
    ])
    st = statuses(r)
    assert r["ok"]
    assert st["sales.xml"] == ("bods_xml", "ok")
    assert st["broken.xml"] == ("bods_xml", "error")  # BODS root tag, broken later
    assert st["other.xml"] == ("unknown", "error")
    assert st["notes.txt"] == ("unknown", "error")
    assert st["data.json"] == ("unknown", "error")
    assert st["bad.json"] == ("unknown", "error")
    msg = next(e["message"] for e in r["load_report"] if e["file"] == "other.xml")
    assert "<catalog>" in msg and "Supported:" in msg


def test_malformed_bods_xml_after_detection():
    # Detected as BODS from its first tag, broken later on.
    r = bridge.load([("half.xml", b"<DataIntegratorExport><DIJob name='J'></DataIntegratorExport>")])
    assert statuses(r)["half.xml"] == ("bods_xml", "error") and not r["ok"]


def test_ssis_and_atl_are_recognised_as_unsupported():
    dtsx = b'<?xml version="1.0"?><DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" />'
    r = bridge.load([("PKG_SALES.dtsx", dtsx), ("repo.atl", b"AL_PROJECT ...")])
    st = statuses(r)
    assert st["PKG_SALES.dtsx"] == ("ssis", "unsupported")
    assert st["repo.atl"] == ("atl", "unsupported")
    assert not r["ok"]


def test_xml_and_estate_json_together_uses_the_xml():
    est = bods.parse_files([SAMPLES / "bods_mini" / "retail_demo.xml"]).model_dump_json().encode()
    r = bridge.load(files(*BODS) + [("estate.json", est)])
    assert statuses(r)["estate.json"] == ("estate_json", "skipped")
    assert r["analysis"]["counts"]["job"] == 15


def test_second_estate_json_is_skipped():
    est = bods.parse_files([SAMPLES / "bods_mini" / "retail_demo.xml"]).model_dump_json().encode()
    r = bridge.load([("a.json", est), ("b.json", est)])
    assert statuses(r)["b.json"] == ("estate_json", "skipped")


def test_explanations_bundle_alone_keeps_current_estate():
    bridge.load(files(*BODS))
    bundle = json.dumps({"format": "deltabound-explanations", "version": 1, "jobs": {}, "dataflows": {}}).encode()
    r = bridge.load([("explanations.json", bundle)])
    assert not r["ok"] and r["explanations"]["format"] == "deltabound-explanations"
    assert bridge.lineage("DF_FACT_SALES")["edges"]  # still the earlier estate


def test_real_explanations_bundle_is_detected(tmp_path):
    from deltabound.explain import ExplainConfig, Explainer, FakeProvider

    e = bods.parse_files(BODS)
    cfg = ExplainConfig(provider="fake", cache_dir=str(tmp_path))
    bundle = Explainer(e, cfg, provider=FakeProvider()).bundle(["JOB_CUSTOMER_SYNC"])
    data = bundle.model_dump_json().encode()
    assert bundle.ir_version  # the bundle carries ir_version, like an estate
    assert bridge.detect("explanations.json", data) == ("explanations_json", "")
    r = bridge.load(files(*BODS) + [("explanations.json", data)])
    assert r["ok"] and list(r["explanations"]["jobs"]) == ["JOB_CUSTOMER_SYNC"]


# ---------- analysis and lineage ----------

def test_analysis_contents():
    a = bridge.load(files(*BODS))["analysis"]
    assert a["jobs"][0] == {"job": "JOB_SALES_DAILY", "score": 77, "dataflows": 15, "depth": 6, "wave": 2}
    assert a["missing"] == {"WF_PURGE_AUDIT_LOG": ["WF_HOUSEKEEPING"]}
    assert "WF_LOAD_FX_RATES" in a["trees"] and "JOB_SALES_DAILY" in a["trees"]
    assert len(a["used_by_jobs"]["DF_DIM_CUSTOMER"]) >= 3
    assert a["coverage"] == 97.3


def test_lineage_owners():
    bridge.load(files(*BODS))
    lin = bridge.lineage("DF_FACT_POS_SALES")
    own = lin["owner"]
    assert own["CH_KIOSK.AMOUNT"] == "transform:CASE_CHANNEL"
    assert own["DS_DWH.MART.FACT_POS_SALES.AMOUNT"] == "transform:FACT_POS_SALES"
    assert own["DS_DWH.MART.DIM_STORE.STORE_KEY"] == "lookup:DS_DWH.MART.DIM_STORE"
    assert own["STG_POS_TXN.AMOUNT"] == "transform:STG_POS_TXN"
    assert all(a in own and b in own for a, b in lin["edges"])
    assert bridge.lineage("NOT_A_DATAFLOW") == {"edges": [], "owner": {}}


def test_call_tree():
    e = bods.parse_files(BODS)
    t = graph.call_tree(e, "WF_SALES_DAILY_MAIN")
    names = [c["name"] for c in t["calls"]]
    assert names == ["WF_JOB_START", "WF_STAGE_SALES", "WF_LOAD_SALES_MART", "DF_CTL_JOB_ERROR", "WF_JOB_END"]
    err = t["calls"][3]
    assert (err["control"], err["condition"]) == ("error_handler", "on error (ALL)")
    housekeeping = graph.call_tree(e, "WF_HOUSEKEEPING")
    assert housekeeping["calls"][-1] == {"name": "WF_PURGE_AUDIT_LOG", "kind": "missing", "condition": None,
                                         "control": None, "order": 3, "calls": []}


def test_call_tree_marks_cycles():
    from deltabound.ir import CallStep, EtlObject, ObjectKind
    e = Estate(source_tool="x", objects={
        "A": EtlObject(name="A", kind=ObjectKind.WORKFLOW, calls=[CallStep(target="B")]),
        "B": EtlObject(name="B", kind=ObjectKind.WORKFLOW, calls=[CallStep(target="A")]),
    })
    t = graph.call_tree(e, "A")
    assert t["calls"][0]["calls"][0] == {"name": "A", "kind": "workflow", "condition": None, "control": None,
                                         "order": 0, "calls": [], "cycle": True}


# ---------- committed viewer artifacts stay current ----------

def test_viewer_schema_is_current():
    committed = (ROOT / "viewer" / "src" / "ir.schema.json").read_text()
    assert committed == cli.schema_json(), "run: cd viewer && npm run gen:types"


def test_schema_marks_all_fields_required():
    schema = json.loads(cli.schema_json())
    t = schema["$defs"]["Transform"]
    assert set(t["required"]) == set(t["properties"])


def test_viewer_fixtures_are_current():
    spec = importlib.util.spec_from_file_location("gen_fixtures", ROOT / "viewer" / "scripts" / "gen_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, text in mod.fixtures().items():
        assert (ROOT / "viewer" / "test" / "fixtures" / name).read_text() == text, \
            "run: .venv/bin/python viewer/scripts/gen_fixtures.py"


@pytest.fixture(autouse=True)
def _reset_bridge_state():
    yield
    bridge._state["estate"] = None

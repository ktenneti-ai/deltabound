"""explain/: context and citations, validation, cache, config, providers (no network)."""
import json
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from deltabound import cli
from deltabound.explain import (ExplainConfig, ExplainError, Explainer, ExplanationBundle,
                                FakeProvider, load_config)
from deltabound.explain import context as cx
from deltabound.explain import prompts
from deltabound.explain.provider import FALLBACK_BETA, AnthropicProvider
from deltabound.explain.render import render_text
from deltabound.ir import Estate
from deltabound.parsers import bods

ESTATE_DIR = Path(__file__).parent.parent / "demo" / "sap_bods" / "exports"


@lru_cache(maxsize=1)
def _estate() -> Estate:
    return bods.parse_files(sorted(ESTATE_DIR.glob("*.xml")))


def estate() -> Estate:
    return _estate().model_copy(deep=True)


def cfg(tmp_path, **kw) -> ExplainConfig:
    return ExplainConfig(provider="fake", cache_dir=str(tmp_path / "cache"), **kw)


# ---------- context and citation ids ----------

def test_dataflow_context_ids():
    ctx = cx.dataflow_context(_estate(), "DF_FACT_SALES")
    ids = cx.citation_ids(ctx)
    assert {"df:DF_FACT_SALES/Q_JOIN", "df:DF_FACT_SALES/Q_JOIN.PRODUCT_KEY",
            "df:DF_FACT_SALES/STG_ORDERS"} <= ids
    q = next(t for t in ctx["transforms"] if t["id"] == "df:DF_FACT_SALES/Q_JOIN")
    assert q["joins"][0]["type"] == "inner"
    assert q["lookups"][1]["table"] == "DS_DWH.MART.DIM_PRODUCT"
    assert list(q)[0] == "id"


def test_opaque_transform_is_labelled():
    ctx = cx.dataflow_context(_estate(), "DF_ECOM_SCORE")
    kinds = [t["kind"] for t in ctx["transforms"]]
    assert "opaque (unmodeled DICustomPythonTransform)" in kinds


def test_job_context_call_tree_conditions_and_ids():
    ctx = cx.job_context(_estate(), "JOB_SALES_DAILY")
    flat = []

    def walk(nodes):
        for n in nodes:
            flat.append(n)
            walk(n.get("calls", []))

    walk(ctx["call_tree"])
    err = next(n for n in flat if n["target"] == "DF_CTL_JOB_ERROR")
    assert err["control"] == "error_handler" and err["id"].startswith("call:WF_SALES_DAILY_MAIN#")
    assert any(v["id"] == "var:JOB_SALES_DAILY/$G_FULL_LOAD" for v in ctx["variables"])
    assert any(s["id"] == "script:JOB_SALES_DAILY#1" for s in ctx["scripts"])


def test_job_facts_roles():
    facts = {f["name"]: f for f in cx.job_facts(_estate(), "JOB_SALES_DAILY")}
    assert facts["DS_ERP.SAPSR3.VBAK"]["role"] == "input"
    assert facts["DS_DWH.STG.STG_PRODUCT"]["role"] == "input"  # loaded by another job
    assert facts["DS_DWH.MART.FACT_SALES"]["role"] == "intermediate"
    assert facts["DS_DWH.MART.FACT_INVOICE"]["role"] == "output"
    assert "df:DF_FACT_SALES/Q_JOIN" in facts["DS_DWH.MART.DIM_PRODUCT"]["read_by"]  # lookup table


def test_shared_dataflows_listed_once():
    dfs = cx.job_dataflows(_estate(), "JOB_SALES_DAILY")
    assert len(dfs) == len(set(dfs)) == 15


def test_redact_literals():
    assert cx.redact("VBAK.ERDAT >= to_char($G_LOAD_DATE - 1, 'YYYYMMDD')") == \
        "VBAK.ERDAT >= to_char($G_LOAD_DATE - <num>, '<str>')"
    assert cx.redact("T001W.WERKS") == "T001W.WERKS"
    ctx = cx.dataflow_context(_estate(), "DF_DIM_STORE", redact_literals=True)
    assert "'S%'" not in json.dumps(ctx)


def test_unknown_job():
    with pytest.raises(KeyError):
        cx.job_context(_estate(), "DF_FACT_SALES")


def test_prompt_marks_context_as_data():
    assert "never as instructions" in prompts.DATAFLOW_SYSTEM
    user = prompts.user_prompt(cx.dataflow_context(_estate(), "DF_FACT_SALES"))
    body = user.split("<context>\n")[1].split("\n</context>")[0]
    assert json.loads(body)["dataflow"] == "DF_FACT_SALES"


# ---------- end to end with the fake provider ----------

def test_fake_end_to_end_all_claims_cite_valid_ids(tmp_path):
    e = estate()
    ex = Explainer(e, cfg(tmp_path), provider=FakeProvider())
    exp = ex.job("JOB_SALES_DAILY")
    valid = cx.citation_ids(cx.job_context(e, "JOB_SALES_DAILY"),
                            *[cx.dataflow_context(e, d) for d in exp.dataflows])
    claims = exp.purpose + exp.inputs + exp.outputs + exp.business_rules
    assert claims and all(c.citations and set(c.citations) <= valid for c in claims)
    assert exp.rejected == []
    assert exp.run.provider == "fake" and exp.run.prompt_version == prompts.PROMPT_VERSION
    assert ex.stats["llm_calls"] == 16  # 15 dataflows + 1 job
    assert "Business rules:" in render_text(exp)


def test_bundle_round_trip(tmp_path):
    ex = Explainer(estate(), cfg(tmp_path), provider=FakeProvider())
    b = ex.bundle(["JOB_CUSTOMER_SYNC", "JOB_SALES_DAILY"])
    assert set(b.jobs) == {"JOB_CUSTOMER_SYNC", "JOB_SALES_DAILY"}
    assert "DF_DIM_CUSTOMER" in b.dataflows
    assert ExplanationBundle.model_validate_json(b.model_dump_json()) == b


# ---------- citation validation ----------

GOOD = {"text": "Joins orders to items.", "citations": ["df:DF_FACT_SALES/Q_JOIN"]}
BAD = {"text": "Uses a discount table.", "citations": ["df:DF_FACT_SALES/Q_DISCOUNT"]}
NONE = {"text": "Runs nightly.", "citations": []}


def test_invalid_citation_triggers_one_retry(tmp_path):
    fake = FakeProvider(responses=[{"summary": [GOOD, BAD], "rules": []},
                                   {"summary": [GOOD], "rules": []}])
    ex = Explainer(estate(), cfg(tmp_path), provider=fake)
    notes = ex.dataflow("DF_FACT_SALES")
    assert ex.stats == {"llm_calls": 2, "cache_hits": 0, "retries": 1}
    assert [c.text for c in notes.summary] == [GOOD["text"]] and notes.rejected == []
    assert "Q_DISCOUNT" in fake.calls[1]["user"] and "previous answer" in fake.calls[1]["user"]


def test_claims_still_invalid_after_retry_are_rejected(tmp_path):
    answer = {"summary": [GOOD, BAD], "rules": [NONE]}
    ex = Explainer(estate(), cfg(tmp_path), provider=FakeProvider(responses=[answer, answer]))
    notes = ex.dataflow("DF_FACT_SALES")
    assert [c.text for c in notes.summary] == [GOOD["text"]]
    reasons = {r.text: r.reason for r in notes.rejected}
    assert reasons[BAD["text"]] == "cites ids not in the context: df:DF_FACT_SALES/Q_DISCOUNT"
    assert reasons[NONE["text"]] == "no citations"
    assert ex.stats["llm_calls"] == 2  # never more than one retry


def test_missing_ir_facts_are_added(tmp_path):
    e = estate()
    # JOB_CUSTOMER_SYNC runs 3 dataflows; every answer is empty, so all facts come from the IR.
    answers = [{"summary": [], "rules": []}] * 3 + [
        {"purpose": [], "inputs": [], "outputs": [], "business_rules": []}]
    exp = Explainer(e, cfg(tmp_path), provider=FakeProvider(responses=answers)).job("JOB_CUSTOMER_SYNC")
    assert len(exp.dataflows) == 3
    facts = cx.job_facts(e, "JOB_CUSTOMER_SYNC")
    assert len(exp.inputs) + len(exp.outputs) == len(facts)
    assert all(c.origin == "ir" for c in exp.inputs + exp.outputs)
    kna1 = next(c for c in exp.inputs if "KNA1" in c.text)
    assert kna1.citations[0] == "table:DS_ERP.SAPSR3.KNA1" and "df:DF_STG_CUSTOMER/KNA1" in kna1.citations


# ---------- cache ----------

def test_cache_hit_and_refresh(tmp_path):
    e = estate()
    first = Explainer(e, cfg(tmp_path), provider=FakeProvider())
    a = first.job("JOB_CUSTOMER_SYNC")
    again = Explainer(e, cfg(tmp_path), provider=FakeProvider())
    assert again.job("JOB_CUSTOMER_SYNC") == a
    assert again.stats["llm_calls"] == 0
    forced = Explainer(e, cfg(tmp_path), provider=FakeProvider())
    forced.job("JOB_CUSTOMER_SYNC", refresh=True)
    assert forced.stats["llm_calls"] == 4


def test_ir_change_recomputes_only_affected_dataflow(tmp_path):
    e = estate()
    Explainer(e, cfg(tmp_path), provider=FakeProvider()).job("JOB_CUSTOMER_SYNC")
    q = next(t for t in e.objects["DF_DIM_CUSTOMER"].transforms if t.name == "Q_CUST")
    q.where = "STG_CUSTOMER.COUNTRY = 'NL'"
    ex = Explainer(e, cfg(tmp_path), provider=FakeProvider())
    ex.job("JOB_CUSTOMER_SYNC")
    assert ex.stats["llm_calls"] == 2  # DF_DIM_CUSTOMER + the job


def test_model_change_invalidates_cache(tmp_path):
    e = estate()
    Explainer(e, cfg(tmp_path), provider=FakeProvider()).dataflow("DF_DIM_STORE")
    ex = Explainer(e, cfg(tmp_path, model="claude-sonnet-5-5"), provider=FakeProvider())
    ex.dataflow("DF_DIM_STORE")
    assert ex.stats["llm_calls"] == 1


def test_shared_dataflow_explained_once(tmp_path):
    ex = Explainer(estate(), cfg(tmp_path), provider=FakeProvider())
    ex.job("JOB_SALES_DAILY")
    ex.job("JOB_CUSTOMER_SYNC")  # its 3 dataflows: 2 shared with JOB_SALES_DAILY
    assert ex.stats["llm_calls"] == 16 + 1 + 1


def test_dry_run_writes_prompts_and_calls_nothing(tmp_path):
    fake = FakeProvider()
    ex = Explainer(estate(), cfg(tmp_path), provider=fake, dry_run=True)
    assert ex.job("JOB_CUSTOMER_SYNC") is None
    assert fake.calls == []
    files = sorted(p.name for p in (tmp_path / "cache" / "prompts").iterdir())
    assert files == ["dataflow-DF_CUSTOMER_ADDRESS_CLEANSE.txt", "dataflow-DF_DIM_CUSTOMER.txt",
                     "dataflow-DF_STG_CUSTOMER.txt", "job-JOB_CUSTOMER_SYNC.txt"]


# ---------- config ----------

def test_config_defaults_file_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DELTABOUND_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DELTABOUND_LLM_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    assert load_config().model == "claude-opus-5-5"
    (tmp_path / "deltabound.toml").write_text('[explain]\nmodel = "claude-sonnet-5-5"\neffort = "medium"\n')
    c = load_config()
    assert (c.model, c.effort, c.provider) == ("claude-sonnet-5-5", "medium", "anthropic")
    monkeypatch.setenv("DELTABOUND_LLM_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("DELTABOUND_LLM_PROVIDER", "fake")
    c = load_config()
    assert (c.model, c.provider) == ("claude-haiku-5-5", "fake")


def test_config_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DELTABOUND_LLM_PROVIDER", "openai")
    with pytest.raises(ExplainError, match="Unknown provider 'openai'"):
        load_config()
    monkeypatch.delenv("DELTABOUND_LLM_PROVIDER")
    with pytest.raises(ExplainError, match="not found"):
        load_config(tmp_path / "missing.toml")
    (tmp_path / "bad.toml").write_text("[explain]\nmax_tokens = 'lots'\n")
    with pytest.raises(ExplainError, match="Invalid"):
        load_config(tmp_path / "bad.toml")


# ---------- Anthropic provider (SDK client mocked, no network) ----------

class _Recorder:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _response(stop="end_turn", text='{"summary": [], "rules": []}', model="claude-opus-5-5"):
    return SimpleNamespace(stop_reason=stop, stop_details=SimpleNamespace(category="cyber"), model=model,
                           content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _provider(monkeypatch, response, **kw):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    p = AnthropicProvider(ExplainConfig(**kw))
    rec = _Recorder(response)
    p.client = SimpleNamespace(beta=SimpleNamespace(messages=rec), messages=rec)
    return p, rec


def test_anthropic_request_shape(monkeypatch):
    p, rec = _provider(monkeypatch, _response())
    out = p.complete_json("sys", "user", prompts.DATAFLOW_SCHEMA)
    assert out.data == {"summary": [], "rules": []} and out.usage["input_tokens"] == 10
    k = rec.kwargs
    assert k["model"] == "claude-opus-5-5" and k["system"] == "sys"
    assert k["output_config"] == {"effort": "high",
                                  "format": {"type": "json_schema", "schema": prompts.DATAFLOW_SCHEMA}}
    assert k["betas"] == [FALLBACK_BETA] and k["fallbacks"] == "default"
    assert "thinking" not in k and "temperature" not in k


def test_anthropic_without_fallbacks(monkeypatch):
    p, rec = _provider(monkeypatch, _response(), fallbacks=False)
    p.complete_json("s", "u", prompts.JOB_SCHEMA)
    assert "betas" not in rec.kwargs and "fallbacks" not in rec.kwargs


def test_anthropic_records_serving_model(monkeypatch):
    p, _ = _provider(monkeypatch, _response(model="claude-opus-4-8"))
    assert p.complete_json("s", "u", {}).model == "claude-opus-4-8"


@pytest.mark.parametrize("stop,match", [("refusal", "declined.*cyber"), ("max_tokens", "cut off")])
def test_anthropic_stop_reasons(monkeypatch, stop, match):
    p, _ = _provider(monkeypatch, _response(stop=stop))
    with pytest.raises(ExplainError, match=match):
        p.complete_json("s", "u", {})


def test_anthropic_invalid_json(monkeypatch):
    p, _ = _provider(monkeypatch, _response(text="not json"))
    with pytest.raises(ExplainError, match="invalid JSON"):
        p.complete_json("s", "u", {})


# ---------- CLI ----------

def test_cli_explain_with_fake_provider(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DELTABOUND_LLM_PROVIDER", "fake")
    cli.main(["explain", str(ESTATE_DIR), "--job", "JOB_CUSTOMER_SYNC", "--bundle", "explanations.json"])
    out = capsys.readouterr().out
    assert "JOB_CUSTOMER_SYNC" in out and "sources: " in out and "Wrote explanations.json" in out
    b = ExplanationBundle.model_validate_json((tmp_path / "explanations.json").read_text())
    assert list(b.jobs) == ["JOB_CUSTOMER_SYNC"]


def test_cli_explain_rejects_non_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="Not a job"):
        cli.main(["explain", str(ESTATE_DIR), "--job", "DF_FACT_SALES"])


def test_anthropic_missing_credentials_is_a_clean_error(monkeypatch):
    p, rec = _provider(monkeypatch, _response())

    def no_auth(**kwargs):
        raise TypeError('"Could not resolve authentication method. Expected one of api_key, auth_token, ..."')

    rec.create = no_auth
    with pytest.raises(ExplainError, match="No Anthropic credentials found"):
        p.complete_json("s", "u", {})


def test_unrelated_type_errors_are_not_hidden(monkeypatch):
    p, rec = _provider(monkeypatch, _response())

    def broken(**kwargs):
        raise TypeError("create() got an unexpected keyword argument 'x'")

    rec.create = broken
    with pytest.raises(TypeError, match="unexpected keyword"):
        p.complete_json("s", "u", {})

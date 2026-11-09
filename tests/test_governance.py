"""API governance: no external call without an approved, priced plan; caps enforced; everything audited."""
import ast
import json
from functools import lru_cache
from pathlib import Path

import pytest

from deltabound import cli
from deltabound.explain import ExplainConfig, ExplainError, Explainer, FakeProvider
from deltabound.explain.config import Limits
from deltabound.explain.governance import RETRY_FACTOR, Plan, approve
from deltabound.parsers import bods

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "deltabound"


@lru_cache(maxsize=1)
def estate():
    return bods.parse_files(sorted((ROOT / "demo" / "sap_bods" / "exports").glob("*.xml")))


class ExternalFake(FakeProvider):
    """Behaves like an outside API (external = True) but answers locally, with token usage."""

    external = True
    name = "anthropic"

    def complete_json(self, system, user, schema):
        out = super().complete_json(system, user, schema)
        out.usage = {"input_tokens": 1000, "output_tokens": 500}
        return out


def cfg(tmp_path, **kw) -> ExplainConfig:
    return ExplainConfig(provider="anthropic", cache_dir=str(tmp_path / "cache"), **kw)


def audit(tmp_path) -> list[dict]:
    path = tmp_path / "cache" / "audit.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


# ---------- the boundary ----------

NETWORK_MODULES = {"anthropic", "openai", "requests", "httpx", "httpx2", "aiohttp", "urllib.request", "http.client",
                   "socket", "smtplib", "ftplib"}


def test_only_the_explain_provider_can_reach_an_outside_api():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n in NETWORK_MODULES or n.split(".")[0] in {"anthropic", "openai", "requests", "httpx", "aiohttp"}:
                    offenders.append((str(path.relative_to(ROOT)), n))
    assert offenders == [("deltabound/explain/provider.py", "anthropic")]


def test_anthropic_provider_is_marked_external():
    from deltabound.explain.provider import AnthropicProvider
    assert AnthropicProvider.external is True and FakeProvider.external is False


# ---------- approval required ----------

def test_no_approval_no_call(tmp_path):
    fake = ExternalFake()
    ex = Explainer(estate(), cfg(tmp_path), provider=fake)
    with pytest.raises(ExplainError, match="need an approved plan"):
        ex.job("JOB_CUSTOMER_SYNC")
    assert fake.calls == []
    assert audit(tmp_path)[-1]["reason"] == "no approved plan"


def test_approved_plan_runs_and_is_audited(tmp_path):
    c = cfg(tmp_path)
    ex = Explainer(estate(), c, provider=ExternalFake())
    plan = ex.plan(["JOB_CUSTOMER_SYNC"])
    ex.approval = approve(plan, "Test Approver")
    exp = ex.job("JOB_CUSTOMER_SYNC")
    assert exp is not None
    events = audit(tmp_path)
    assert events[0]["event"] == "approved" and events[0]["approver"] == "Test Approver"
    calls = [e for e in events if e["event"] == "call"]
    assert len(calls) == 4 and {e["token"] for e in calls} == {plan.token}
    assert calls[0]["purpose"] == "dataflow DF_STG_CUSTOMER" and calls[-1]["purpose"] == "job JOB_CUSTOMER_SYNC"
    assert calls[-1]["run_cost_usd"] == pytest.approx(4 * (1000 * 4 + 500 * 20) / 1e6)


def test_budget_is_enforced_per_call(tmp_path):
    c = cfg(tmp_path)
    fake = ExternalFake()
    ex = Explainer(estate(), c, provider=fake)
    plan = ex.plan(["JOB_CUSTOMER_SYNC"])
    plan.max_calls = 2  # approve less than the run needs
    ex.approval = approve(plan)
    with pytest.raises(ExplainError, match="approved 2 calls already used"):
        ex.job("JOB_CUSTOMER_SYNC")
    assert len(fake.calls) == 2
    assert audit(tmp_path)[-1]["event"] == "refused"


def test_cost_cap_stops_before_the_call_that_could_exceed_it(tmp_path):
    c = cfg(tmp_path)
    fake = ExternalFake()
    ex = Explainer(estate(), c, provider=fake)
    plan = ex.plan(["JOB_CUSTOMER_SYNC"])
    plan.max_cost_usd = 0.30  # one worst-case call is ~0.32 USD (16k output tokens at $20/M)
    ex.approval = approve(plan)
    with pytest.raises(ExplainError, match="could exceed the approved"):
        ex.job("JOB_CUSTOMER_SYNC")
    assert fake.calls == []


def test_cache_hits_need_no_approval(tmp_path):
    c = cfg(tmp_path)
    ex = Explainer(estate(), c, provider=ExternalFake())
    ex.approval = approve(ex.plan(["JOB_CUSTOMER_SYNC"]))
    ex.job("JOB_CUSTOMER_SYNC")
    again = Explainer(estate(), c, provider=ExternalFake())  # no approval
    assert again.plan(["JOB_CUSTOMER_SYNC"]).calls == []
    assert again.job("JOB_CUSTOMER_SYNC") is not None  # served from the cache, nothing sent


# ---------- the plan ----------

def test_plan_contents_and_worst_case(tmp_path):
    plan = Explainer(estate(), cfg(tmp_path), provider=ExternalFake()).plan(["JOB_CUSTOMER_SYNC", "JOB_SALES_DAILY"])
    purposes = [c.purpose for c in plan.calls]
    assert purposes.count("dataflow DF_DIM_CUSTOMER") == 1  # shared dataflow planned once
    assert len(plan.calls) == 3 + 1 + 13 + 1  # JOB_SALES_DAILY adds 13 more dataflows
    assert plan.max_calls == len(plan.calls) * RETRY_FACTOR
    assert plan.max_output_tokens == plan.max_calls * 16000
    assert plan.max_cost_usd == round((plan.max_input_tokens * 4 + plan.max_output_tokens * 20) / 1e6, 2)


def test_token_identifies_exactly_one_plan(tmp_path):
    a = Explainer(estate(), cfg(tmp_path), provider=ExternalFake()).plan(["JOB_CUSTOMER_SYNC"])
    assert Explainer(estate(), cfg(tmp_path), provider=ExternalFake()).plan(["JOB_CUSTOMER_SYNC"]).token == a.token
    assert Explainer(estate(), cfg(tmp_path, max_tokens=8000), provider=ExternalFake()).plan(["JOB_CUSTOMER_SYNC"]).token != a.token
    assert Explainer(estate(), cfg(tmp_path, model="claude-sonnet-5-5"), provider=ExternalFake()).plan(["JOB_CUSTOMER_SYNC"]).token != a.token
    assert Explainer(estate(), cfg(tmp_path), provider=ExternalFake()).plan(["JOB_RETURNS_DAILY"]).token != a.token


def test_limits_and_unknown_prices(tmp_path):
    plan = Explainer(estate(), cfg(tmp_path), provider=ExternalFake()).plan(["JOB_SALES_DAILY"])
    assert plan.violations(Limits()) == []
    v = plan.violations(Limits(max_calls=5, max_cost_usd=1.0))
    assert len(v) == 2 and v[0].startswith(f"up to {plan.max_calls} calls")
    with pytest.raises(ExplainError, match="No price known for model claude-x"):
        Explainer(estate(), cfg(tmp_path, model="claude-x"), provider=ExternalFake()).plan(["JOB_CUSTOMER_SYNC"])
    priced = cfg(tmp_path, model="claude-x", limits=Limits(price_input_per_mtok=1, price_output_per_mtok=2))
    assert Explainer(estate(), priced, provider=ExternalFake()).plan(["JOB_CUSTOMER_SYNC"]).max_cost_usd > 0


# ---------- CLI ----------

def project(tmp_path, limits: str = "") -> Path:
    (tmp_path / "exports").mkdir()
    for f in (ROOT / "demo" / "sap_bods" / "exports").glob("*.xml"):
        (tmp_path / "exports" / f.name).write_bytes(f.read_bytes())
    (tmp_path / "deltabound.toml").write_text(
        '[source]\nname = "t"\ntype = "sap_bods"\n[explain]\nprovider = "anthropic"\n' + limits)
    return tmp_path


def test_cli_plan_sends_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("deltabound.explain.explainer.get_provider", lambda c: pytest.fail("provider created"))
    cli.main(["explain", str(project(tmp_path)), "--job", "JOB_CUSTOMER_SYNC", "--plan"])
    out = capsys.readouterr().out
    assert "4 request(s) to anthropic claude-opus-5-5" in out and "Nothing was sent. To run exactly this plan: --approve" in out


def test_cli_without_approval_is_refused_non_interactively(tmp_path, monkeypatch):
    monkeypatch.setattr("deltabound.explain.explainer.get_provider", lambda c: pytest.fail("provider created"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="needs human approval"):
        cli.main(["explain", str(project(tmp_path)), "--job", "JOB_CUSTOMER_SYNC"])


def test_cli_interactive_prompt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("deltabound.explain.explainer.get_provider", lambda c: ExternalFake())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    with pytest.raises(SystemExit, match="cancelled, nothing was sent"):
        cli.main(["explain", str(project(tmp_path)), "--job", "JOB_CUSTOMER_SYNC"])
    monkeypatch.setattr("builtins.input", lambda prompt: "APPROVE")
    cli.main(["explain", str(tmp_path), "--job", "JOB_CUSTOMER_SYNC", "--approver", "Kishore"])
    events = [json.loads(l) for l in (tmp_path / ".deltabound" / "explain" / "audit.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events][:2] == ["refused", "approved"]
    assert events[1]["approver"] == "Kishore" and sum(e["event"] == "call" for e in events) == 4


def test_cli_approve_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("deltabound.explain.explainer.get_provider", lambda c: ExternalFake())
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    p = project(tmp_path)
    cli.main(["explain", str(p), "--job", "JOB_CUSTOMER_SYNC", "--plan"])
    token = capsys.readouterr().out.rsplit("--approve ", 1)[1].strip()
    with pytest.raises(SystemExit, match="does not match this plan"):
        cli.main(["explain", str(p), "--job", "JOB_CUSTOMER_SYNC", "--approve", "abc"])
    cli.main(["explain", str(p), "--job", "JOB_CUSTOMER_SYNC", "--approve", token])
    assert "4 model call(s)" in capsys.readouterr().out
    # The same token cannot be reused for a different run: cached now, and a new job changes the plan.
    with pytest.raises(SystemExit, match="does not match this plan"):
        cli.main(["explain", str(p), "--job", "JOB_SALES_DAILY", "--approve", token])


def test_cli_over_limits_is_refused_before_asking(tmp_path, monkeypatch):
    monkeypatch.setattr("deltabound.explain.explainer.get_provider", lambda c: pytest.fail("provider created"))
    p = project(tmp_path, "[explain.limits]\nmax_cost_usd = 1.0\n")
    with pytest.raises(SystemExit, match="over the limits in \\[explain.limits\\]: up to \\$2.61 > max_cost_usd \\$1.00"):
        cli.main(["explain", str(p), "--job", "JOB_CUSTOMER_SYNC", "--approve", "anything"])


def test_fake_provider_needs_no_approval(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DELTABOUND_LLM_PROVIDER", "fake")
    cli.main(["explain", str(ROOT / "demo" / "sap_bods" / "exports"), "--job", "JOB_CUSTOMER_SYNC"])
    assert "4 model call(s)" in capsys.readouterr().out


def test_plan_model_round_trip():
    p = Plan(jobs=["J"], provider="anthropic", model="claude-opus-5-5", max_tokens=100)
    assert Plan.model_validate_json(p.model_dump_json()).token == p.token

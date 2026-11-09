"""Sign-off report: Markdown and a static HTML page (no scripts), from one ValidationRun."""
from __future__ import annotations

import html
from typing import Any

from .model import CheckResult, StepResult, ValidationRun

CHECKS = ("schema", "row_count", "keys", "values", "multiset")
LABEL = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "waived": "WAIVED", "skipped": "-",
         "pass_with_waivers": "PASS WITH WAIVERS"}
MD_SAMPLES = 5


def _check(step: StepResult, name: str) -> CheckResult | None:
    return next((c for c in step.checks if c.name == name), None)


def headline(run: ValidationRun) -> str:
    failed = [s for s in run.steps if s.status == "fail"]
    root = [s for s in failed if not s.cascading_from]
    waived = sum(1 for s in run.steps if s.status == "waived")
    text = f"{len(run.steps) - len(failed)} of {len(run.steps)} steps pass"
    if failed:
        text += f"; {len(failed)} fail ({len(root)} root cause(s), {len(failed) - len(root)} cascading)"
        text += f"; first failing step: {failed[0].dataflow}"
    if waived:
        text += f"; {waived} pass only with waivers"
    return text


def _cell(v: Any) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ")


def render_markdown(run: ValidationRun) -> str:
    out = [f"# Validation sign-off: {run.job}", "",
           f"**Status: {LABEL[run.status]}** ({headline(run)}).", "",
           f"Business date {run.business_date or 'not set'} · engine {run.engine} · run {run.created_at} · "
           f"deltabound {run.inputs.get('deltabound')}", "", "## Steps in call order", "",
           "| # | Dataflow | Target | Schema | Rows | Keys | Values / rows | Status |", "|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(run.steps, 1):
        cells = []
        for name in ("schema", "row_count", "keys"):
            c = _check(s, name)
            cells.append(LABEL[c.status] if c else "-")
        v = _check(s, "values") or _check(s, "multiset")
        cells.append(LABEL[v.status] if v else "-")
        status = LABEL[s.status] + (f" (cascading from {s.cascading_from})" if s.cascading_from else "")
        out.append(f"| {i} | `{s.dataflow}` | `{s.target}` | " + " | ".join(cells) + f" | **{status}** |")
    out += ["", "## Details", ""]
    for s in run.steps:
        if s.status in ("pass", "skipped") and not s.convert_todos:
            continue
        out += [f"### {s.dataflow} → {s.target}: {LABEL[s.status]}", ""]
        if s.cascading_from:
            out += [f"Reads the output of {s.cascading_from}, which also failed: fix that step first.", ""]
        if s.error:
            out += [f"- **Error:** {_cell(s.error)}"]
        if s.convert_todos:
            out += [f"- Conversion TODO in this dataflow: `{t}`" for t in s.convert_todos]
        for c in s.checks:
            if c.status in ("pass", "skipped"):
                continue
            out.append(f"- **{c.name}** ({LABEL[c.status]}): {_cell(c.summary)}")
            for f in c.findings:
                col = f"`{f.column}`: " if f.column else ""
                out.append(f"  - {LABEL[f.status]} {col}{_cell(f.issue)}" + (f" — waived: {_cell(f.waiver)}" if f.waiver else ""))
                out += [f"    - {_cell(h)}" for h in f.hints]
                out += _md_samples(f.samples)
            out += _md_samples(c.samples)
        out.append("")
    out += ["## Tolerances", "", "Defaults for every step (per-step overrides below):", "",
            "| Setting | Value |", "|---|---|"]
    out += [f"| {k} | `{_cell(v)}` |" for k, v in run.defaults.items()]
    out += [""]
    for s in run.steps:
        r = s.rules
        if r.get("ignored") or r.get("column_rules"):
            out.append(f"- `{s.dataflow}`: keys {r.get('keys') or 'none (keyless)'}; ignored {r.get('ignored') or 'none'}"
                       + (f"; column rules {r['column_rules']}" if r.get("column_rules") else ""))
    out += ["", "## Waivers", ""]
    out += [f"- `{w['dataflow']}` {w['check']}{' ' + w['column'] if w.get('column') else ''}: {_cell(w['reason'])} "
            f"(approved by {w['approved_by']}{', expires ' + str(w['expires']) if w.get('expires') else ''})"
            + (" — **EXPIRED, not applied**" if w.get("expired") else "") for w in run.waivers] or ["None."]
    out += ["", "## Inputs", "", "| Step | Legacy | Databricks |", "|---|---|---|"]
    out += [f"| `{s.dataflow}` | `{s.inputs.get('legacy', '-')}` | `{s.inputs.get('databricks', '-')}` |" for s in run.steps]
    out += ["", f"IR fingerprint `{run.inputs.get('ir')}`.", "", "## Sign-off", "",
            "| Role | Name | Date | Signature |", "|---|---|---|---|",
            "| Validated by | | | |", "| Approved by (data owner) | | | |", ""]
    return "\n".join(out)


def _md_samples(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    cols = list(rows[0])
    out = ["", "    | " + " | ".join(cols) + " |", "    |" + "---|" * len(cols)]
    out += ["    | " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows[:MD_SAMPLES]]
    if len(rows) > MD_SAMPLES:
        out.append(f"    ({len(rows) - MD_SAMPLES} more sample rows in result.json)")
    return out + [""]


CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6a64;--card:#fff;--line:#dedcd5;--pass:#2e7d4f;--fail:#b42318;--warn:#a8620a;--waived:#5b5bd6}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--fg:#ecebe6;--muted:#a3a29b;--card:#1e1e1c;--line:#383733;--pass:#6fcf97;--fail:#ff8a80;--warn:#f2b24c;--waived:#a5b4fc}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px}h2{font-size:17px;margin-top:28px}h3{font-size:15px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}code,.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.s{font-weight:700}.pass{color:var(--pass)}.fail{color:var(--fail)}.warn{color:var(--warn)}.waived,.pass_with_waivers{color:var(--waived)}
.muted{color:var(--muted)}ul{padding-left:20px}.sign td{height:28px}
"""


def render_html(run: ValidationRun) -> str:
    e = html.escape

    def badge(status: str) -> str:
        return f'<span class="s {e(status)}">{e(LABEL[status])}</span>'

    def samples(rows: list[dict]) -> str:
        if not rows:
            return ""
        cols = list(rows[0])
        head = "".join(f"<th>{e(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td class=mono>{e(str(r.get(c)))}</td>" for c in cols) + "</tr>" for r in rows)
        return f"<table><tr>{head}</tr>{body}</table>"

    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Validation {e(run.job)}</title><style>{CSS}</style></head><body><main>",
             f"<h1>Validation sign-off: {e(run.job)}</h1>",
             f"<p>Status: {badge(run.status)} — {e(headline(run))}</p>",
             f"<p class=muted>Business date {e(run.business_date or 'not set')} · engine {e(run.engine)} · run "
             f"{e(run.created_at)} · deltabound {e(run.inputs.get('deltabound', ''))}</p>",
             "<h2>Steps in call order</h2><section><table><tr><th>#</th><th>Dataflow</th><th>Target</th>"
             + "".join(f"<th>{n}</th>" for n in ("Schema", "Rows", "Keys", "Values / rows", "Status")) + "</tr>"]
    for i, s in enumerate(run.steps, 1):
        cells = [_check(s, n) for n in ("schema", "row_count", "keys")] + [_check(s, "values") or _check(s, "multiset")]
        casc = f"<br><span class=muted>cascading from {e(s.cascading_from)}</span>" if s.cascading_from else ""
        parts.append(f"<tr><td>{i}</td><td><a href='#{e(s.dataflow)}' class=mono>{e(s.dataflow)}</a></td>"
                     f"<td class=mono>{e(s.target)}</td>" + "".join(f"<td>{badge(c.status) if c else '-'}</td>" for c in cells)
                     + f"<td>{badge(s.status)}{casc}</td></tr>")
    parts.append("</table></section><h2>Details</h2>")
    for s in run.steps:
        if s.status in ("pass", "skipped") and not s.convert_todos:
            continue
        parts.append(f"<section id='{e(s.dataflow)}'><h3>{e(s.dataflow)} → {e(s.target)}: {badge(s.status)}</h3>")
        if s.cascading_from:
            parts.append(f"<p>Reads the output of <b>{e(s.cascading_from)}</b>, which also failed: fix that step first.</p>")
        if s.error:
            parts.append(f"<p class=fail><b>Error:</b> {e(s.error)}</p>")
        parts += [f"<p>Conversion TODO: <code>{e(t)}</code></p>" for t in s.convert_todos]
        for c in s.checks:
            if c.status in ("pass", "skipped"):
                continue
            parts.append(f"<p><b>{e(c.name)}</b> {badge(c.status)}: {e(c.summary)}</p><ul>")
            for f in c.findings:
                col = f"<code>{e(f.column)}</code>: " if f.column else ""
                waived = f" <span class=waived>waived: {e(f.waiver)}</span>" if f.waiver else ""
                hints = "".join(f"<li class=muted>{e(h)}</li>" for h in f.hints)
                parts.append(f"<li>{badge(f.status)} {col}{e(f.issue)}{waived}<ul>{hints}</ul>{samples(f.samples)}</li>")
            parts.append(f"</ul>{samples(c.samples)}")
        parts.append("</section>")
    parts.append("<h2>Tolerances</h2><section><table>" + "".join(
        f"<tr><th>{e(k)}</th><td class=mono>{e(str(v))}</td></tr>" for k, v in run.defaults.items()) + "</table><ul>")
    for s in run.steps:
        r = s.rules
        if r.get("ignored") or r.get("column_rules"):
            parts.append(f"<li><code>{e(s.dataflow)}</code>: keys {e(str(r.get('keys') or 'none (keyless)'))}; "
                         f"ignored {e(str(r.get('ignored') or 'none'))}</li>")
    parts.append("</ul></section><h2>Waivers</h2><section><ul>")
    parts += [f"<li><code>{e(w['dataflow'])}</code> {e(w['check'])} {e(w.get('column') or '')}: {e(w['reason'])} "
              f"(approved by {e(w['approved_by'])})" + (" <b class=fail>EXPIRED, not applied</b>" if w.get("expired") else "")
              + "</li>" for w in run.waivers] or ["<li>None.</li>"]
    parts.append("</ul></section><h2>Inputs</h2><section><table><tr><th>Step</th><th>Legacy</th><th>Databricks</th></tr>")
    parts += [f"<tr><td class=mono>{e(s.dataflow)}</td><td class=mono>{e(s.inputs.get('legacy', '-'))}</td>"
              f"<td class=mono>{e(s.inputs.get('databricks', '-'))}</td></tr>" for s in run.steps]
    parts.append(f"</table><p class=muted>IR fingerprint <code>{e(run.inputs.get('ir', ''))}</code></p></section>"
                 "<h2>Sign-off</h2><section><table class=sign><tr><th>Role</th><th>Name</th><th>Date</th><th>Signature</th></tr>"
                 "<tr><td>Validated by</td><td></td><td></td><td></td></tr>"
                 "<tr><td>Approved by (data owner)</td><td></td><td></td><td></td></tr></table></section></main></body></html>\n")
    return "".join(parts)

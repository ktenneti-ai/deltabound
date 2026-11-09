"""Conversion report: what was converted by which rule, and what a person must still do."""
from __future__ import annotations

import json
import re

from .model import ConversionResult

UNMAPPED = re.compile(r"datastore (\S+) is not mapped")


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_report(r: ConversionResult) -> tuple[str, str]:
    transforms = [t for d in r.dataflows for t in d.transforms]
    converted = sum(1 for t in transforms if t.status == "converted")
    task_todos = [t for t in r.tasks if t.todos]
    out = [
        f"# Conversion report: {r.job}",
        "",
        f"- Dataflows: {len(r.dataflows)}, transforms: {len(transforms)}, "
        f"converted: {converted} ({100 * converted / len(transforms):.0f}%)" if transforms else "- No transforms",
        f"- Tasks in the Databricks job: {sum(1 for t in r.tasks if t.kind != 'workflow')}",
        f"- **Blocking TODOs: {r.todo_count}.** Steps with a TODO raise `NotImplementedError` (notebooks) "
        "or are flagged in the task description (job). The job must not run in production until this list is empty.",
        "",
        "## Blocking TODOs",
        "",
    ]
    todos = [(t.cite, x) for t in transforms for x in t.todos] + [(t.source, x) for t in task_todos for x in t.todos]
    out += [f"- [ ] `{c}`: {_cell(x)}" for c, x in todos] or ["None."]
    out += ["", "## Review notes", "", "Assumptions the converter made. Check each one against the legacy behaviour.", ""]
    notes = [(t.cite, x) for t in transforms for x in t.notes] + [(t.source, x) for t in r.tasks for x in t.notes]
    unmapped: dict[str, int] = {}
    for _, x in notes:
        if m := UNMAPPED.search(x):
            unmapped[m.group(1)] = unmapped.get(m.group(1), 0) + 1
    out += [f"- Datastore `{ds}` is not mapped in `[convert.datastores]`; {n} table reference(s) use the "
            "lower-case legacy names. Map it to a Unity Catalog `catalog.schema`." for ds, n in sorted(unmapped.items())]
    rest = [(c, x) for c, x in notes if not UNMAPPED.search(x)]
    out += [f"- `{c}`: {_cell(x)}" for c, x in rest]
    if not notes:
        out.append("None.")
    out += ["", "## Dataflows", ""]
    for d in r.dataflows:
        out += [f"### {d.dataflow}", "", f"Notebook `{d.notebook}`; writes {', '.join(f'`{w}`' for w in d.writes) or 'nothing'}; "
                f"parameters {', '.join(d.variables) or 'none'}.", "",
                "| Transform | Kind | Rule | Status |", "|---|---|---|---|"]
        out += [f"| `{t.cite.split('/', 1)[1]}` | {t.kind} | {t.rule or '-'} | {'TODO' if t.status == 'todo' else 'converted'} |"
                for t in d.transforms]
        out.append("")
    out += ["## Job tasks", "", "| Task | Kind | From | TODO |", "|---|---|---|---|"]
    out += [f"| `{t.task_key}` | {t.kind} | `{t.source}` | {'yes' if t.todos else ''} |" for t in r.tasks]
    md = "\n".join(out).rstrip() + "\n"
    js = json.dumps({"job": r.job, "todo_count": r.todo_count, "dataflows": [d.model_dump() for d in r.dataflows],
                     "tasks": [t.model_dump() for t in r.tasks]}, indent=2) + "\n"
    return md, js

"""Plain-text rendering of an explanation for the terminal."""
from __future__ import annotations

import textwrap

from .model import JobExplanation

SECTIONS = (("purpose", "Purpose"), ("inputs", "Inputs"), ("outputs", "Outputs"),
            ("business_rules", "Business rules"))


def render_text(exp: JobExplanation, width: int = 100) -> str:
    lines = [f"{exp.job}  (model {exp.run.model}, {exp.run.created_at})", ""]
    for attr, title in SECTIONS:
        lines.append(f"{title}:")
        claims = getattr(exp, attr)
        if not claims:
            lines.append("  (none)")
        for c in claims:
            mark = " [from IR]" if c.origin == "ir" else ""
            lines += textwrap.wrap(c.text + mark, width, initial_indent="  - ", subsequent_indent="    ")
            lines += textwrap.wrap("sources: " + ", ".join(c.citations), width,
                                   initial_indent="      ", subsequent_indent="        ")
        lines.append("")
    if exp.rejected:
        lines.append(f"{len(exp.rejected)} claim(s) not shown: they failed the citation check.")
    return "\n".join(lines).rstrip() + "\n"

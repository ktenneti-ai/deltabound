"""Step-level validation of a converted pipeline against saved legacy outputs, with a sign-off report.

Reads only the IR (never a parser). See docs/design/validate.md.
"""
from .engines import DuckDBEngine, SparkEngine
from .manifest import Manifest, ValidateError, Waiver, load_manifest
from .model import ValidationRun
from .report import render_html, render_markdown
from .run import dumps, run_validation
from .scaffold import scaffold

__all__ = ["DuckDBEngine", "Manifest", "SparkEngine", "ValidateError", "ValidationRun", "Waiver", "dumps",
           "load_manifest", "render_html", "render_markdown", "run_validation", "scaffold"]

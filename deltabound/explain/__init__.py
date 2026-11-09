"""Plain-English job explanations with citations back to the IR.

Reads only the IR (never a parser). See docs/design/explain.md.
"""
from .config import ExplainConfig, ExplainError, load_config
from .explainer import Explainer, explainable_jobs
from .model import Claim, DataflowNotes, ExplanationBundle, JobExplanation
from .provider import FakeProvider, get_provider

__all__ = [
    "Claim", "DataflowNotes", "ExplainConfig", "ExplainError", "ExplanationBundle", "Explainer",
    "FakeProvider", "JobExplanation", "explainable_jobs", "get_provider", "load_config",
]

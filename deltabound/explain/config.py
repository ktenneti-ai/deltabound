"""Explain settings: defaults, then deltabound.toml [explain], then environment variables."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ValidationError

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    tomllib = None  # type: ignore[assignment]

PROVIDERS = ("anthropic", "fake")
ENV_PROVIDER = "DELTABOUND_LLM_PROVIDER"
ENV_MODEL = "DELTABOUND_LLM_MODEL"


class ExplainError(Exception):
    """A problem the user can fix: configuration, credentials, provider refusal."""


class ExplainConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 16000
    max_retries: int = 3  # SDK retries for 429, 5xx and connection errors
    # Server-side refusal fallback: if the model declines, the API reruns the
    # request on a fallback model it chooses. Set false to fail instead.
    fallbacks: bool = True
    redact_literals: bool = False  # replace string/number literals before sending
    cache_dir: str = ".deltabound/explain"


def load_config(path: Optional[Path] = None) -> ExplainConfig:
    """Load settings. `path` defaults to ./deltabound.toml when it exists."""
    values: dict = {}
    toml_path = path or Path("deltabound.toml")
    if toml_path.exists():
        if tomllib is None:
            raise ExplainError(f"Reading {toml_path} needs Python 3.11+; use environment variables instead.")
        with toml_path.open("rb") as f:
            values = dict(tomllib.load(f).get("explain", {}))
    elif path is not None:
        raise ExplainError(f"Config file not found: {path}")
    if os.environ.get(ENV_PROVIDER):
        values["provider"] = os.environ[ENV_PROVIDER]
    if os.environ.get(ENV_MODEL):
        values["model"] = os.environ[ENV_MODEL]
    try:
        cfg = ExplainConfig(**values)
    except ValidationError as e:
        raise ExplainError(f"Invalid [explain] settings: {e}") from e
    if toml_path.exists() and not Path(cfg.cache_dir).is_absolute():
        cfg.cache_dir = str(toml_path.resolve().parent / cfg.cache_dir)  # cache lives next to its settings
    if cfg.provider not in PROVIDERS:
        raise ExplainError(f"Unknown provider '{cfg.provider}'. Supported: {', '.join(PROVIDERS)}.")
    return cfg

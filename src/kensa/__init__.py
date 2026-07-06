"""Kensa agent regression eval harness."""

from __future__ import annotations

from importlib.metadata import version as _metadata_version

__version__ = _metadata_version("kensa")

from kensa.pytest import kensa_case
from kensa.tracing import instrument, record_llm_call, record_span, record_tool_call

__all__ = [
    "__version__",
    "instrument",
    "kensa_case",
    "record_llm_call",
    "record_span",
    "record_tool_call",
]

"""Kensa agent regression eval harness."""

from __future__ import annotations

from importlib.metadata import version as _metadata_version

__version__ = _metadata_version("kensa")

from kensa.behavior import (
    BEHAVIOR_CANDIDATE_SCHEMA_VERSION,
    BEHAVIOR_FINGERPRINT_VERSION,
    BehaviorCandidate,
    behavior_candidate_schema,
    behavior_semantic_fingerprint,
)
from kensa.errors import KensaTimeoutError
from kensa.pytest import kensa_case
from kensa.tracing import instrument, record_llm_call, record_span, record_tool_call

__all__ = [
    "BEHAVIOR_CANDIDATE_SCHEMA_VERSION",
    "BEHAVIOR_FINGERPRINT_VERSION",
    "BehaviorCandidate",
    "KensaTimeoutError",
    "__version__",
    "behavior_candidate_schema",
    "behavior_semantic_fingerprint",
    "instrument",
    "kensa_case",
    "record_llm_call",
    "record_span",
    "record_tool_call",
]

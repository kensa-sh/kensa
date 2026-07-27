"""Public pytest authoring API for Kensa tests."""

from __future__ import annotations

from kensa.case import (
    KensaAssistantMessage,
    KensaCase,
    KensaDeveloperMessage,
    KensaFunctionCall,
    KensaMessage,
    KensaSystemMessage,
    KensaToolCall,
    KensaToolMessage,
    KensaUserMessage,
    kensa_case,
)
from kensa.conversation import (
    CaseResult,
    ConversationAgent,
    ConversationError,
    ConversationResponse,
    LLMSimulator,
    Simulator,
    Termination,
)
from kensa.errors import KensaTimeoutError
from kensa.judge import JudgeResult, judge
from kensa.runtime import KensaTrace
from kensa.target import (
    AgentEvent,
    AgentRunEvidence,
    EffectPolicy,
    EvidenceCompleteness,
    ExecutionAttestation,
    StateObservation,
    TraceReference,
    attach_agent_run,
)

__all__ = [
    "AgentEvent",
    "AgentRunEvidence",
    "CaseResult",
    "ConversationAgent",
    "ConversationError",
    "ConversationResponse",
    "EffectPolicy",
    "EvidenceCompleteness",
    "ExecutionAttestation",
    "JudgeResult",
    "KensaAssistantMessage",
    "KensaCase",
    "KensaDeveloperMessage",
    "KensaFunctionCall",
    "KensaMessage",
    "KensaSystemMessage",
    "KensaTimeoutError",
    "KensaToolCall",
    "KensaToolMessage",
    "KensaTrace",
    "KensaUserMessage",
    "LLMSimulator",
    "Simulator",
    "StateObservation",
    "Termination",
    "TraceReference",
    "attach_agent_run",
    "judge",
    "kensa_case",
]

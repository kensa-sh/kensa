"""Pydantic models for Kensa CLI contracts."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KensaModel(BaseModel):
    """Base model for structured Kensa CLI payloads."""

    model_config = ConfigDict(frozen=True)


class LLMProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class LLMModel(StrEnum):
    GPT_5_4_MINI = "gpt-5.4-mini"
    GPT_5_5 = "gpt-5.5"
    CLAUDE_SONNET_4_6 = "claude-sonnet-4-6"
    CLAUDE_OPUS_4_7 = "claude-opus-4-7"


class LLMConfig(KensaModel):
    provider: LLMProvider = LLMProvider.OPENAI
    model: LLMModel = LLMModel.GPT_5_4_MINI


class CliEnvelope(KensaModel):
    schema_version: Literal["kensa.cli.v1"] = "kensa.cli.v1"
    command: str
    ok: bool
    exit_code: int
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


EvidenceSource = Literal["langfuse", "trace_export", "local"]
AgentInstruction = Literal["codex", "claude", "cursor", "other"]
RedactionModelChoice = Literal["small", "large"]


class KensaProjectConfig(KensaModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_source: EvidenceSource | None = None
    redaction_model: RedactionModelChoice | None = None


class InspectStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    GENERATED = "generated"


class ExpectedCurrentBehavior(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class InspectIdea(KensaModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    trace_ids: list[str] = Field(min_length=1)
    source: str = Field(min_length=1)
    status: InspectStatus = InspectStatus.PENDING
    failure_pattern: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    expected_current_behavior: ExpectedCurrentBehavior
    proposed_checks: list[str] = Field(default_factory=list)
    case_shape: str | None = None
    risks: str | None = None

    @field_validator("trace_ids")
    @classmethod
    def _non_empty_trace_ids(cls, value: list[str]) -> list[str]:
        if any(not trace_id.strip() for trace_id in value):
            raise ValueError("trace_ids entries must be non-empty")
        return value


class InspectQueue(KensaModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["kensa.inspect.v1"] = "kensa.inspect.v1"
    items: list[InspectIdea] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_item_ids(self) -> InspectQueue:
        counts = Counter(item.id for item in self.items)
        duplicates = sorted(item_id for item_id, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError("duplicate inspect item ids: " + ", ".join(duplicates))
        return self

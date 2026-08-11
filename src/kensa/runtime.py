"""Per-pytest-trial runtime state and trace evidence."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock, get_ident
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from kensa._serialization import json_value, jsonable
from kensa._smoke import is_smoke_identity
from kensa.errors import KensaCaseError, TrialFailure

if TYPE_CHECKING:
    from kensa.case import KensaCase
    from kensa.engine import EngineClient
    from kensa.target import AgentEvent, AgentRunEvidence

_CURRENT_RUNTIME: ContextVar[KensaTrialRuntime | None] = ContextVar(
    "kensa_current_runtime", default=None
)
_EXPORTER: Any | None = None
_PROVIDER_READY = False

OperationKind = Literal["span", "tool", "llm"]
_ExecutionOwner = tuple[int, asyncio.Task[Any] | None]
_GEN_AI_LLM_OPERATIONS = frozenset({"chat", "embeddings", "generate_content", "text_completion"})
_GEN_AI_LLM_ATTRIBUTES = frozenset(
    {
        "gen_ai.completion",
        "gen_ai.prompt",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.system",
        "gen_ai.usage.completion_tokens",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.prompt_tokens",
    }
)
_JSON_OBJECT_ADAPTER = TypeAdapter(
    dict[str, JsonValue],
    config=ConfigDict(strict=True, allow_inf_nan=False),
)


def _execution_owner() -> _ExecutionOwner:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return get_ident(), task


@dataclass(frozen=True)
class KensaTrial:
    trial_index: int
    configured_trials: int
    timeout_s: float | None = None

    @property
    def id(self) -> str:
        return f"trial{self.trial_index}"


@dataclass(frozen=True)
class ActiveOperation:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    kind: OperationKind = "span"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "attributes": self.attributes,
        }


@dataclass
class KensaSpan:
    name: str
    kind: str = "span"
    tool_name: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    start_time_unix_nano: int | None = None
    end_time_unix_nano: int | None = None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.start_time_unix_nano is None or self.end_time_unix_nano is None:
            return 0.0
        return max(0.0, (self.end_time_unix_nano - self.start_time_unix_nano) / 1_000_000)

    @property
    def cost_usd(self) -> float:
        value = self._cost_value()
        return value if value is not None else 0.0

    @property
    def cost_available(self) -> bool:
        return self._cost_value() is not None

    def _cost_value(self) -> float | None:
        if "kensa.cost_usd" in self.attributes:
            value = self.attributes["kensa.cost_usd"]
        elif "cost_usd" in self.attributes:
            value = self.attributes["cost_usd"]
        else:
            return None
        if isinstance(value, bool):
            return None
        try:
            cost = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return cost if math.isfinite(cost) and cost >= 0 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "start_time_unix_nano": self.start_time_unix_nano,
            "end_time_unix_nano": self.end_time_unix_nano,
            "status": self.status,
            "attributes": self.attributes,
            "duration_ms": self.duration_ms,
            "cost_usd": self.cost_usd if self.cost_available else None,
            "cost_available": self.cost_available,
        }


class ToolCallEvidence(BaseModel):
    """Immutable normalized evidence for one observed tool call."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        allow_inf_nan=False,
    )

    sequence: Annotated[int, Field(ge=0)]
    name: str
    arguments: JsonValue | None
    result: JsonValue | None
    arguments_recorded: bool
    result_recorded: bool
    status: str
    span_id: str | None
    duration_ms: Annotated[float, Field(ge=0)]


class KensaTraceTools:
    """Tool-call assertions for a trial trace."""

    def __init__(self, trace: KensaTrace) -> None:
        self._trace = trace

    @property
    def calls(self) -> tuple[ToolCallEvidence, ...]:
        """Return normalized tool calls in merged trace order."""
        tool_spans = [
            span for span in self._trace.spans if span.tool_name or span.kind.lower() == "tool"
        ]
        return tuple(
            _normalize_tool_call(span, sequence) for sequence, span in enumerate(tool_spans)
        )

    @property
    def names(self) -> list[str]:
        """Return observed tool names in trace order, including repeats."""
        return [call.name for call in self.calls]

    def matching(
        self,
        name: str,
        *,
        arguments: Mapping[str, JsonValue] | None = None,
        result: Mapping[str, JsonValue] | None = None,
        status: str | None = None,
    ) -> tuple[ToolCallEvidence, ...]:
        """Return calls satisfying the supplied recursive object subsets."""
        arguments_subset = _validate_tool_subset(arguments, boundary="arguments")
        result_subset = _validate_tool_subset(result, boundary="result")
        return tuple(
            call
            for call in self.calls
            if call.name == name
            and (status is None or call.status == status)
            and (
                arguments_subset is None
                or (call.arguments_recorded and _mapping_subset(call.arguments, arguments_subset))
            )
            and (
                result_subset is None
                or (call.result_recorded and _mapping_subset(call.result, result_subset))
            )
        )

    def called(
        self,
        name: str,
        *,
        arguments: Mapping[str, JsonValue] | None = None,
        result: Mapping[str, JsonValue] | None = None,
        status: str | None = None,
    ) -> bool:
        """Return whether any call satisfies the supplied filters."""
        return bool(
            self.matching(
                name,
                arguments=arguments,
                result=result,
                status=status,
            )
        )

    def include(self, tool_names: list[str]) -> bool:
        """Return whether every listed tool appears at least once."""
        actual = self.names
        return all(name in actual for name in tool_names)

    def exclude(self, tool_names: list[str]) -> bool:
        """Return whether none of the listed tools appear."""
        actual = self.names
        return all(name not in actual for name in tool_names)

    def order(self, tool_names: list[str]) -> bool:
        """Return whether listed tools appear in order, allowing interleaved calls."""
        actual = iter(self.names)
        return all(name in actual for name in tool_names)

    def no_repeats(self) -> bool:
        """Return whether no observed tool name appears more than once."""
        names = self.names
        return len(names) == len(set(names))


class KensaTrace:
    """Live trace evidence view for the current Kensa trial."""

    def __init__(self) -> None:
        self.spans: list[KensaSpan] = []
        self.agent_runs: tuple[AgentRunEvidence, ...] = ()
        self.incomplete = False
        self.incomplete_reason: str | None = None

    @property
    def tools(self) -> KensaTraceTools:
        return KensaTraceTools(self)

    @property
    def cost_usd(self) -> float | None:
        return self.known_cost_usd if self.cost_available else None

    @property
    def known_cost_usd(self) -> float | None:
        costs = [span.cost_usd for span in self.spans if span.cost_available]
        return round(sum(costs), 8) if costs else None

    @property
    def cost_available(self) -> bool:
        billable = [
            span for span in self.spans if span.kind.lower() == "llm" or span.cost_available
        ]
        return bool(billable) and all(span.cost_available for span in billable)

    @property
    def llm_turns(self) -> int:
        return sum(1 for span in self.spans if span.kind.lower() == "llm")

    @property
    def duration_ms(self) -> float:
        if not self.spans:
            return 0.0
        starts = [s.start_time_unix_nano for s in self.spans if s.start_time_unix_nano is not None]
        ends = [s.end_time_unix_nano for s in self.spans if s.end_time_unix_nano is not None]
        if not starts or not ends:
            return 0.0
        return max(0.0, (max(ends) - min(starts)) / 1_000_000)

    def replace(
        self,
        spans: list[KensaSpan],
        *,
        agent_runs: tuple[AgentRunEvidence, ...] = (),
        incomplete: bool = False,
        incomplete_reason: str | None = None,
    ) -> None:
        self.spans = spans
        self.agent_runs = agent_runs
        self.incomplete = incomplete
        self.incomplete_reason = incomplete_reason

    def to_dict(self) -> dict[str, Any]:
        known_cost_usd = self.known_cost_usd
        tool_calls = self.tools.calls
        return {
            "spans": [span.to_dict() for span in self.spans],
            "agent_runs": [run.model_dump(mode="json") for run in self.agent_runs],
            "tools": [call.name for call in tool_calls],
            "tool_calls": [call.model_dump(mode="json") for call in tool_calls],
            "cost_usd": self.cost_usd,
            "known_cost_usd": known_cost_usd,
            "cost_available": self.cost_available,
            "llm_turns": self.llm_turns,
            "duration_ms": self.duration_ms,
            "incomplete": self.incomplete,
            "incomplete_reason": self.incomplete_reason,
        }


def _normalize_tool_call(span: KensaSpan, sequence: int) -> ToolCallEvidence:
    arguments, arguments_recorded = _tool_payload(
        span,
        canonical="kensa.tool.args",
        attached="input",
        flat="arguments",
    )
    result, result_recorded = _tool_payload(
        span,
        canonical="kensa.tool.result",
        attached="output",
        flat="result",
    )
    return ToolCallEvidence(
        sequence=sequence,
        name=span.tool_name or span.name,
        arguments=arguments,
        result=result,
        arguments_recorded=arguments_recorded,
        result_recorded=result_recorded,
        status=span.status,
        span_id=span.span_id,
        duration_ms=span.duration_ms,
    )


def _tool_payload(
    span: KensaSpan,
    *,
    canonical: str,
    attached: str,
    flat: str,
) -> tuple[Any, bool]:
    attributes = span.attributes
    if canonical in attributes:
        value = attributes[canonical]
    elif attributes.get("kensa.evidence.source") == "agent_run" and attached in attributes:
        value = attributes[attached]
    elif flat in attributes:
        value = attributes[flat]
    else:
        return None, False
    return _decode_json_once(value), True


def _decode_json_once(value: Any) -> Any:
    if not isinstance(value, str):
        return deepcopy(value)
    try:
        return json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not valid JSON")


def _validate_tool_subset(
    value: Mapping[str, JsonValue] | None,
    *,
    boundary: str,
) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{boundary} filter must be a mapping")
    try:
        return _JSON_OBJECT_ADAPTER.validate_python(dict(value))
    except ValidationError as exc:
        raise TypeError(f"{boundary} filter must be a strict JSON object: {exc}") from exc


def _mapping_subset(observed: JsonValue | None, expected: dict[str, JsonValue]) -> bool:
    if not isinstance(observed, dict):
        return False
    for key, expected_value in expected.items():
        if key not in observed:
            return False
        observed_value = observed[key]
        if isinstance(expected_value, dict):
            if not _mapping_subset(observed_value, expected_value):
                return False
        elif not _json_values_equal(observed_value, expected_value):
            return False
    return True


def _json_values_equal(observed: JsonValue, expected: JsonValue) -> bool:
    if type(observed) is not type(expected):
        return False
    if isinstance(expected, dict):
        if not isinstance(observed, dict) or observed.keys() != expected.keys():
            return False
        return all(_json_values_equal(observed[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(observed, list) or len(observed) != len(expected):
            return False
        return all(
            _json_values_equal(observed_item, expected_item)
            for observed_item, expected_item in zip(observed, expected, strict=True)
        )
    return observed == expected


@dataclass
class TrialMetadata:
    nodeid: str
    group_id: str
    case_id: str
    trial_index: int
    configured_trials: int
    status: str
    case: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    failure: TrialFailure | None = None
    duration_ms: float = 0.0
    trace: dict[str, Any] = field(default_factory=dict)
    judges: list[dict[str, Any]] = field(default_factory=list)
    active_operation: dict[str, Any] | None = None
    smoke: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "error", "skipped", "provisional"}:
            raise ValueError(f"Unknown trial status: {self.status!r}")
        requires_failure = self.status in {"fail", "error", "skipped"}
        if requires_failure != (self.failure is not None):
            expectation = "one failure" if requires_failure else "failure=None"
            raise ValueError(f"Trial status {self.status!r} requires {expectation}")

    @property
    def is_smoke(self) -> bool:
        return self.smoke or is_smoke_identity(
            case_id=self.case_id,
            group_id=self.group_id,
            nodeid=self.nodeid,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "group_id": self.group_id,
            "case_id": self.case_id,
            "trial_index": self.trial_index,
            "configured_trials": self.configured_trials,
            "status": self.status,
            "case": self.case,
            "output": self.output,
            "failure": (self.failure.model_dump(mode="json") if self.failure is not None else None),
            "duration_ms": self.duration_ms,
            "trace": self.trace,
            "judges": self.judges,
            "active_operation": self.active_operation,
            "smoke": self.is_smoke,
        }


class KensaTrialRuntime:
    """Mutable runtime state for one pytest item/trial."""

    def __init__(
        self,
        *,
        trial: KensaTrial,
        nodeid: str,
        group_id: str,
        case_id: str,
        no_judge: bool,
        judge_timeout_s: float = 30.0,
        operation_callback: Callable[[ActiveOperation | None], None] | None = None,
        snapshot_callback: Callable[[KensaTrialRuntime], None] | None = None,
        engine: EngineClient | None = None,
    ) -> None:
        self.trial = trial
        self.nodeid = nodeid
        self.group_id = group_id
        self.case_id = case_id
        self.no_judge = no_judge
        self.judge_timeout_s = judge_timeout_s
        self.trace = KensaTrace()
        self.output_recorded = False
        self.output: Any = None
        self.case: dict[str, Any] = {}
        self.judges: list[Any] = []
        self._run_started = False
        self._run_owner: _ExecutionOwner | None = None
        self._trace_id: str | None = None
        self._local_spans: list[KensaSpan] = []
        self._attached_runs: dict[str, AgentRunEvidence] = {}
        self._local_trace_incomplete = False
        self._local_trace_incomplete_reason: str | None = None
        self._active_operations: dict[object, ActiveOperation] = {}
        self._operation_callback = operation_callback
        self._snapshot_callback = snapshot_callback
        self._engine = engine
        self._engine_evaluation_id: str | None = None
        self._engine_verdict: str | None = None

    @contextmanager
    def operation(
        self,
        name: str,
        attributes: dict[str, Any],
        *,
        kind: OperationKind = "span",
    ) -> Iterator[None]:
        token = object()
        operation = ActiveOperation(
            name=name,
            attributes=_jsonable_mapping(attributes),
            kind=kind,
        )
        self._active_operations[token] = operation
        self._publish_active_operation(operation)
        try:
            yield
        finally:
            self._active_operations.pop(token)
            active = next(reversed(self._active_operations.values()), None)
            self._publish_active_operation(active)
            if kind == "llm" and self._trace_id is not None:
                self._flush_and_populate_trace()
                self._publish_snapshot(require_output=False)

    def _publish_active_operation(self, operation: ActiveOperation | None) -> None:
        if self._operation_callback is not None:
            self._operation_callback(operation)

    def run_case(self, case: KensaCase, operation: Callable[[], Any]) -> Any:
        if self._run_started:
            raise KensaCaseError("case.run(...) may be called at most once per trial")
        self._run_started = True
        self.case_id = case.id
        self.case = _jsonable_mapping(case.row)
        if self._engine is not None:
            try:
                engine_case = {
                    "id": case.id,
                    "input": json_value(case.input),
                    "metadata": self.case,
                }
            except (TypeError, ValueError) as exc:
                raise KensaCaseError(f"case input must be JSON-serializable: {exc}") from exc
            self._engine_evaluation_id = f"{self.nodeid}::{self.trial.id}"
            self._engine.start_case(self._engine_evaluation_id, engine_case)
        ensure_tracing()
        tracer = trace.get_tracer("kensa.pytest")
        span = tracer.start_span(
            "kensa.pytest.trial",
            context=otel_context.Context(),
            attributes={
                "kensa.case_id": case.id,
                "kensa.trial_index": self.trial.trial_index,
                "kensa.configured_trials": self.trial.configured_trials,
                "kensa.pytest_nodeid": self.nodeid,
            },
        )
        self._trace_id = f"{span.get_span_context().trace_id:032x}"
        self._activate_run()
        try:
            with trace.use_span(span, end_on_exit=False):
                result = operation()
        except BaseException as exc:
            self._deactivate_run()
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            self._flush_and_populate_trace()
            raise

        if inspect.isawaitable(result):
            self._deactivate_run()
            return self._await_result(result, span)

        self._deactivate_run()
        span.end()
        return self._record_output_and_trace(result)

    async def _await_result(self, result: Awaitable[Any], span: Any) -> Any:
        self._activate_run()
        try:
            with trace.use_span(span, end_on_exit=False):
                value = await result
        except BaseException as exc:
            self._deactivate_run()
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            self._flush_and_populate_trace()
            raise
        self._deactivate_run()
        span.end()
        return self._record_output_and_trace(value)

    def _activate_run(self) -> None:
        self._run_owner = _execution_owner()

    def _deactivate_run(self) -> None:
        self._run_owner = None

    def _owns_active_run(self) -> bool:
        if self._run_owner is None:
            return False
        owner_thread, owner_task = self._run_owner
        current_thread, current_task = _execution_owner()
        return owner_thread == current_thread and owner_task is current_task

    def attach_agent_run(self, evidence: AgentRunEvidence) -> None:
        if not self._owns_active_run():
            raise KensaCaseError("attach_agent_run() requires an active case.run() operation")
        snapshot = evidence.model_copy(deep=True)
        existing = self._attached_runs.get(snapshot.run_id)
        if existing == snapshot:
            return
        if existing is not None:
            raise KensaCaseError(
                f"attach_agent_run() received conflicting evidence for run_id {snapshot.run_id!r}"
            )
        self._attached_runs[snapshot.run_id] = snapshot
        self._refresh_trace()
        self._publish_snapshot(require_output=False)

    def _record_output_and_trace(self, value: Any) -> Any:
        try:
            self.output = json_value(value)
        except (TypeError, ValueError) as exc:
            raise KensaCaseError(f"case.run(...) output must be JSON-serializable: {exc}") from exc
        self.output_recorded = True
        self._flush_and_populate_trace()
        self._publish_snapshot()
        return value

    def _record_conversation_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.output = json_value(snapshot)
        self.output_recorded = True
        self._flush_and_populate_trace()
        self._publish_snapshot()

    def _publish_snapshot(self, *, require_output: bool = True) -> None:
        if (self.output_recorded or not require_output) and self._snapshot_callback is not None:
            self._snapshot_callback(self)

    def _start_instrumented_genai_span(self, key: tuple[int, int], span: Span) -> None:
        operation = ActiveOperation(
            name=span.name,
            attributes=_jsonable_mapping(dict(span.attributes or {})),
            kind="llm",
        )
        self._active_operations[key] = operation
        self._publish_active_operation(operation)

    def _finish_instrumented_genai_span(self, key: tuple[int, int]) -> None:
        self._active_operations.pop(key, None)
        active = next(reversed(self._active_operations.values()), None)
        self._publish_active_operation(active)
        self._local_spans = collect_spans(self._trace_id)
        self._refresh_trace()
        self._publish_snapshot(require_output=False)

    def _flush_and_populate_trace(self) -> None:
        incomplete = False
        reason: str | None = None
        provider = trace.get_tracer_provider()
        force_flush = getattr(provider, "force_flush", None)
        if callable(force_flush):
            try:
                flushed = force_flush(timeout_millis=10_000)
                if flushed is False:
                    incomplete = True
                    reason = "OpenTelemetry force_flush returned false"
            except TypeError:
                flushed = force_flush()
                if flushed is False:
                    incomplete = True
                    reason = "OpenTelemetry force_flush returned false"
            except Exception as exc:
                incomplete = True
                reason = f"OpenTelemetry force_flush failed: {exc}"
        self._local_spans = collect_spans(self._trace_id)
        self._local_trace_incomplete = incomplete
        self._local_trace_incomplete_reason = reason
        self._refresh_trace()

    def _refresh_trace(self) -> None:
        agent_runs = tuple(self._attached_runs.values())
        self.trace.replace(
            _merge_spans(self._local_spans, agent_runs),
            agent_runs=agent_runs,
            incomplete=self._local_trace_incomplete,
            incomplete_reason=self._local_trace_incomplete_reason,
        )

    def record_judge(self, result: Any) -> None:
        self.judges.append(result)
        self._publish_snapshot()

    def metadata(
        self,
        *,
        status: str,
        duration_ms: float,
        failure: TrialFailure | None = None,
    ) -> TrialMetadata:
        return TrialMetadata(
            nodeid=self.nodeid,
            group_id=self.group_id,
            case_id=self.case_id,
            trial_index=self.trial.trial_index,
            configured_trials=self.trial.configured_trials,
            status=status,
            case=self.case,
            output=self.output if self.output_recorded else None,
            failure=failure,
            duration_ms=round(duration_ms, 3),
            trace=self.trace.to_dict(),
            judges=[j.to_dict() if hasattr(j, "to_dict") else dict(j) for j in self.judges],
        )

    def finalize_engine(self, status: str, failure: TrialFailure | None) -> str:
        if self._engine is None or self._engine_evaluation_id is None:
            return status
        if self._engine_verdict is not None:
            return self._engine_verdict
        failure_payload = failure.model_dump(mode="json") if failure is not None else None
        observation_failure = (
            failure_payload
            if status == "error"
            and failure is not None
            and failure.category in {"agent", "simulator"}
            else None
        )
        observation = {
            "output": self.output if self.output_recorded else None,
            "output_recorded": self.output_recorded,
            "trace": _engine_trace(self.trace.to_dict()),
            "failure": observation_failure,
        }
        self._engine_verdict = self._engine.complete_case(
            self._engine_evaluation_id,
            observation=observation,
            status=status,
            failure=failure_payload,
        )
        return self._engine_verdict


def _engine_trace(trace_snapshot: dict[str, Any]) -> dict[str, Any]:
    from kensa.engine import _wire_json_value

    wire_trace = _wire_json_value(deepcopy(trace_snapshot))
    return cast(dict[str, Any], wire_trace)


class _RuntimeSpanProcessor(SpanProcessor):
    def __init__(self) -> None:
        self._runtimes: dict[tuple[int, int], KensaTrialRuntime] = {}
        self._lock = Lock()

    def on_start(
        self,
        span: Span,
        parent_context: otel_context.Context | None = None,
    ) -> None:
        del parent_context
        runtime = _CURRENT_RUNTIME.get()
        key = _span_key(span)
        if (
            runtime is None
            or runtime._trace_id is None
            or key is None
            or not _is_instrumented_genai_llm_span(span, trace_id=runtime._trace_id)
        ):
            return
        runtime._start_instrumented_genai_span(key, span)
        with self._lock:
            self._runtimes[key] = runtime

    def on_end(self, span: ReadableSpan) -> None:
        key = _span_key(span)
        with self._lock:
            runtime = self._runtimes.pop(key, None) if key is not None else None
        if runtime is not None and key is not None:
            runtime._finish_instrumented_genai_span(key)


def ensure_tracing() -> None:
    global _EXPORTER, _PROVIDER_READY
    if _PROVIDER_READY:
        return
    _PROVIDER_READY = True
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(_RuntimeSpanProcessor())
    provider_for_exporter: Any = provider
    provider_for_exporter._kensa_exporter = exporter
    try:
        trace.set_tracer_provider(provider)
        _EXPORTER = exporter
    except Exception:
        _EXPORTER = getattr(trace.get_tracer_provider(), "_kensa_exporter", None)


def _merge_spans(
    local_spans: list[KensaSpan],
    agent_runs: tuple[AgentRunEvidence, ...],
) -> list[KensaSpan]:
    if not agent_runs:
        return list(local_spans)

    sortable: list[tuple[KensaSpan, int, int, int]] = [
        (span, 0, index, 0) for index, span in enumerate(local_spans)
    ]
    for run_index, run in enumerate(agent_runs):
        sortable.extend(
            (_normalize_agent_event(run, event), 1, run_index, event.sequence)
            for event in run.events
        )
    sortable.sort(key=_merged_span_sort_key)
    return [span for span, _, _, _ in sortable]


def _merged_span_sort_key(
    item: tuple[KensaSpan, int, int, int],
) -> tuple[int, int, int, int, int]:
    span, source, first_order, second_order = item
    start = span.start_time_unix_nano
    return (
        start is None,
        start if start is not None else 0,
        source,
        first_order,
        second_order,
    )


def _normalize_agent_event(run: AgentRunEvidence, event: AgentEvent) -> KensaSpan:
    attributes = deepcopy(event.attributes)
    for key in {
        "input",
        "output",
        "kensa.evidence.source",
        "kensa.agent.run_id",
        "kensa.agent.revision",
        "kensa.agent.environment",
        "kensa.agent.effects",
        "kensa.agent.event.sequence",
    }:
        attributes.pop(key, None)
    if "input" in event.model_fields_set:
        attributes["input"] = deepcopy(event.input)
    if "output" in event.model_fields_set:
        attributes["output"] = deepcopy(event.output)
    attributes.update(
        {
            "kensa.evidence.source": "agent_run",
            "kensa.agent.run_id": run.run_id,
            "kensa.agent.revision": run.attestation.revision,
            "kensa.agent.environment": run.attestation.environment,
            "kensa.agent.effects": run.attestation.effects.value,
            "kensa.agent.event.sequence": event.sequence,
        }
    )
    status = {
        "completed": "ok",
        "failed": "error",
        "cancelled": "cancelled",
    }[event.status]
    return KensaSpan(
        name=event.name,
        kind=event.kind,
        tool_name=event.name if event.kind == "tool" else None,
        trace_id=run.trace.trace_id if run.trace is not None else None,
        span_id=event.id,
        parent_span_id=event.parent_id,
        start_time_unix_nano=event.started_at_ns,
        end_time_unix_nano=event.ended_at_ns,
        status=status,
        attributes=attributes,
    )


def collect_spans(trace_id: str | None) -> list[KensaSpan]:
    if not trace_id or _EXPORTER is None:
        return []
    raw_spans = _EXPORTER.get_finished_spans()
    spans: list[KensaSpan] = []
    seen_span_ids: set[str] = set()
    for raw in raw_spans:
        context = raw.get_span_context()
        if f"{context.trace_id:032x}" != trace_id:
            continue
        span_id = f"{context.span_id:016x}"
        if span_id in seen_span_ids:
            continue
        seen_span_ids.add(span_id)
        spans.append(_normalize_span(raw))
    spans.sort(key=lambda s: s.start_time_unix_nano or 0)
    return spans


def _normalize_span(raw: Any) -> KensaSpan:
    attrs = dict(getattr(raw, "attributes", None) or {})
    tool_name = (
        attrs.get("kensa.tool.name")
        or attrs.get("tool.name")
        or attrs.get("gen_ai.tool.name")
        or attrs.get("openinference.tool.name")
    )
    kind = _normalized_span_kind(attrs, tool_name=tool_name)
    parent = getattr(raw, "parent", None)
    status = getattr(getattr(raw, "status", None), "status_code", None)
    status_name = getattr(status, "name", "OK").lower()
    return KensaSpan(
        name=raw.name,
        kind=kind,
        tool_name=str(tool_name) if tool_name else None,
        trace_id=f"{raw.get_span_context().trace_id:032x}",
        span_id=f"{raw.get_span_context().span_id:016x}",
        parent_span_id=f"{parent.span_id:016x}" if parent is not None else None,
        start_time_unix_nano=getattr(raw, "start_time", None),
        end_time_unix_nano=getattr(raw, "end_time", None),
        status="error" if status_name == "error" else "ok",
        attributes={str(k): jsonable(v) for k, v in attrs.items()},
    )


def _span_key(raw: Span | ReadableSpan) -> tuple[int, int] | None:
    context = raw.get_span_context()
    return (context.trace_id, context.span_id) if context is not None else None


def _is_instrumented_genai_llm_span(
    raw: Span | ReadableSpan,
    *,
    trace_id: str,
) -> bool:
    attrs = dict(raw.attributes or {})
    context = raw.get_span_context()
    span_trace_id = f"{context.trace_id:032x}" if context is not None else None
    tool_name = (
        attrs.get("kensa.tool.name")
        or attrs.get("tool.name")
        or attrs.get("gen_ai.tool.name")
        or attrs.get("openinference.tool.name")
    )
    return (
        span_trace_id == trace_id
        and "kensa.span.kind" not in attrs
        and _normalized_span_kind(attrs, tool_name=tool_name) == "llm"
    )


def _normalized_span_kind(attrs: dict[str, Any], *, tool_name: Any) -> str:
    explicit_kind = attrs.get("kensa.span.kind")
    if explicit_kind:
        return str(explicit_kind)
    if tool_name:
        return "tool"
    operation = attrs.get("gen_ai.operation.name")
    if operation is not None:
        return "llm" if str(operation) in _GEN_AI_LLM_OPERATIONS else "span"
    if any(attribute in attrs for attribute in _GEN_AI_LLM_ATTRIBUTES):
        return "llm"
    return "span"


def _jsonable_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = dict(value)
    return {str(key): jsonable(item) for key, item in value.items()}


def set_current_runtime(runtime: KensaTrialRuntime | None) -> Any:
    return _CURRENT_RUNTIME.set(runtime)


def reset_current_runtime(token: Any) -> None:
    _CURRENT_RUNTIME.reset(token)


def current_runtime() -> KensaTrialRuntime | None:
    return _CURRENT_RUNTIME.get()


__all__ = [
    "ActiveOperation",
    "KensaSpan",
    "KensaTrace",
    "KensaTrial",
    "KensaTrialRuntime",
    "OperationKind",
    "ToolCallEvidence",
    "TrialMetadata",
    "collect_spans",
    "current_runtime",
    "ensure_tracing",
    "reset_current_runtime",
    "set_current_runtime",
]

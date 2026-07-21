"""Per-pytest-trial runtime state and trace evidence."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from kensa._serialization import json_value, jsonable
from kensa.errors import KensaCaseError

if TYPE_CHECKING:
    from kensa.case import KensaCase

_CURRENT_RUNTIME: ContextVar[KensaTrialRuntime | None] = ContextVar(
    "kensa_current_runtime", default=None
)
_EXPORTER: Any | None = None
_PROVIDER_READY = False


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
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
        value = self.attributes.get("kensa.cost_usd", self.attributes.get("cost_usd", 0.0))
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

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
            "cost_usd": self.cost_usd,
        }


class KensaTraceTools:
    """Tool-call assertions for a trial trace."""

    def __init__(self, trace: KensaTrace) -> None:
        self._trace = trace

    @property
    def names(self) -> list[str]:
        """Return observed tool names in trace order, including repeats."""
        return [span.tool_name for span in self._trace.spans if span.tool_name]

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
        self.incomplete = False
        self.incomplete_reason: str | None = None

    @property
    def tools(self) -> KensaTraceTools:
        return KensaTraceTools(self)

    @property
    def cost_usd(self) -> float:
        return round(sum(span.cost_usd for span in self.spans), 8)

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
        incomplete: bool = False,
        incomplete_reason: str | None = None,
    ) -> None:
        self.spans = spans
        self.incomplete = incomplete
        self.incomplete_reason = incomplete_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "spans": [span.to_dict() for span in self.spans],
            "tools": self.tools.names,
            "cost_usd": self.cost_usd,
            "llm_turns": self.llm_turns,
            "duration_ms": self.duration_ms,
            "incomplete": self.incomplete,
            "incomplete_reason": self.incomplete_reason,
        }


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
    error: str | None = None
    error_kind: str | None = None
    duration_ms: float = 0.0
    trace: dict[str, Any] = field(default_factory=dict)
    judges: list[dict[str, Any]] = field(default_factory=list)
    active_operation: dict[str, Any] | None = None

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
            "error": self.error,
            "error_kind": self.error_kind,
            "duration_ms": self.duration_ms,
            "trace": self.trace,
            "judges": self.judges,
            "active_operation": self.active_operation,
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
        self._trace_id: str | None = None
        self._active_operations: dict[object, ActiveOperation] = {}
        self._operation_callback = operation_callback
        self._snapshot_callback = snapshot_callback

    @contextmanager
    def operation(self, name: str, attributes: dict[str, Any]) -> Iterator[None]:
        token = object()
        operation = ActiveOperation(name=name, attributes=_jsonable_mapping(attributes))
        self._active_operations[token] = operation
        self._publish_active_operation(operation)
        try:
            yield
        finally:
            self._active_operations.pop(token)
            active = next(reversed(self._active_operations.values()), None)
            self._publish_active_operation(active)

    def _publish_active_operation(self, operation: ActiveOperation | None) -> None:
        if self._operation_callback is not None:
            self._operation_callback(operation)

    def run_case(self, case: KensaCase, operation: Callable[[], Any]) -> Any:
        if self._run_started:
            raise KensaCaseError("case.run(...) may be called at most once per trial")
        self._run_started = True
        self.case_id = case.id
        self.case = _jsonable_mapping(case.row)
        ensure_tracing()
        tracer = trace.get_tracer("kensa.pytest")
        span_cm = tracer.start_as_current_span(
            "kensa.pytest.trial",
            context=otel_context.Context(),
            attributes={
                "kensa.case_id": case.id,
                "kensa.trial_index": self.trial.trial_index,
                "kensa.configured_trials": self.trial.configured_trials,
                "kensa.pytest_nodeid": self.nodeid,
            },
        )
        span = span_cm.__enter__()
        self._trace_id = f"{span.get_span_context().trace_id:032x}"
        try:
            result = operation()
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span_cm.__exit__(type(exc), exc, exc.__traceback__)
            self._flush_and_populate_trace()
            raise

        if inspect.isawaitable(result):
            return self._await_result(result, span_cm, span)

        span_cm.__exit__(None, None, None)
        return self._record_output_and_trace(result)

    async def _await_result(self, result: Awaitable[Any], span_cm: Any, span: Any) -> Any:
        try:
            value = await result
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span_cm.__exit__(type(exc), exc, exc.__traceback__)
            self._flush_and_populate_trace()
            raise
        span_cm.__exit__(None, None, None)
        return self._record_output_and_trace(value)

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

    def _publish_snapshot(self) -> None:
        if self.output_recorded and self._snapshot_callback is not None:
            self._snapshot_callback(self)

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
        spans = collect_spans(self._trace_id)
        self.trace.replace(spans, incomplete=incomplete, incomplete_reason=reason)

    def record_judge(self, result: Any) -> None:
        self.judges.append(result)
        self._publish_snapshot()

    def metadata(
        self,
        *,
        status: str,
        duration_ms: float,
        error: str | None = None,
        error_kind: str | None = None,
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
            error=error,
            error_kind=error_kind,
            duration_ms=round(duration_ms, 3),
            trace=self.trace.to_dict(),
            judges=[j.to_dict() if hasattr(j, "to_dict") else dict(j) for j in self.judges],
        )


def ensure_tracing() -> None:
    global _EXPORTER, _PROVIDER_READY
    if _PROVIDER_READY:
        return
    _PROVIDER_READY = True
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider_for_exporter: Any = provider
    provider_for_exporter._kensa_exporter = exporter
    try:
        trace.set_tracer_provider(provider)
        _EXPORTER = exporter
    except Exception:
        _EXPORTER = getattr(trace.get_tracer_provider(), "_kensa_exporter", None)


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
    kind = str(attrs.get("kensa.span.kind") or ("tool" if tool_name else "span"))
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
    "TrialMetadata",
    "collect_spans",
    "current_runtime",
    "ensure_tracing",
    "reset_current_runtime",
    "set_current_runtime",
]

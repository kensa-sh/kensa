from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

import kensa
import kensa.pytest as kensa_pytest
import kensa.runtime as runtime_module
import kensa.target as kensa_target
from kensa.case import kensa_case
from kensa.errors import KensaCaseError
from kensa.runtime import (
    KensaSpan,
    KensaTrial,
    KensaTrialRuntime,
    reset_current_runtime,
    set_current_runtime,
)
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


def _event(
    *,
    event_id: str = "event-1",
    sequence: int = 1,
    kind: str = "llm",
    name: str = "agent.llm",
    status: str = "completed",
    **values: Any,
) -> AgentEvent:
    return AgentEvent.model_validate(
        {
            "id": event_id,
            "sequence": sequence,
            "kind": kind,
            "name": name,
            "status": status,
            **values,
        }
    )


def _evidence(
    *,
    run_id: str = "run-1",
    events: tuple[AgentEvent, ...] | None = None,
    state: tuple[StateObservation, ...] = (),
    trajectory_completeness: str = "complete",
    state_completeness: str = "complete",
    incomplete_reason: str | None = None,
    trace: TraceReference | None = None,
    effects: str = "captured",
) -> AgentRunEvidence:
    return AgentRunEvidence.model_validate(
        {
            "run_id": run_id,
            "attestation": {
                "revision": "revision-1",
                "environment": "test",
                "effects": effects,
            },
            "events": events if events is not None else (_event(),),
            "trace": trace,
            "trajectory_completeness": trajectory_completeness,
            "state": state,
            "state_completeness": state_completeness,
            "incomplete_reason": incomplete_reason,
        }
    )


def _runtime(
    *,
    snapshots: list[dict[str, Any]] | None = None,
) -> KensaTrialRuntime:
    return KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="tests/test_target.py::test_external",
        group_id="external",
        case_id="external",
        no_judge=True,
        snapshot_callback=(
            None if snapshots is None else lambda current: snapshots.append(current.trace.to_dict())
        ),
    )


def test_external_evidence_models_enforce_the_v1_contract() -> None:
    event = AgentEvent.model_validate(
        {
            "id": " event-1 ",
            "parent_id": " parent ",
            "sequence": 1,
            "kind": "tool",
            "name": " lookup ",
            "input": {"ids": [1, 2]},
            "output": {"found": True},
            "attributes": {"nested": {"value": None}},
            "status": "completed",
            "started_at_ns": 10,
            "ended_at_ns": 20,
        }
    )
    observation = StateObservation(
        name=" account ",
        value={"status": "active"},
        source=" database ",
        observed_at_ns=30,
    )
    evidence = AgentRunEvidence(
        run_id=" run-1 ",
        attestation=ExecutionAttestation(
            revision=" abc123 ",
            environment=" staging ",
            effects=EffectPolicy.SANDBOXED,
        ),
        events=(event,),
        trace=TraceReference(
            provider=" langfuse ",
            trace_id=" trace-1 ",
            url="https://example.test/trace-1",
        ),
        trajectory_completeness=EvidenceCompleteness.COMPLETE,
        state=(observation,),
        state_completeness=EvidenceCompleteness.COMPLETE,
    )

    assert evidence.model_dump(mode="json") == {
        "schema_version": "kensa.agent_run.v1",
        "run_id": "run-1",
        "attestation": {
            "revision": "abc123",
            "environment": "staging",
            "effects": "sandboxed",
        },
        "events": [
            {
                "id": "event-1",
                "parent_id": "parent",
                "sequence": 1,
                "kind": "tool",
                "name": "lookup",
                "input": {"ids": [1, 2]},
                "output": {"found": True},
                "attributes": {"nested": {"value": None}},
                "status": "completed",
                "started_at_ns": 10,
                "ended_at_ns": 20,
            }
        ],
        "trace": {
            "provider": "langfuse",
            "trace_id": "trace-1",
            "url": "https://example.test/trace-1",
        },
        "trajectory_completeness": "complete",
        "state": [
            {
                "name": "account",
                "value": {"status": "active"},
                "source": "database",
                "observed_at_ns": 30,
            }
        ],
        "state_completeness": "complete",
        "incomplete_reason": None,
    }
    assert (
        AgentRunEvidence(
            run_id="empty",
            attestation=evidence.attestation,
            trajectory_completeness=EvidenceCompleteness.COMPLETE,
            state_completeness=EvidenceCompleteness.COMPLETE,
        ).events
        == ()
    )

    models_and_fields: list[tuple[BaseModel, str, Any]] = [
        (cast(BaseModel, evidence.trace), "provider", "other"),
        (evidence.attestation, "revision", "other"),
        (event, "name", "other"),
        (observation, "source", "other"),
        (evidence, "run_id", "other"),
    ]
    for model, field, value in models_and_fields:
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["str_strip_whitespace"] is True
        assert model.model_config["allow_inf_nan"] is False
        with pytest.raises(ValidationError, match="frozen"):
            setattr(model, field, value)
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            type(model).model_validate({**model.model_dump(), "unknown": True})

    invalid_identities: list[tuple[type[BaseModel], dict[str, Any], str]] = [
        (TraceReference, {"provider": " ", "trace_id": "trace"}, "provider"),
        (TraceReference, {"provider": "provider", "trace_id": " "}, "trace_id"),
        (
            ExecutionAttestation,
            {"revision": " ", "environment": "test", "effects": "none"},
            "revision",
        ),
        (
            ExecutionAttestation,
            {"revision": "rev", "environment": " ", "effects": "none"},
            "environment",
        ),
        (
            AgentEvent,
            {
                "id": " ",
                "sequence": 1,
                "kind": "span",
                "name": "name",
                "status": "completed",
            },
            "id",
        ),
        (
            AgentEvent,
            {
                "id": "id",
                "parent_id": " ",
                "sequence": 1,
                "kind": "span",
                "name": "name",
                "status": "completed",
            },
            "parent_id",
        ),
        (
            AgentEvent,
            {
                "id": "id",
                "sequence": 1,
                "kind": "span",
                "name": " ",
                "status": "completed",
            },
            "name",
        ),
        (
            StateObservation,
            {"name": " ", "value": None, "source": "database"},
            "name",
        ),
        (
            StateObservation,
            {"name": "account", "value": None, "source": " "},
            "source",
        ),
        (
            AgentRunEvidence,
            {
                "run_id": " ",
                "attestation": evidence.attestation,
                "trajectory_completeness": "complete",
                "state_completeness": "complete",
            },
            "run_id",
        ),
    ]
    for model_type, payload, field in invalid_identities:
        with pytest.raises(ValidationError, match=field):
            model_type.model_validate(payload)

    invalid_events = [
        {"sequence": True},
        {"sequence": -1},
        {"started_at_ns": True},
        {"started_at_ns": -1},
        {"ended_at_ns": True},
        {"ended_at_ns": -1},
        {"kind": "unknown"},
        {"status": "unknown"},
        {"input": object()},
        {"output": float("inf")},
        {"attributes": {"value": float("nan")}},
    ]
    base_event = _event().model_dump()
    for replacement in invalid_events:
        with pytest.raises(ValidationError):
            AgentEvent.model_validate({**base_event, **replacement})

    for replacement in [{"observed_at_ns": True}, {"observed_at_ns": -1}, {"value": {1, 2}}]:
        with pytest.raises(ValidationError):
            StateObservation.model_validate(
                {
                    "name": "state",
                    "value": None,
                    "source": "database",
                    **replacement,
                }
            )

    base_run = evidence.model_dump()
    invalid_runs = [
        {
            **base_run,
            "events": [
                _event(event_id="a", sequence=2).model_dump(),
                _event(event_id="b", sequence=1).model_dump(),
            ],
        },
        {
            **base_run,
            "events": [
                _event(event_id="same", sequence=1).model_dump(),
                _event(event_id="same", sequence=2).model_dump(),
            ],
        },
        {
            **base_run,
            "events": [
                _event(event_id="self", sequence=1, parent_id="self").model_dump(),
            ],
        },
        {
            **base_run,
            "events": [
                {
                    **base_event,
                    "id": "reversed",
                    "started_at_ns": 20,
                    "ended_at_ns": 10,
                },
            ],
        },
        {
            **base_run,
            "trajectory_completeness": "partial",
            "incomplete_reason": None,
        },
        {
            **base_run,
            "state_completeness": "pending",
            "incomplete_reason": " ",
        },
        {
            **base_run,
            "incomplete_reason": "unexpected",
        },
        {
            **base_run,
            "trajectory_completeness": "unknown",
        },
        {
            **base_run,
            "schema_version": "kensa.agent_run.v2",
        },
    ]
    for payload in invalid_runs:
        with pytest.raises(ValidationError):
            AgentRunEvidence.model_validate(payload)

    partial_parent = _evidence(
        events=(_event(parent_id="outside-the-supplied-events"),),
        trajectory_completeness="unavailable",
        incomplete_reason="provider omitted the rest",
    )
    assert partial_parent.events[0].parent_id == "outside-the-supplied-events"


@pytest.mark.asyncio
async def test_attach_agent_run_requires_only_an_active_case_operation() -> None:
    assert attach_agent_run(_evidence()) is None

    sync_runtime = _runtime()
    sync_evidence = _evidence(run_id="sync")
    token = set_current_runtime(sync_runtime)
    try:
        with pytest.raises(KensaCaseError, match=r"active case\.run"):
            attach_agent_run(sync_evidence)

        def sync_operation() -> str:
            assert attach_agent_run(sync_evidence) is None
            return "done"

        assert (
            sync_runtime.run_case(
                kensa_case(id="sync", input="hello"),
                sync_operation,
            )
            == "done"
        )
        with pytest.raises(KensaCaseError, match=r"active case\.run"):
            attach_agent_run(_evidence(run_id="after-sync"))
    finally:
        reset_current_runtime(token)

    async_runtime = _runtime()
    async_evidence = _evidence(run_id="async")
    release_late_task = asyncio.Event()
    late_errors: list[BaseException] = []
    late_tasks: list[asyncio.Task[None]] = []

    async def late_attach() -> None:
        await release_late_task.wait()
        try:
            attach_agent_run(_evidence(run_id="late"))
        except BaseException as exc:
            late_errors.append(exc)

    async def async_operation() -> str:
        late_tasks.append(asyncio.create_task(late_attach()))
        await asyncio.sleep(0)
        assert attach_agent_run(async_evidence) is None
        return "done"

    token = set_current_runtime(async_runtime)
    try:
        assert (
            await async_runtime.run_case(
                kensa_case(id="async", input="hello"),
                async_operation,
            )
            == "done"
        )
        release_late_task.set()
        await late_tasks[0]
    finally:
        reset_current_runtime(token)

    assert len(late_errors) == 1
    assert isinstance(late_errors[0], KensaCaseError)
    assert [run.run_id for run in async_runtime.trace.agent_runs] == ["async"]


def test_attachment_snapshots_and_deduplicates_run_identity() -> None:
    snapshots: list[dict[str, Any]] = []
    runtime = _runtime(snapshots=snapshots)
    evidence = _evidence(
        events=(
            _event(
                attributes={"nested": {"values": ["original"]}},
                input={"messages": ["hello"]},
            ),
        )
    )
    trace_identity = runtime.trace

    def operation() -> str:
        attach_agent_run(evidence)
        nested = cast(dict[str, Any], evidence.events[0].attributes["nested"])
        cast(list[str], nested["values"]).append("mutated")
        event_input = cast(dict[str, Any], evidence.events[0].input)
        cast(list[str], event_input["messages"]).append("mutated")
        attach_agent_run(
            _evidence(
                events=(
                    _event(
                        attributes={"nested": {"values": ["original"]}},
                        input={"messages": ["hello"]},
                    ),
                )
            )
        )
        with pytest.raises(KensaCaseError, match="run_id"):
            attach_agent_run(_evidence(events=(_event(name="conflicting"),)))
        return "done"

    token = set_current_runtime(runtime)
    try:
        runtime.run_case(kensa_case(id="snapshot", input="hello"), operation)
    finally:
        reset_current_runtime(token)

    assert runtime.trace is trace_identity
    assert len(runtime.trace.agent_runs) == 1
    stored_event = runtime.trace.agent_runs[0].events[0]
    assert stored_event.attributes == {"nested": {"values": ["original"]}}
    assert stored_event.input == {"messages": ["hello"]}
    assert [snapshot["agent_runs"][0]["run_id"] for snapshot in snapshots] == [
        "run-1",
        "run-1",
    ]


def test_external_events_normalize_and_merge_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_spans = [
        KensaSpan(name="local-known", start_time_unix_nano=5),
        KensaSpan(name="local-missing"),
    ]
    monkeypatch.setattr(runtime_module, "collect_spans", lambda trace_id: local_spans)
    events = (
        _event(
            event_id="llm",
            sequence=1,
            kind="llm",
            name="external.llm",
            status="completed",
            started_at_ns=5,
            ended_at_ns=6,
            input={"prompt": "hello"},
            output={"text": "done"},
            attributes={
                "input": "spoofed",
                "output": "spoofed",
                "kensa.evidence.source": "spoofed",
                "kensa.agent.run_id": "spoofed",
                "kensa.agent.revision": "spoofed",
                "kensa.agent.environment": "spoofed",
                "kensa.agent.effects": "spoofed",
                "kensa.agent.event.sequence": 999,
                "kensa.cost_usd": 0.25,
                "custom": [1, 2],
            },
        ),
        _event(
            event_id="tool",
            parent_id="outside",
            sequence=2,
            kind="tool",
            name="lookup",
            status="failed",
            started_at_ns=5,
        ),
        _event(
            event_id="handoff",
            sequence=3,
            kind="handoff",
            name="handoff",
            status="cancelled",
            started_at_ns=7,
        ),
        _event(
            event_id="retrieval",
            sequence=4,
            kind="retrieval",
            name="retrieval",
        ),
        _event(event_id="action", sequence=5, kind="action", name="action"),
        _event(event_id="state", sequence=6, kind="state", name="state-transition"),
        _event(
            event_id="span",
            sequence=7,
            kind="span",
            name="external.span",
            started_at_ns=8,
        ),
    )
    traced = _evidence(
        events=events,
        trace=TraceReference(provider="provider", trace_id="external-trace"),
        effects="live",
    )
    sparse = _evidence(
        run_id="run-2",
        events=(
            _event(
                event_id="sparse",
                sequence=1,
                kind="span",
                name="sparse",
                parent_id="missing",
            ),
        ),
    )
    runtime = _runtime()
    trace_identity = runtime.trace

    def operation() -> str:
        attach_agent_run(traced)
        attach_agent_run(sparse)
        runtime._refresh_trace()
        return "done"

    token = set_current_runtime(runtime)
    try:
        runtime.run_case(kensa_case(id="merge", input="hello"), operation)
    finally:
        reset_current_runtime(token)

    assert runtime.trace is trace_identity
    assert [span.name for span in runtime.trace.spans] == [
        "local-known",
        "external.llm",
        "lookup",
        "handoff",
        "external.span",
        "local-missing",
        "retrieval",
        "action",
        "state-transition",
        "sparse",
    ]
    normalized = {span.span_id: span for span in runtime.trace.spans if span.span_id is not None}
    assert normalized["llm"].status == "ok"
    assert normalized["tool"].status == "error"
    assert normalized["handoff"].status == "cancelled"
    assert normalized["tool"].tool_name == "lookup"
    assert normalized["llm"].tool_name is None
    assert normalized["tool"].parent_span_id == "outside"
    assert normalized["sparse"].trace_id is None
    assert normalized["sparse"].start_time_unix_nano is None
    assert normalized["sparse"].end_time_unix_nano is None
    assert normalized["llm"].attributes == {
        "input": {"prompt": "hello"},
        "output": {"text": "done"},
        "kensa.evidence.source": "agent_run",
        "kensa.agent.run_id": "run-1",
        "kensa.agent.revision": "revision-1",
        "kensa.agent.environment": "test",
        "kensa.agent.effects": "live",
        "kensa.agent.event.sequence": 1,
        "kensa.cost_usd": 0.25,
        "custom": [1, 2],
    }
    assert normalized["action"].attributes.get("input") is None
    assert normalized["action"].attributes.get("output") is None
    assert len(normalized) == 8


def test_state_and_completeness_remain_run_evidence() -> None:
    evidence = _evidence(
        events=(_event(kind="state", name="reported-transition"),),
        state=(
            StateObservation(
                name="account",
                value={"status": "active"},
                source="database",
                observed_at_ns=50,
            ),
        ),
        trajectory_completeness="partial",
        state_completeness="unavailable",
        incomplete_reason="provider omitted some evidence",
    )
    runtime = _runtime()

    token = set_current_runtime(runtime)
    try:
        runtime.run_case(
            kensa_case(id="state", input="hello"),
            lambda: (attach_agent_run(evidence), "done")[1],
        )
    finally:
        reset_current_runtime(token)

    assert runtime.trace.incomplete is False
    assert runtime.trace.incomplete_reason is None
    assert [span.name for span in runtime.trace.spans if span.kind == "state"] == [
        "reported-transition"
    ]
    assert runtime.trace.agent_runs[0].state == evidence.state
    assert runtime.trace.agent_runs[0].trajectory_completeness == EvidenceCompleteness.PARTIAL
    assert runtime.trace.agent_runs[0].state_completeness == EvidenceCompleteness.UNAVAILABLE
    assert runtime.metadata(status="pass", duration_ms=1).status == "pass"


def test_external_events_feed_existing_trace_accessors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = KensaSpan(
        name="local.llm",
        kind="llm",
        start_time_unix_nano=1,
        end_time_unix_nano=3,
        attributes={"kensa.cost_usd": 0.5},
    )
    monkeypatch.setattr(runtime_module, "collect_spans", lambda trace_id: [local])
    evidence = _evidence(
        events=(
            _event(
                event_id="priced",
                sequence=1,
                kind="llm",
                name="priced.llm",
                started_at_ns=4,
                ended_at_ns=6,
                attributes={"kensa.cost_usd": 0.25},
            ),
            _event(
                event_id="unpriced",
                sequence=2,
                kind="llm",
                name="unpriced.llm",
                started_at_ns=7,
                ended_at_ns=8,
            ),
            _event(
                event_id="tool",
                sequence=3,
                kind="tool",
                name="external-tool",
                started_at_ns=9,
                ended_at_ns=10,
            ),
        ),
    )
    runtime = _runtime()

    token = set_current_runtime(runtime)
    try:
        runtime.run_case(
            kensa_case(id="accessors", input="hello"),
            lambda: (attach_agent_run(evidence), "done")[1],
        )
    finally:
        reset_current_runtime(token)

    assert runtime.trace.tools.names == ["external-tool"]
    assert runtime.trace.llm_turns == 3
    assert runtime.trace.known_cost_usd == 0.75
    assert runtime.trace.cost_available is False
    assert runtime.trace.cost_usd is None
    assert runtime.trace.duration_ms == pytest.approx(0.000009)


def test_external_evidence_public_api_is_target_scoped() -> None:
    expected = [
        "AgentEvent",
        "AgentRunEvidence",
        "EffectPolicy",
        "EvidenceCompleteness",
        "ExecutionAttestation",
        "StateObservation",
        "TraceReference",
        "attach_agent_run",
    ]
    assert runtime_module.current_runtime is not None
    assert kensa_target.__all__ == expected
    assert all(hasattr(kensa_pytest, name) for name in expected)
    assert all(name not in kensa.__all__ for name in expected)

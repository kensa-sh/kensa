from __future__ import annotations

import json
import ssl
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpcore
import httpx
import pytest
from conftest import FakeRedactionEnv
from langfuse.api.commons.types.observation_v2 import ObservationV2
from langfuse.api.core import ApiError

from kensa.providers import langfuse as provider
from kensa.traces import import_trace_records

_Responses = list[Any]
_Payload = dict[str, Any]


class _FakeProjectsClient:
    def __init__(self, responses: _Responses) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _pop_response(self.responses)


class _FakeTraceClient:
    def __init__(self, responses: _Responses) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _pop_response(self.responses)


class _FakeObservationsClient:
    def __init__(self, responses: _Responses) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get_many(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _pop_response(self.responses)


class _FakeLegacyApi:
    def __init__(self, observation_responses: list[Any]) -> None:
        self.observations_v1 = _FakeObservationsClient(observation_responses)


class _FakeApi:
    def __init__(
        self,
        *,
        project_responses: list[Any] | None = None,
        trace_responses: list[Any] | None = None,
        observation_responses: list[Any] | None = None,
    ) -> None:
        self.projects = _FakeProjectsClient(project_responses or [])
        self.trace = _FakeTraceClient(trace_responses or [])
        self.observations = _FakeObservationsClient(observation_responses or [])
        self.legacy = _FakeLegacyApi(list(observation_responses or []))


class _FakeLangfuse:
    def __init__(
        self,
        *,
        project_responses: list[Any] | None = None,
        trace_responses: list[Any] | None = None,
        observation_responses: list[Any] | None = None,
    ) -> None:
        self.api = _FakeApi(
            project_responses=project_responses,
            trace_responses=trace_responses,
            observation_responses=observation_responses,
        )
        self.constructor_calls: list[dict[str, Any]] = []
        self.api_constructor_calls: list[dict[str, Any]] = []
        self.shutdown_calls = 0

    def constructor(self, **kwargs: Any) -> _FakeLangfuse:
        self.constructor_calls.append(kwargs)
        return self

    def api_constructor(self, **kwargs: Any) -> _FakeApi:
        self.api_constructor_calls.append(kwargs)
        return self.api

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _RecordingNetworkStream(httpcore.NetworkStream):
    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.read_limits: list[int] = []
        self.writes: list[bytes] = []
        self.closed = False
        self.tls_calls: list[tuple[ssl.SSLContext, str | None, float | None]] = []

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.read_limits.append(max_bytes)
        data, self.payload = self.payload[:max_bytes], self.payload[max_bytes:]
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    def close(self) -> None:
        self.closed = True

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self.tls_calls.append((ssl_context, server_hostname, timeout))
        return self

    def get_extra_info(self, info: str) -> Any:
        return {"kind": "recording"}.get(info)


class _RecordingNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, stream: _RecordingNetworkStream) -> None:
        self.stream = stream
        self.tcp_calls: list[tuple[Any, ...]] = []
        self.unix_calls: list[tuple[Any, ...]] = []
        self.sleeps: list[float] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        self.tcp_calls.append((host, port, timeout, local_address, socket_options))
        return self.stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        self.unix_calls.append((path, timeout, socket_options))
        return self.stream

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _pop_response(responses: list[Any]) -> Any:
    response = responses.pop(0)
    if isinstance(response, BaseException):
        raise response
    return response


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeLangfuse) -> None:
    monkeypatch.setattr(provider, "Langfuse", fake.constructor)
    monkeypatch.setattr(provider, "LangfuseAPI", fake.api_constructor)


def _api_error(status_code: int, body: Any = None) -> ApiError:
    return ApiError(status_code=status_code, body=body)


def _request_options(call: dict[str, Any]) -> dict[str, Any]:
    value = call["request_options"]
    assert isinstance(value, dict)
    return value


def _sdk_httpx_client(api: Any) -> httpx.Client:
    return cast(httpx.Client, api._client_wrapper.httpx_client.httpx_client)


def test_check_langfuse_connection_uses_projects_get(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLangfuse(project_responses=[{"data": [], "meta": {}}])
    _install_fake_client(monkeypatch, fake)

    provider.check_langfuse_connection(
        endpoint="https://langfuse.example.com/",
        public_key="public",
        secret_key="secret",
    )

    assert fake.constructor_calls == [
        {
            "base_url": "https://langfuse.example.com",
            "public_key": "public",
            "secret_key": "secret",
            "tracing_enabled": False,
            "timeout": 30,
        }
    ]
    assert fake.api.projects.calls == [
        {"request_options": {"timeout_in_seconds": 30, "max_retries": 3}}
    ]


def test_legacy_traces_uses_sdk_pagination_and_returns_existing_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        trace_responses=[
            {
                "data": [{"id": "tr_1", "name": "first"}],
                "meta": {"page": 1, "limit": 1, "totalPages": 2},
            },
            {
                "data": [{"traceId": "tr_2", "name": "second"}],
                "meta": {"page": 2, "limit": 1, "totalPages": 2},
            },
        ],
        observation_responses=[
            {
                "data": [{"id": "obs_1", "traceId": "tr_1", "type": "SPAN"}],
                "meta": {"page": 1, "totalPages": 2},
            },
            {
                "data": [{"id": "obs_2", "traceId": "tr_1", "type": "GENERATION"}],
                "meta": {"page": 2, "totalPages": 2},
            },
            {
                "data": [{"id": "obs_3", "traceId": "tr_2", "type": "SPAN"}],
                "meta": {"page": 1, "totalPages": 1},
            },
        ],
    )
    _install_fake_client(monkeypatch, fake)

    payload = provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since="2026-06-01T00:00:00Z",
        limit=2,
        import_mode="legacy_traces",
    )

    assert payload == {
        "traces": [{"id": "tr_1", "name": "first"}, {"traceId": "tr_2", "name": "second"}],
        "observations": [
            {"id": "obs_1", "traceId": "tr_1", "type": "SPAN"},
            {"id": "obs_2", "traceId": "tr_1", "type": "GENERATION"},
            {"id": "obs_3", "traceId": "tr_2", "type": "SPAN"},
        ],
        "meta": {"page": 2, "limit": 1, "totalPages": 2},
    }
    assert fake.api.trace.calls[0]["page"] == 1
    assert fake.api.trace.calls[0]["limit"] == 2
    assert fake.api.trace.calls[0]["fields"] == "core,io"
    assert fake.api.trace.calls[0]["from_timestamp"] == datetime(2026, 6, 1, tzinfo=UTC)
    assert fake.api.trace.calls[1]["page"] == 2
    assert fake.api.legacy.observations_v1.calls == [
        {
            "page": 1,
            "trace_id": "tr_1",
            "limit": 100,
            "request_options": {"timeout_in_seconds": 30, "max_retries": 3},
        },
        {
            "page": 2,
            "trace_id": "tr_1",
            "limit": 100,
            "request_options": {"timeout_in_seconds": 30, "max_retries": 3},
        },
        {
            "page": 1,
            "trace_id": "tr_2",
            "limit": 100,
            "request_options": {"timeout_in_seconds": 30, "max_retries": 3},
        },
    ]


def test_observations_v2_discovers_trace_ids_and_refetches_full_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "obs_1", "traceId": "tr_1"}],
                "meta": {"cursor": "next-page"},
            },
            {
                "data": [{"id": "obs_2", "traceId": "tr_2"}],
                "meta": {"cursor": None},
            },
            {
                "data": [{"id": "obs_1", "traceId": "tr_1", "input": json.dumps({"q": "hi"})}],
                "meta": {"cursor": None},
            },
            {
                "data": [{"id": "obs_2", "traceId": "tr_2", "output": json.dumps(["bye"])}],
                "meta": {"cursor": None},
            },
        ],
    )
    _install_fake_client(monkeypatch, fake)

    payload = provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since="2026-06-01T00:00:00Z",
        limit=2,
        import_mode="observations_v2",
    )

    assert payload == {
        "data": [
            {"id": "obs_1", "traceId": "tr_1", "input": {"q": "hi"}},
            {"id": "obs_2", "traceId": "tr_2", "output": ["bye"]},
        ],
        "meta": {},
    }
    first_call, second_call, first_refetch, second_refetch = fake.api.observations.calls
    assert first_call["fields"] == "core"
    assert first_call["limit"] == 2
    assert first_call["cursor"] is None
    assert first_call["from_start_time"] == datetime(2026, 6, 1, tzinfo=UTC)
    assert "parse_io_as_json" not in first_call
    assert second_call["fields"] == "core"
    assert second_call["cursor"] == "next-page"
    assert first_refetch["trace_id"] == "tr_1"
    assert first_refetch["fields"] == provider._OBSERVATIONS_V2_FIELDS
    assert second_refetch["trace_id"] == "tr_2"


def test_observations_v2_limit_counts_traces_not_observation_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [
                    {"id": "obs_1", "traceId": "tr_1"},
                    {"id": "obs_2", "traceId": "tr_2"},
                    {"id": "obs_3", "traceId": "tr_3"},
                ],
                "meta": {"cursor": None},
            },
            {
                "data": [
                    {"id": "obs_1a", "traceId": "tr_1"},
                    {"id": "obs_1b", "traceId": "tr_1"},
                ],
                "meta": {"cursor": None},
            },
            {
                "data": [
                    {"id": "obs_2a", "traceId": "tr_2"},
                    {"id": "obs_2b", "traceId": "tr_2"},
                ],
                "meta": {"cursor": None},
            },
        ],
    )
    _install_fake_client(monkeypatch, fake)

    payload = provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since=None,
        limit=2,
        import_mode="observations_v2",
    )

    assert [row["traceId"] for row in payload["data"]] == ["tr_1", "tr_1", "tr_2", "tr_2"]
    assert [call.get("trace_id") for call in fake.api.observations.calls[1:]] == ["tr_1", "tr_2"]


def test_observations_v2_io_parsing_keeps_raw_and_scalar_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
            {
                "data": [
                    {
                        "id": "obs_1",
                        "traceId": "tr_1",
                        "input": json.dumps([{"role": "user"}]),
                        "output": json.dumps({"answer": "Done"}),
                    },
                    {
                        "id": "obs_2",
                        "traceId": "tr_1",
                        "input": "raw prompt",
                        "output": json.dumps("plain output"),
                    },
                    {
                        "id": "obs_3",
                        "traceId": "tr_1",
                        "input": {"already": "structured"},
                        "output": ["done"],
                    },
                ],
                "meta": {"cursor": None},
            },
        ],
    )
    _install_fake_client(monkeypatch, fake)

    payload = provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since=None,
        limit=1,
        import_mode="observations_v2",
    )

    rows = payload["data"]
    assert rows[0]["input"] == [{"role": "user"}]
    assert rows[0]["output"] == {"answer": "Done"}
    assert rows[1]["input"] == "raw prompt"
    assert rows[1]["output"] == json.dumps("plain output")
    assert rows[2]["input"] == {"already": "structured"}
    assert rows[2]["output"] == ["done"]


def test_sdk_to_plain_preserves_langfuse_api_aliases() -> None:
    observation = ObservationV2.model_construct(
        id="obs_1",
        trace_id="tr_1",
        parent_observation_id="obs_parent",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        trace_name="agent",
        session_id="session_1",
    )

    plain = provider.sdk_to_plain({"rows": [observation], "none": None})

    assert plain["none"] is None
    assert plain["rows"][0]["traceId"] == "tr_1"
    assert plain["rows"][0]["parentObservationId"] == "obs_parent"
    assert plain["rows"][0]["startTime"] == "2026-01-01T00:00:00Z"
    assert plain["rows"][0]["endTime"] == "2026-01-01T00:00:01Z"
    assert plain["rows"][0]["traceName"] == "agent"
    assert plain["rows"][0]["sessionId"] == "session_1"


def test_sdk_to_plain_fallback_serializers() -> None:
    class ModelDumpWithoutMode:
        def model_dump(self, **kwargs: Any) -> _Payload:
            if "mode" in kwargs:
                raise TypeError("mode unsupported")
            return {"items": ("a", "b")}

    class DictDumpWithAlias:
        def dict(self, **kwargs: Any) -> _Payload:
            assert kwargs == {"by_alias": True}
            return {"ok": True}

    class DictDumpWithoutAlias:
        def dict(self, **kwargs: Any) -> _Payload:
            if kwargs:
                raise TypeError("alias unsupported")
            return {"fallback": True}

    assert provider.sdk_to_plain(ModelDumpWithoutMode()) == {"items": ["a", "b"]}
    assert provider.sdk_to_plain(DictDumpWithAlias()) == {"ok": True}
    assert provider.sdk_to_plain(DictDumpWithoutAlias()) == {"fallback": True}


def test_malformed_since_uses_raw_sdk_query_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _FakeLangfuse(
        trace_responses=[{"data": [], "meta": {"page": 1, "totalPages": 1}}],
    )
    _install_fake_client(monkeypatch, legacy)

    provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since=" not-a-date ",
        limit=1,
        import_mode="legacy_traces",
    )

    assert legacy.api.trace.calls[0]["from_timestamp"] is None
    assert _request_options(legacy.api.trace.calls[0])["additional_query_parameters"] == {
        "fromTimestamp": "not-a-date"
    }

    observations = _FakeLangfuse(
        observation_responses=[{"data": [], "meta": {"cursor": None}}],
    )
    _install_fake_client(monkeypatch, observations)

    provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since="not-a-date",
        limit=1,
        import_mode="observations_v2",
    )

    assert observations.api.observations.calls[0]["from_start_time"] is None
    assert _request_options(observations.api.observations.calls[0])[
        "additional_query_parameters"
    ] == {"fromStartTime": "not-a-date"}


def test_naive_iso_since_is_treated_as_utc() -> None:
    since_filter = provider._provider_since_filter("2026-07-08")

    assert since_filter.parsed == datetime(2026, 7, 8, tzinfo=UTC)
    assert since_filter.raw is None


def test_auto_falls_back_only_on_trace_list_404(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = _FakeLangfuse(
        trace_responses=[_api_error(404, {"error": "events_only"})],
        observation_responses=[
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
        ],
    )
    _install_fake_client(monkeypatch, fallback)

    assert provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since=None,
        limit=1,
    ) == {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {}}
    assert len(fallback.api.trace.calls) == 1
    assert len(fallback.api.observations.calls) == 2

    for status_code in (400, 401, 403):
        no_fallback = _FakeLangfuse(trace_responses=[_api_error(status_code)])
        _install_fake_client(monkeypatch, no_fallback)
        with pytest.raises(provider.LangfuseProviderError) as exc_info:
            provider.fetch_langfuse_connected_export(
                endpoint="https://langfuse.example.com",
                public_key="public",
                secret_key="secret",
                since=None,
                limit=1,
            )
        assert exc_info.value.label == "traces"
        assert exc_info.value.status_code == status_code
        assert no_fallback.api.observations.calls == []

    observation_failure = _FakeLangfuse(
        trace_responses=[{"data": [{"id": "tr_1"}], "meta": {"totalPages": 1}}],
        observation_responses=[_api_error(404)],
    )
    _install_fake_client(monkeypatch, observation_failure)
    with pytest.raises(provider.LangfuseProviderError) as exc_info:
        provider.fetch_langfuse_connected_export(
            endpoint="https://langfuse.example.com",
            public_key="public",
            secret_key="secret",
            since=None,
            limit=1,
        )
    assert exc_info.value.label == "observations"
    assert observation_failure.api.legacy.observations_v1.calls == [
        {
            "page": 1,
            "trace_id": "tr_1",
            "limit": 100,
            "request_options": {"timeout_in_seconds": 30, "max_retries": 3},
        }
    ]


def test_auto_fallback_preserves_trace_404_as_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    broken_fallback = _FakeLangfuse(
        trace_responses=[_api_error(404, {"error": "events_only"})],
        observation_responses=[{"data": {}, "meta": {}}],
    )
    _install_fake_client(monkeypatch, broken_fallback)

    with pytest.raises(ValueError, match="data list") as exc_info:
        provider.fetch_langfuse_connected_export(
            endpoint="https://langfuse.example.com",
            public_key="public",
            secret_key="secret",
            since=None,
            limit=1,
        )

    assert isinstance(exc_info.value.__cause__, provider.LangfuseProviderError)


def test_call_sdk_wraps_transport_errors() -> None:
    with pytest.raises(provider.LangfuseProviderError) as exc_info:
        provider._call_sdk(
            lambda: (_ for _ in ()).throw(TimeoutError("timed out")),
            label="traces",
            endpoint="https://langfuse.example.com",
        )

    assert exc_info.value.status_code is None
    assert "timed out before Langfuse responded" in str(exc_info.value)


def test_call_sdk_does_not_mask_unexpected_errors() -> None:
    with pytest.raises(ValueError, match="contract drift"):
        provider._call_sdk(
            lambda: (_ for _ in ()).throw(ValueError("contract drift")),
            label="observations",
            endpoint="https://langfuse.example.com",
        )


def test_provider_errors_are_actionable_and_include_response_body() -> None:
    error = provider._provider_error_from_api_error(
        _api_error(
            400,
            {
                "message": "Invalid request",
                "error": [{"keys": ["parseIoAsJson"], "code": "unrecognized_keys"}],
            },
        ),
        label="observations",
        endpoint="https://langfuse.example.com",
    )

    assert error.status_code == 400
    assert error.label == "observations"
    assert error.response_body["message"] == "Invalid request"
    assert "Langfuse returned HTTP 400 while fetching observations." in str(error)
    assert "Langfuse response:" in str(error)
    assert "parseIoAsJson" in str(error)
    assert "Langfuse rejected request parameters" in str(error)

    trace_404 = provider._provider_error_from_api_error(
        _api_error(404, {"error": "events_only"}),
        label="traces",
        endpoint="https://langfuse.example.com",
    )
    assert "trace read API was unavailable" in str(trace_404)
    assert "events_only hint" in str(trace_404)

    auth_error = provider._provider_error_from_api_error(
        _api_error(401),
        label="projects",
        endpoint="https://langfuse.example.com",
    )
    assert "Langfuse rejected the credentials" in str(auth_error)

    transport = provider._provider_error_from_transport_error(
        TimeoutError("timed out"),
        label="traces",
        endpoint="https://langfuse.example.com",
    )
    assert "Langfuse could not be reached while fetching traces." in str(transport)
    assert "timed out before Langfuse responded" in str(transport)


def test_error_helper_branches() -> None:
    assert provider._trace_page_is_last({}, 1) is False
    assert provider._langfuse_http_error_body_hint(500, "plain error") is None
    assert provider._response_body_text(" plain\nerror ") == "plain error"
    assert provider._response_body_text({object()}) is not None
    assert provider._langfuse_response_body_hint("\x07") is None
    long_hint = provider._langfuse_response_body_hint("x" * (provider._RESPONSE_HINT_MAX_CHARS + 1))
    assert long_hint is not None
    assert long_hint.endswith("...")
    assert provider._langfuse_http_error_reason(429) == "rate limited Kensa"
    assert provider._langfuse_http_error_reason(None) == "returned an error"
    assert "Wait a minute" in provider._langfuse_http_error_next_step(429)
    assert "retry after Langfuse is healthy" in provider._langfuse_http_error_next_step(500)
    assert (
        provider._langfuse_transport_hint(Exception("name resolution failed"))
        == "Kensa could not resolve the Langfuse host."
    )
    assert (
        provider._langfuse_transport_hint(Exception("certificate verify failed"))
        == "TLS certificate verification failed for the Langfuse host."
    )
    assert (
        provider._langfuse_transport_hint(Exception("connection reset"))
        == "Kensa could not reach Langfuse."
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({"data": {}}, "data list"),
        ({"data": [1], "meta": {}}, "object rows"),
        ({"data": [], "meta": None}, "meta object"),
    ],
)
def test_envelope_validation_errors_are_explicit(payload: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        provider._langfuse_response_envelope(payload, label="observations")


def test_missing_trace_id_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLangfuse(
        trace_responses=[{"data": [{"name": "missing"}], "meta": {"totalPages": 1}}],
    )
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(ValueError, match="missing a trace id"):
        provider.fetch_langfuse_connected_export(
            endpoint="https://langfuse.example.com",
            public_key="public",
            secret_key="secret",
            since=None,
            limit=1,
            import_mode="legacy_traces",
        )


def test_fetch_rejects_unknown_import_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLangfuse()
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(ValueError, match="Unsupported Langfuse import mode"):
        provider.fetch_langfuse_connected_export(
            endpoint="https://langfuse.example.com",
            public_key="public",
            secret_key="secret",
            since=None,
            limit=1,
            import_mode=cast(Any, "bad"),
        )


def test_zero_limits_return_empty_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLangfuse()
    _install_fake_client(monkeypatch, fake)

    assert provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since=None,
        limit=0,
        import_mode="legacy_traces",
    ) == {"traces": [], "observations": [], "meta": {}}

    assert provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since=None,
        limit=0,
        import_mode="observations_v2",
    ) == {"data": [], "meta": {}}


def test_since_windows_use_native_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return cls(2026, 6, 25, 12, 0, 30, 123456, tzinfo=tz)

    fake = _FakeLangfuse(trace_responses=[{"data": [], "meta": {}}])
    _install_fake_client(monkeypatch, fake)
    monkeypatch.setattr(provider, "datetime", FixedDatetime)

    provider.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since="7d",
        limit=1,
        import_mode="legacy_traces",
    )

    assert fake.api.trace.calls[0]["from_timestamp"] == datetime(
        2026,
        6,
        18,
        12,
        0,
        30,
        tzinfo=UTC,
    )


def _batch_arguments(**overrides: Any) -> dict[str, Any]:
    return {
        "endpoint": "https://langfuse.example.com",
        "public_key": "public",
        "secret_key": "secret",
        "from_timestamp": datetime(2026, 6, 1, tzinfo=UTC),
        "to_timestamp": datetime(2026, 6, 8, tzinfo=UTC),
        "trace_limit": 2,
        "request_limit": 20,
        "response_byte_limit": 10_000,
        **overrides,
    }


def test_legacy_batch_resumes_at_complete_trace_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _FakeLangfuse(
        trace_responses=[{"data": [{"id": "tr_1", "name": "first"}], "meta": {"totalPages": 2}}],
        observation_responses=[
            {
                "data": [{"id": "obs_1", "traceId": "tr_1", "type": "SPAN"}],
                "meta": {"totalPages": 1},
            }
        ],
    )
    _install_fake_client(monkeypatch, first)

    first_batch = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(trace_limit=1, import_mode="legacy_traces"))
    )

    assert first_batch == provider.LangfuseFetchBatch(
        payload={
            "traces": [{"id": "tr_1", "name": "first"}],
            "observations": [{"id": "obs_1", "traceId": "tr_1", "type": "SPAN"}],
            "meta": {},
        },
        trace_count=1,
        complete=False,
        next_checkpoint=provider.LangfuseFetchCheckpoint(
            mode="legacy_traces",
            legacy_page=2,
        ),
    )
    trace_call = first.api.trace.calls[0]
    assert trace_call["from_timestamp"] == datetime(2026, 6, 1, tzinfo=UTC)
    assert trace_call["to_timestamp"] == datetime(2026, 6, 8, tzinfo=UTC)
    assert trace_call["order_by"] == "timestamp.asc"
    assert first.shutdown_calls == 0
    batch_http_client = first.api_constructor_calls[0]["httpx_client"]
    assert isinstance(batch_http_client, httpx.Client)
    assert batch_http_client.is_closed

    second = _FakeLangfuse(
        trace_responses=[{"data": [{"id": "tr_2", "name": "second"}], "meta": {"totalPages": 2}}],
        observation_responses=[
            {
                "data": [{"id": "obs_2", "traceId": "tr_2", "type": "GENERATION"}],
                "meta": {"totalPages": 1},
            }
        ],
    )
    _install_fake_client(monkeypatch, second)

    second_batch = provider.fetch_langfuse_connected_batch(
        **cast(
            Any,
            _batch_arguments(
                trace_limit=1,
                import_mode="legacy_traces",
                checkpoint=first_batch.next_checkpoint,
            ),
        )
    )

    assert second_batch.trace_count == 1
    assert second_batch.payload["traces"] == [{"id": "tr_2", "name": "second"}]
    assert second_batch.complete is True
    assert second_batch.next_checkpoint is None
    assert second.api.trace.calls[0]["page"] == 2


def test_real_batch_clients_bypass_langfuse_resource_cache() -> None:
    public_key = "pk-kensa-batch-isolation"
    one_shot = provider._build_client(
        endpoint="https://one-shot.example.com",
        public_key=public_key,
        secret_key="one-shot-secret",
    )
    resources = cast(Any, one_shot)._resources
    assert resources is not None

    try:
        with provider._batch_client(
            endpoint="https://batch.example.com",
            public_key=public_key,
            secret_key="batch-secret",
            request_limit=2,
            response_byte_limit=1_000,
        ) as first:
            first_api = first.api
            first_http_client = _sdk_httpx_client(first_api)
            assert first_api is not one_shot.api
            assert first_http_client is not resources.httpx_client
            assert isinstance(cast(Any, first_http_client)._transport, provider._BudgetedTransport)
            assert not first_http_client.is_closed
        assert first_http_client.is_closed

        with provider._batch_client(
            endpoint="https://batch.example.com",
            public_key=public_key,
            secret_key="batch-secret",
            request_limit=2,
            response_byte_limit=1_000,
        ) as second:
            second_http_client = _sdk_httpx_client(second.api)
            assert second.api is not first_api
            assert second_http_client is not first_http_client
            assert not second_http_client.is_closed
        assert second_http_client.is_closed
    finally:
        one_shot.shutdown()
        cast(Any, type(resources))._instances.pop(public_key, None)


def test_observations_batch_resumes_inside_discovery_page_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = {
        "data": [
            {"id": "discovery_1", "traceId": "tr_1"},
            {"id": "discovery_2", "traceId": "tr_2"},
        ],
        "meta": {"cursor": None},
    }
    first = _FakeLangfuse(
        observation_responses=[
            discovery,
            {
                "data": [
                    {
                        "id": "obs_1",
                        "traceId": "tr_1",
                        "input": json.dumps({"prompt": "hello"}),
                    }
                ],
                "meta": {"cursor": None},
            },
        ]
    )
    _install_fake_client(monkeypatch, first)

    first_batch = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(trace_limit=1, import_mode="observations_v2"))
    )

    assert first_batch.payload == {
        "data": [{"id": "obs_1", "traceId": "tr_1", "input": {"prompt": "hello"}}],
        "meta": {},
    }
    assert first_batch.next_checkpoint == provider.LangfuseFetchCheckpoint(
        mode="observations_v2",
        observations_position=1,
        observations_seen_trace_ids=("tr_1",),
    )
    first_discovery_call = first.api.observations.calls[0]
    assert first_discovery_call["from_start_time"] == datetime(2026, 6, 1, tzinfo=UTC)
    assert first_discovery_call["to_start_time"] == datetime(2026, 6, 8, tzinfo=UTC)

    second = _FakeLangfuse(
        observation_responses=[
            discovery,
            {
                "data": [{"id": "obs_2", "traceId": "tr_2", "output": json.dumps(["done"])}],
                "meta": {"cursor": None},
            },
        ]
    )
    _install_fake_client(monkeypatch, second)

    second_batch = provider.fetch_langfuse_connected_batch(
        **cast(
            Any,
            _batch_arguments(
                trace_limit=1,
                checkpoint=first_batch.next_checkpoint,
            ),
        )
    )

    assert second_batch.payload == {
        "data": [{"id": "obs_2", "traceId": "tr_2", "output": ["done"]}],
        "meta": {},
    }
    assert second_batch.complete is True
    assert [call.get("trace_id") for call in second.api.observations.calls] == [None, "tr_2"]


def test_observations_batch_resumes_at_next_discovery_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": "next-cursor"},
            },
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
        ]
    )
    _install_fake_client(monkeypatch, first)

    first_batch = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(trace_limit=1, import_mode="observations_v2"))
    )

    assert first_batch.next_checkpoint == provider.LangfuseFetchCheckpoint(
        mode="observations_v2",
        observations_cursor="next-cursor",
        observations_seen_trace_ids=("tr_1",),
    )

    second = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_2", "traceId": "tr_2"}],
                "meta": {"cursor": None},
            },
            {"data": [{"id": "obs_2", "traceId": "tr_2"}], "meta": {"cursor": None}},
        ]
    )
    _install_fake_client(monkeypatch, second)

    resumed = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(trace_limit=1, checkpoint=first_batch.next_checkpoint))
    )

    assert resumed.complete is True
    assert second.api.observations.calls[0]["cursor"] == "next-cursor"


def test_observations_batch_skips_trace_repeated_on_resumed_discovery_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _FakeLangfuse(
        observation_responses=[
            {
                "data": [
                    {"id": "discovery_1", "traceId": "tr_1"},
                    {"id": "discovery_2", "traceId": "tr_2"},
                ],
                "meta": {"cursor": "next-cursor"},
            },
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
            {"data": [{"id": "obs_2", "traceId": "tr_2"}], "meta": {"cursor": None}},
        ]
    )
    _install_fake_client(monkeypatch, first)

    first_batch = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(trace_limit=2, import_mode="observations_v2"))
    )

    assert first_batch.trace_count == 2
    assert first_batch.next_checkpoint == provider.LangfuseFetchCheckpoint(
        mode="observations_v2",
        observations_cursor="next-cursor",
        observations_seen_trace_ids=("tr_1", "tr_2"),
    )

    second = _FakeLangfuse(
        observation_responses=[
            {
                "data": [
                    {"id": "discovery_3", "traceId": "tr_2"},
                    {"id": "discovery_4", "traceId": "tr_3"},
                ],
                "meta": {"cursor": None},
            },
            {"data": [{"id": "obs_3", "traceId": "tr_3"}], "meta": {"cursor": None}},
        ]
    )
    _install_fake_client(monkeypatch, second)

    resumed = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(checkpoint=first_batch.next_checkpoint))
    )

    assert resumed.trace_count == 1
    assert resumed.payload["data"] == [{"id": "obs_3", "traceId": "tr_3"}]
    assert resumed.complete is True
    assert [call.get("trace_id") for call in second.api.observations.calls] == [None, "tr_3"]


def test_batch_auto_fallback_returns_active_observations_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        trace_responses=[_api_error(404, {"error": "events_only"})],
        observation_responses=[
            {
                "data": [
                    {"id": "discovery_1", "traceId": "tr_1"},
                    {"id": "discovery_2", "traceId": "tr_2"},
                ],
                "meta": {"cursor": None},
            },
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
        ],
    )
    _install_fake_client(monkeypatch, fake)

    batch = provider.fetch_langfuse_connected_batch(**cast(Any, _batch_arguments(trace_limit=1)))

    assert batch.next_checkpoint == provider.LangfuseFetchCheckpoint(
        mode="observations_v2",
        observations_position=1,
        observations_seen_trace_ids=("tr_1",),
    )
    assert len(fake.api.trace.calls) == 1


def test_observations_batch_rejects_rows_from_a_different_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": None},
            },
            {
                "data": [
                    {"id": "obs_1", "traceId": "tr_1"},
                    {"id": "obs_2", "traceId": "tr_2"},
                ],
                "meta": {"cursor": None},
            },
        ]
    )
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(ValueError, match="different trace"):
        provider.fetch_langfuse_connected_batch(
            **cast(Any, _batch_arguments(trace_limit=1, import_mode="observations_v2"))
        )


def test_observations_batch_rejects_empty_trace_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": None},
            },
            {"data": [], "meta": {"cursor": None}},
        ]
    )
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(ValueError, match="omitted the requested trace"):
        provider.fetch_langfuse_connected_batch(
            **cast(Any, _batch_arguments(trace_limit=1, import_mode="observations_v2"))
        )


def test_observations_batch_trace_count_matches_imported_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": None},
            },
            {
                "data": [
                    {
                        "id": "obs_1",
                        "traceId": "tr_1",
                        "type": "SPAN",
                        "name": "root",
                        "input": "Alice",
                    }
                ],
                "meta": {"cursor": None},
            },
        ]
    )
    _install_fake_client(monkeypatch, fake)

    batch = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(trace_limit=1, import_mode="observations_v2"))
    )
    imported = import_trace_records(
        provider="langfuse",
        payload=batch.payload,
        source_label="langfuse:batch",
        out=tmp_path / "batch.jsonl",
        limit=batch.trace_count,
        max_payload_bytes=10_000,
    )

    assert batch.trace_count == 1
    assert imported.records_written == batch.trace_count


def test_legacy_batch_rejects_observations_from_a_different_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        trace_responses=[{"data": [{"id": "tr_1"}], "meta": {"totalPages": 1}}],
        observation_responses=[
            {"data": [{"id": "obs_1", "traceId": "tr_2"}], "meta": {"totalPages": 1}}
        ],
    )
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(ValueError, match="different trace"):
        provider.fetch_langfuse_connected_batch(
            **cast(Any, _batch_arguments(trace_limit=1, import_mode="legacy_traces"))
        )


@pytest.mark.parametrize("budget", ["request_limit", "response_byte_limit"])
def test_batch_budget_returns_only_completed_trace_progress(
    monkeypatch: pytest.MonkeyPatch,
    budget: provider.LangfuseFetchBudget,
) -> None:
    legacy = _FakeLangfuse(
        trace_responses=[
            {"data": [{"id": "tr_1"}], "meta": {"totalPages": 2}},
            provider.LangfuseFetchBudgetError(budget),
        ],
        observation_responses=[
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"totalPages": 1}}
        ],
    )
    _install_fake_client(monkeypatch, legacy)

    batch = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(import_mode="legacy_traces"))
    )

    assert batch.trace_count == 1
    assert batch.next_checkpoint == provider.LangfuseFetchCheckpoint(
        mode="legacy_traces",
        legacy_page=2,
    )

    observations = _FakeLangfuse(
        observation_responses=[
            {
                "data": [
                    {"id": "discovery_1", "traceId": "tr_1"},
                    {"id": "discovery_2", "traceId": "tr_2"},
                ],
                "meta": {"cursor": None},
            },
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
            provider.LangfuseFetchBudgetError(budget),
        ]
    )
    _install_fake_client(monkeypatch, observations)

    batch = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(import_mode="observations_v2"))
    )

    assert batch.trace_count == 1
    assert batch.next_checkpoint == provider.LangfuseFetchCheckpoint(
        mode="observations_v2",
        observations_position=1,
        observations_seen_trace_ids=("tr_1",),
    )


@pytest.mark.parametrize("mode", ["legacy_traces", "observations_v2"])
def test_batch_budget_before_complete_trace_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    mode: provider.LangfuseActiveImportMode,
) -> None:
    fake = _FakeLangfuse(
        trace_responses=[provider.LangfuseFetchBudgetError("request_limit")],
        observation_responses=[provider.LangfuseFetchBudgetError("response_byte_limit")],
    )
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(provider.LangfuseFetchBudgetError) as exc_info:
        provider.fetch_langfuse_connected_batch(**cast(Any, _batch_arguments(import_mode=mode)))

    assert exc_info.value.budget in {"request_limit", "response_byte_limit"}


def test_budgeted_transport_enforces_request_limit() -> None:
    transport = provider._BudgetedTransport(
        request_limit=1,
        response_byte_limit=100,
        network_backend=httpcore.MockBackend(
            [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"]
        ),
    )
    with httpx.Client(transport=transport) as client:
        assert client.get("https://langfuse.example.com/one").content == b"ok"
        with pytest.raises(provider.LangfuseFetchBudgetError) as request_error:
            client.get("https://langfuse.example.com/two")
    assert request_error.value.budget == "request_limit"


def test_network_read_never_exceeds_remaining_response_budget() -> None:
    raw_stream = _RecordingNetworkStream(b"abcdefghij")
    budget = provider._ResponseBudget(2)
    stream = provider._BudgetedNetworkStream(raw_stream, budget)

    assert stream.read(10) == b"ab"
    assert raw_stream.read_limits == [2]
    assert budget.remaining == 0
    with pytest.raises(provider.LangfuseFetchBudgetError) as exc_info:
        stream.read(10)
    assert exc_info.value.budget == "response_byte_limit"
    assert raw_stream.read_limits == [2]

    stream.write(b"request", 1.0)
    assert raw_stream.writes == [b"request"]
    assert stream.get_extra_info("kind") == "recording"
    context = ssl.create_default_context()
    tls_stream = stream.start_tls(context, "langfuse.example.com", 2.0)
    assert isinstance(tls_stream, provider._BudgetedNetworkStream)
    assert raw_stream.tls_calls == [(context, "langfuse.example.com", 2.0)]
    stream.close()
    assert raw_stream.closed


def test_budgeted_network_backend_wraps_connections_and_delegates_sleep() -> None:
    raw_stream = _RecordingNetworkStream()
    raw_backend = _RecordingNetworkBackend(raw_stream)
    backend = provider._BudgetedNetworkBackend(raw_backend, provider._ResponseBudget(10))

    assert isinstance(
        backend.connect_tcp("langfuse.example.com", 443, 1.0, "127.0.0.1", []),
        provider._BudgetedNetworkStream,
    )
    assert isinstance(
        backend.connect_unix_socket("/tmp/langfuse.sock", 2.0, []),
        provider._BudgetedNetworkStream,
    )
    backend.sleep(0.25)

    assert raw_backend.tcp_calls == [
        ("langfuse.example.com", 443, 1.0, "127.0.0.1", []),
    ]
    assert raw_backend.unix_calls == [("/tmp/langfuse.sock", 2.0, [])]
    assert raw_backend.sleeps == [0.25]


def test_transport_rejects_declared_body_before_reading_it() -> None:
    response_headers = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n"
    raw_stream = _RecordingNetworkStream(response_headers)
    transport = provider._BudgetedTransport(
        request_limit=1,
        response_byte_limit=len(response_headers) + 10,
        network_backend=_RecordingNetworkBackend(raw_stream),
    )

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(provider.LangfuseFetchBudgetError) as exc_info,
    ):
        client.get("https://langfuse.example.com")

    assert exc_info.value.budget == "response_byte_limit"
    assert raw_stream.payload == b""


def test_response_budget_is_cumulative_and_content_length_is_strict() -> None:
    budget = provider._ResponseBudget(5)
    budget.consume(3)
    budget.consume(2)
    with pytest.raises(provider.LangfuseFetchBudgetError):
        budget.consume(1)

    assert (
        provider._content_length(httpx.Response(200, headers={"content-length": "invalid"})) is None
    )
    assert provider._content_length(httpx.Response(200, headers={"content-length": "-1"})) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint": ""},
        {"public_key": "   "},
        {"secret_key": cast(Any, None)},
        {"from_timestamp": datetime(2026, 6, 1), "to_timestamp": datetime(2026, 6, 8)},
        {"from_timestamp": datetime(2026, 6, 8, tzinfo=UTC)},
        {"trace_limit": 0},
        {"request_limit": False},
        {"response_byte_limit": cast(Any, "large")},
        {"import_mode": cast(Any, "invalid")},
        {"checkpoint": cast(Any, {"mode": "legacy_traces"})},
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(mode=cast(Any, "invalid")),
        },
        {
            "import_mode": "legacy_traces",
            "checkpoint": provider.LangfuseFetchCheckpoint(mode="observations_v2"),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="legacy_traces",
                legacy_page=0,
            ),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="observations_v2",
                observations_position=-1,
            ),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="observations_v2",
                observations_cursor="",
            ),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="observations_v2",
                observations_seen_trace_ids=cast(Any, ["tr_1"]),
            ),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="observations_v2",
                observations_seen_trace_ids=("tr_1", "tr_1"),
            ),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="observations_v2",
                observations_seen_trace_ids=(cast(Any, 1),),
            ),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="legacy_traces",
                observations_cursor="cursor",
            ),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="legacy_traces",
                observations_seen_trace_ids=("tr_1",),
            ),
        },
        {
            "checkpoint": provider.LangfuseFetchCheckpoint(
                mode="observations_v2",
                legacy_page=2,
            ),
        },
    ],
)
def test_batch_validation_fails_before_provider_requests(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
) -> None:
    fake = _FakeLangfuse()
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(ValueError, match="Langfuse"):
        provider.fetch_langfuse_connected_batch(**cast(Any, _batch_arguments(**overrides)))

    assert fake.constructor_calls == []
    assert fake.api_constructor_calls == []


def test_batch_rejects_checkpoint_position_beyond_discovery_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": None},
            }
        ]
    )
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(ValueError, match="position exceeds"):
        provider.fetch_langfuse_connected_batch(
            **cast(
                Any,
                _batch_arguments(
                    checkpoint=provider.LangfuseFetchCheckpoint(
                        mode="observations_v2",
                        observations_position=2,
                    )
                ),
            )
        )


def test_legacy_batch_handles_empty_and_oversized_provider_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = _FakeLangfuse(trace_responses=[{"data": [], "meta": {"totalPages": 1}}])
    _install_fake_client(monkeypatch, empty)

    result = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(import_mode="legacy_traces"))
    )

    assert result == provider.LangfuseFetchBatch(
        payload={"traces": [], "observations": [], "meta": {}},
        trace_count=0,
        complete=True,
        next_checkpoint=None,
    )

    oversized = _FakeLangfuse(
        trace_responses=[{"data": [{"id": "tr_1"}, {"id": "tr_2"}], "meta": {}}]
    )
    _install_fake_client(monkeypatch, oversized)

    with pytest.raises(ValueError, match="one-trace page limit"):
        provider.fetch_langfuse_connected_batch(
            **cast(Any, _batch_arguments(import_mode="legacy_traces"))
        )


def test_batch_fallback_failure_preserves_cause_and_forced_mode_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken_fallback = _FakeLangfuse(
        trace_responses=[_api_error(404, {"error": "events_only"})],
        observation_responses=[{"data": {}, "meta": {}}],
    )
    _install_fake_client(monkeypatch, broken_fallback)

    with pytest.raises(ValueError, match="data list") as fallback_error:
        provider.fetch_langfuse_connected_batch(**cast(Any, _batch_arguments()))

    assert isinstance(fallback_error.value.__cause__, provider.LangfuseProviderError)

    forced = _FakeLangfuse(trace_responses=[_api_error(404)])
    _install_fake_client(monkeypatch, forced)

    with pytest.raises(provider.LangfuseProviderError):
        provider.fetch_langfuse_connected_batch(
            **cast(Any, _batch_arguments(import_mode="legacy_traces"))
        )
    assert forced.api.observations.calls == []


def test_observations_budget_during_first_trace_raises_without_partial_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": None},
            },
            provider.LangfuseFetchBudgetError("request_limit"),
        ]
    )
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(provider.LangfuseFetchBudgetError) as exc_info:
        provider.fetch_langfuse_connected_batch(
            **cast(Any, _batch_arguments(import_mode="observations_v2"))
        )

    assert exc_info.value.budget == "request_limit"


def test_observations_checkpoint_at_page_end_advances_or_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": None},
            }
        ]
    )
    _install_fake_client(monkeypatch, complete)
    checkpoint = provider.LangfuseFetchCheckpoint(
        mode="observations_v2",
        observations_position=1,
    )

    result = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(checkpoint=checkpoint))
    )

    assert result.complete is True
    assert result.trace_count == 0

    advance = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": "next-cursor"},
            },
            {
                "data": [{"id": "discovery_2", "traceId": "tr_2"}],
                "meta": {"cursor": None},
            },
            {"data": [{"id": "obs_2", "traceId": "tr_2"}], "meta": {"cursor": None}},
        ]
    )
    _install_fake_client(monkeypatch, advance)

    result = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(trace_limit=1, checkpoint=checkpoint))
    )

    assert result.complete is True
    assert advance.api.observations.calls[1]["cursor"] == "next-cursor"


def test_observations_batch_preserves_progress_when_next_discovery_hits_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": "next-cursor"},
            },
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
            provider.LangfuseFetchBudgetError("response_byte_limit"),
        ]
    )
    _install_fake_client(monkeypatch, fake)

    result = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(import_mode="observations_v2"))
    )

    assert result.trace_count == 1
    assert result.next_checkpoint == provider.LangfuseFetchCheckpoint(
        mode="observations_v2",
        observations_cursor="next-cursor",
        observations_seen_trace_ids=("tr_1",),
    )


def test_observations_batch_completes_when_provider_exhausts_before_trace_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLangfuse(
        observation_responses=[
            {
                "data": [{"id": "discovery_1", "traceId": "tr_1"}],
                "meta": {"cursor": "next-cursor"},
            },
            {"data": [{"id": "obs_1", "traceId": "tr_1"}], "meta": {"cursor": None}},
            {
                "data": [{"id": "discovery_2", "traceId": "tr_2"}],
                "meta": {"cursor": None},
            },
            {"data": [{"id": "obs_2", "traceId": "tr_2"}], "meta": {"cursor": None}},
        ]
    )
    _install_fake_client(monkeypatch, fake)

    result = provider.fetch_langfuse_connected_batch(
        **cast(Any, _batch_arguments(trace_limit=3, import_mode="observations_v2"))
    )

    assert result.trace_count == 2
    assert result.complete is True
    assert result.next_checkpoint is None


def test_content_length_handles_absent_header() -> None:
    assert provider._content_length(httpx.Response(200)) is None

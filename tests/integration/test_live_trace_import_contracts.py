from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from langfuse import Langfuse

from kensa import cli
from kensa import traces as traces_module
from kensa.providers import langfuse as langfuse_provider

pytestmark = pytest.mark.live

TRACE_VIEW_KEYS = {
    "schema_version",
    "id",
    "name",
    "source",
    "started_at_unix_nano",
    "ended_at_unix_nano",
    "duration_ms",
    "status",
    "input",
    "output",
    "attributes",
    "spans",
    "raw",
}
TRACE_SOURCE_KEYS = {
    "provider",
    "import_run_id",
    "imported_at",
    "source_path",
    "source_url",
    "trace_url",
}
SPAN_VIEW_KEYS = {
    "id",
    "trace_id",
    "parent_id",
    "name",
    "kind",
    "tool_name",
    "started_at_unix_nano",
    "ended_at_unix_nano",
    "duration_ms",
    "status",
    "status_message",
    "input",
    "output",
    "attributes",
    "events",
    "raw",
}


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set")
    return value


def _live_since(provider: str) -> str:
    return os.environ.get(f"{provider.upper()}_SINCE") or "7d"


def _live_limit() -> int:
    raw = os.environ.get("TRACE_IMPORT_LIMIT") or "10"
    return max(1, int(raw))


def _payload_size(payload: dict[str, Any]) -> int:
    return max(1, len(json.dumps(payload, sort_keys=True).encode()))


def _assert_data_envelope(payload: dict[str, Any], *, provider: str) -> list[Any]:
    data = payload.get("data")
    assert isinstance(data, list), f"{provider} live response must include a data list"
    return data


def _assert_trace_view_shape(trace: dict[str, Any]) -> None:
    assert set(trace) == TRACE_VIEW_KEYS
    assert trace["schema_version"] == traces_module.TRACE_VIEW_SCHEMA_VERSION
    assert set(trace["source"]) == TRACE_SOURCE_KEYS
    for span in trace["spans"]:
        assert set(span) == SPAN_VIEW_KEYS


def _assert_non_empty_data(payload: dict[str, Any], *, provider: str) -> None:
    if "data" in payload:
        rows = _assert_data_envelope(payload, provider=provider)
    else:
        rows = payload.get("traces")
        assert isinstance(rows, list), f"{provider} live response must include trace records"
    assert rows, (
        f"{provider} live response returned no records. "
        f"Set {provider.upper()}_SINCE to a window with known trace data."
    )


def _import_live_payload(
    *,
    provider: str,
    payload: dict[str, Any],
    endpoint: str,
    project: str | None,
    since: str,
    tmp_path: Path,
) -> None:
    out = tmp_path / f"{provider}.jsonl"
    result = cli._import_connected_payload(
        provider=provider,
        payload=payload,
        out=out,
        endpoint=endpoint,
        project=project,
        since=since,
        limit=_live_limit(),
        max_payload_bytes=_payload_size(payload),
        redact="keys",
    )

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert result.records_written > 0
    assert len(rows) == result.records_written
    for row in rows:
        _assert_trace_view_shape(row)


def test_live_langfuse_observations_endpoint_returns_official_envelope() -> None:
    endpoint = _required_env("LANGFUSE_BASE_URL")
    public_key = _required_env("LANGFUSE_PUBLIC_KEY")
    secret_key = _required_env("LANGFUSE_SECRET_KEY")
    since_filter = langfuse_provider._provider_since_filter(_live_since("langfuse"))
    client = Langfuse(
        base_url=endpoint.rstrip("/"),
        public_key=public_key,
        secret_key=secret_key,
        tracing_enabled=False,
        timeout=30,
    )
    response = client.api.observations.get_many(
        limit=1,
        from_start_time=since_filter.parsed,
        request_options=langfuse_provider._request_options(
            langfuse_provider._observations_since_query(since_filter)
        ),
    )
    payload = langfuse_provider.sdk_to_plain(response)

    _assert_data_envelope(payload, provider="langfuse")
    assert isinstance(payload.get("meta"), dict)
    cursor = payload["meta"].get("cursor")
    assert cursor is None or isinstance(cursor, str)


def test_live_langfuse_legacy_mode_still_works_where_trace_endpoint_exists() -> None:
    endpoint = _required_env("LANGFUSE_BASE_URL")
    public_key = _required_env("LANGFUSE_PUBLIC_KEY")
    secret_key = _required_env("LANGFUSE_SECRET_KEY")
    try:
        payload = langfuse_provider.fetch_langfuse_connected_export(
            endpoint=endpoint,
            public_key=public_key,
            secret_key=secret_key,
            since=_live_since("langfuse"),
            limit=_live_limit(),
            import_mode="legacy_traces",
        )
    except langfuse_provider.LangfuseProviderError as exc:
        if exc.label == "traces" and exc.status_code == 404:
            pytest.skip("Langfuse trace endpoint is unavailable for this deployment")
        raise

    rows = payload.get("traces")
    assert isinstance(rows, list), "langfuse live legacy import must include trace records"
    _assert_non_empty_data(payload, provider="langfuse")
    assert isinstance(payload.get("meta"), dict)
    for row in rows:
        assert isinstance(row, dict)
        assert row.get("id") or row.get("traceId") or row.get("trace_id")


def test_live_langfuse_connected_import_writes_non_empty_records(tmp_path: Path) -> None:
    endpoint = _required_env("LANGFUSE_BASE_URL")
    public_key = _required_env("LANGFUSE_PUBLIC_KEY")
    secret_key = _required_env("LANGFUSE_SECRET_KEY")
    since = _live_since("langfuse")

    payload = langfuse_provider.fetch_langfuse_connected_export(
        endpoint=endpoint,
        since=since,
        limit=_live_limit(),
        public_key=public_key,
        secret_key=secret_key,
    )

    _assert_non_empty_data(payload, provider="langfuse")
    _import_live_payload(
        provider="langfuse",
        payload=payload,
        endpoint=endpoint,
        project=None,
        since=since,
        tmp_path=tmp_path,
    )


def test_live_langfuse_observations_only_import_writes_non_empty_records(
    tmp_path: Path,
) -> None:
    endpoint = _required_env("LANGFUSE_BASE_URL")
    public_key = _required_env("LANGFUSE_PUBLIC_KEY")
    secret_key = _required_env("LANGFUSE_SECRET_KEY")
    since = _live_since("langfuse")

    payload = langfuse_provider.fetch_langfuse_connected_export(
        endpoint=endpoint,
        public_key=public_key,
        secret_key=secret_key,
        since=since,
        limit=_live_limit(),
        import_mode="observations_v2",
    )

    _assert_non_empty_data(payload, provider="langfuse")
    _import_live_payload(
        provider="langfuse",
        payload=payload,
        endpoint=endpoint,
        project=None,
        since=since,
        tmp_path=tmp_path,
    )

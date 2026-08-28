"""Langfuse SDK provider adapter."""

from __future__ import annotations

import contextlib
import json
import re
import ssl
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import Any, Literal, TypeVar, cast

import httpcore
import httpx
from langfuse import Langfuse
from langfuse.api import LangfuseAPI
from langfuse.api.core import ApiError, RequestOptions

LangfuseImportMode = Literal["legacy_traces", "observations_v2", "auto"]
LangfuseActiveImportMode = Literal["legacy_traces", "observations_v2"]
LangfuseFetchBudget = Literal["request_limit", "response_byte_limit"]

_CLIENT_TIMEOUT_SECONDS = 30
_SDK_MAX_RETRIES = 3
_TRACE_PAGE_LIMIT = 100
_LEGACY_OBSERVATION_PAGE_LIMIT = 100
_OBSERVATION_PAGE_LIMIT = 1000
_LEGACY_TRACE_FIELDS = "core,io"
_OBSERVATIONS_V2_DISCOVERY_FIELDS = "core"
_OBSERVATIONS_V2_FIELDS = "core,basic,io,model,usage,trace_context"
_SINCE_WINDOW = re.compile(r"^(?P<count>\d+)(?P<unit>[mhdw])$")
_RESPONSE_HINT_MAX_CHARS = 300
_LANGFUSE_SDK_VERSION = version("langfuse")

_T = TypeVar("_T")


class LangfuseProviderError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        label: str,
        status_code: int | None = None,
        endpoint: str | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.label = label
        self.status_code = status_code
        self.endpoint = endpoint
        self.response_body = response_body


class LangfuseFetchBudgetError(ValueError):
    def __init__(self, budget: LangfuseFetchBudget) -> None:
        super().__init__(f"Langfuse fetch exceeded {budget}")
        self.budget = budget


@dataclass(frozen=True)
class LangfuseFetchCheckpoint:
    mode: LangfuseActiveImportMode
    legacy_page: int = 1
    observations_cursor: str | None = None
    observations_position: int = 0
    observations_seen_trace_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LangfuseFetchBatch:
    payload: dict[str, Any]
    trace_count: int
    complete: bool
    next_checkpoint: LangfuseFetchCheckpoint | None


@dataclass(frozen=True)
class _BatchLangfuseClient:
    api: LangfuseAPI


_LangfuseClient = Langfuse | _BatchLangfuseClient


class _ResponseBudget:
    def __init__(self, byte_limit: int) -> None:
        self._byte_limit = byte_limit
        self._bytes_read = 0

    @property
    def remaining(self) -> int:
        return self._byte_limit - self._bytes_read

    def consume(self, byte_count: int) -> None:
        if byte_count > self.remaining:
            raise LangfuseFetchBudgetError("response_byte_limit")
        self._bytes_read += byte_count


class _BudgetedNetworkStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream, budget: _ResponseBudget) -> None:
        self._stream = stream
        self._budget = budget

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._budget.remaining <= 0:
            raise LangfuseFetchBudgetError("response_byte_limit")
        data = self._stream.read(min(max_bytes, self._budget.remaining), timeout)
        self._budget.consume(len(data))
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, timeout)

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        return _BudgetedNetworkStream(
            self._stream.start_tls(ssl_context, server_hostname, timeout),
            self._budget,
        )

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _BudgetedNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, backend: httpcore.NetworkBackend, budget: _ResponseBudget) -> None:
        self._backend = backend
        self._budget = budget

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        return _BudgetedNetworkStream(
            self._backend.connect_tcp(host, port, timeout, local_address, socket_options),
            self._budget,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        return _BudgetedNetworkStream(
            self._backend.connect_unix_socket(path, timeout, socket_options),
            self._budget,
        )

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _RawBudgetedHTTPTransport(httpx.HTTPTransport):
    def __init__(
        self,
        budget: _ResponseBudget,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        super().__init__(http1=True, http2=False)
        pool = cast(Any, self._pool)
        pool._network_backend = _BudgetedNetworkBackend(
            backend or httpcore.SyncBackend(),
            budget,
        )


class _BudgetedTransport(httpx.BaseTransport):
    def __init__(
        self,
        *,
        request_limit: int,
        response_byte_limit: int,
        network_backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._request_limit = request_limit
        self._request_count = 0
        self._response_budget = _ResponseBudget(response_byte_limit)
        self._transport = _RawBudgetedHTTPTransport(
            self._response_budget,
            network_backend,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._request_count >= self._request_limit:
            raise LangfuseFetchBudgetError("request_limit")
        self._request_count += 1
        response = self._transport.handle_request(request)
        response_stream = cast(httpx.SyncByteStream, response.stream)
        content_length = _content_length(response)
        if content_length is not None and content_length > self._response_budget.remaining:
            response_stream.close()
            raise LangfuseFetchBudgetError("response_byte_limit")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=response_stream,
            extensions=response.extensions,
            request=request,
        )

    def close(self) -> None:
        self._transport.close()


@dataclass(frozen=True)
class _SinceFilter:
    parsed: datetime | None = None
    raw: str | None = None


def check_langfuse_connection(
    *,
    endpoint: str,
    public_key: str,
    secret_key: str,
) -> None:
    client = _build_client(endpoint=endpoint, public_key=public_key, secret_key=secret_key)
    _call_sdk(
        lambda: client.api.projects.get(request_options=_request_options()),
        label="projects",
        endpoint=endpoint,
    )


def fetch_langfuse_connected_export(
    *,
    endpoint: str,
    public_key: str,
    secret_key: str,
    since: str | None,
    limit: int,
    import_mode: LangfuseImportMode = "auto",
) -> dict[str, Any]:
    client = _build_client(endpoint=endpoint, public_key=public_key, secret_key=secret_key)
    since_filter = _provider_since_filter(since)
    if import_mode == "legacy_traces":
        return _fetch_legacy_trace_export(
            client=client,
            endpoint=endpoint,
            since_filter=since_filter,
            limit=limit,
        )
    if import_mode == "observations_v2":
        return _fetch_observations_v2_export(
            client=client,
            endpoint=endpoint,
            since_filter=since_filter,
            limit=limit,
        )
    if import_mode != "auto":
        raise ValueError(f"Unsupported Langfuse import mode: {import_mode}")
    try:
        return _fetch_legacy_trace_export(
            client=client,
            endpoint=endpoint,
            since_filter=since_filter,
            limit=limit,
        )
    except LangfuseProviderError as exc:
        if exc.label != "traces" or exc.status_code != 404:
            raise
        try:
            return _fetch_observations_v2_export(
                client=client,
                endpoint=endpoint,
                since_filter=since_filter,
                limit=limit,
            )
        except (OSError, RuntimeError, ValueError) as fallback_exc:
            raise fallback_exc from exc


def fetch_langfuse_connected_batch(
    *,
    endpoint: str,
    public_key: str,
    secret_key: str,
    from_timestamp: datetime,
    to_timestamp: datetime,
    trace_limit: int,
    request_limit: int,
    response_byte_limit: int,
    checkpoint: LangfuseFetchCheckpoint | None = None,
    import_mode: LangfuseImportMode = "auto",
) -> LangfuseFetchBatch:
    """Fetch one bounded, resumable batch of complete Langfuse traces."""

    _validate_batch_arguments(
        endpoint=endpoint,
        public_key=public_key,
        secret_key=secret_key,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        trace_limit=trace_limit,
        request_limit=request_limit,
        response_byte_limit=response_byte_limit,
        checkpoint=checkpoint,
        import_mode=import_mode,
    )
    with _batch_client(
        endpoint=endpoint,
        public_key=public_key,
        secret_key=secret_key,
        request_limit=request_limit,
        response_byte_limit=response_byte_limit,
    ) as client:
        if checkpoint is not None:
            return _fetch_batch_for_checkpoint(
                client=client,
                endpoint=endpoint,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                trace_limit=trace_limit,
                checkpoint=checkpoint,
            )
        if import_mode == "observations_v2":
            return _fetch_observations_v2_batch(
                client=client,
                endpoint=endpoint,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                trace_limit=trace_limit,
                checkpoint=LangfuseFetchCheckpoint(mode="observations_v2"),
            )
        try:
            return _fetch_legacy_batch(
                client=client,
                endpoint=endpoint,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                trace_limit=trace_limit,
                checkpoint=LangfuseFetchCheckpoint(mode="legacy_traces"),
            )
        except LangfuseProviderError as exc:
            if import_mode != "auto" or exc.label != "traces" or exc.status_code != 404:
                raise
            try:
                return _fetch_observations_v2_batch(
                    client=client,
                    endpoint=endpoint,
                    from_timestamp=from_timestamp,
                    to_timestamp=to_timestamp,
                    trace_limit=trace_limit,
                    checkpoint=LangfuseFetchCheckpoint(mode="observations_v2"),
                )
            except (OSError, RuntimeError, ValueError) as fallback_exc:
                raise fallback_exc from exc


def _fetch_batch_for_checkpoint(
    *,
    client: _LangfuseClient,
    endpoint: str,
    from_timestamp: datetime,
    to_timestamp: datetime,
    trace_limit: int,
    checkpoint: LangfuseFetchCheckpoint,
) -> LangfuseFetchBatch:
    if checkpoint.mode == "legacy_traces":
        return _fetch_legacy_batch(
            client=client,
            endpoint=endpoint,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            trace_limit=trace_limit,
            checkpoint=checkpoint,
        )
    return _fetch_observations_v2_batch(
        client=client,
        endpoint=endpoint,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        trace_limit=trace_limit,
        checkpoint=checkpoint,
    )


def _fetch_legacy_batch(
    *,
    client: _LangfuseClient,
    endpoint: str,
    from_timestamp: datetime,
    to_timestamp: datetime,
    trace_limit: int,
    checkpoint: LangfuseFetchCheckpoint,
) -> LangfuseFetchBatch:
    traces: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    page = checkpoint.legacy_page
    meta: dict[str, Any] = {}
    while len(traces) < trace_limit:
        try:
            current_page = page
            trace_payload = _langfuse_response_envelope(
                _call_sdk(
                    lambda current_page=current_page: client.api.trace.list(
                        page=current_page,
                        limit=1,
                        fields=_LEGACY_TRACE_FIELDS,
                        from_timestamp=from_timestamp,
                        to_timestamp=to_timestamp,
                        order_by="timestamp.asc",
                        request_options=_request_options(),
                    ),
                    label="traces",
                    endpoint=endpoint,
                ),
                label="traces",
            )
            page_rows = trace_payload["data"]
            meta = trace_payload["meta"]
            if not page_rows:
                return _legacy_batch_result(traces, observations, complete=True)
            if len(page_rows) != 1:
                raise ValueError("Langfuse trace response ignored its one-trace page limit")
            trace = page_rows[0]
            trace_id = _langfuse_trace_id(trace)
            trace_observations = _fetch_legacy_observation_rows(
                client=client,
                endpoint=endpoint,
                trace_id=trace_id,
            )
            _validate_observation_trace_rows(trace_observations, trace_id, allow_empty=True)
        except LangfuseFetchBudgetError:
            if not traces:
                raise
            return _legacy_batch_result(
                traces,
                observations,
                complete=False,
                next_page=page,
            )

        traces.append(trace)
        observations.extend(trace_observations)
        current_page = page
        page += 1
        if _trace_page_is_last(meta, current_page):
            return _legacy_batch_result(traces, observations, complete=True)

    return _legacy_batch_result(traces, observations, complete=False, next_page=page)


def _legacy_batch_result(
    traces: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    complete: bool,
    next_page: int | None = None,
) -> LangfuseFetchBatch:
    return LangfuseFetchBatch(
        payload={"traces": traces, "observations": observations, "meta": {}},
        trace_count=len(traces),
        complete=complete,
        next_checkpoint=(
            None
            if complete
            else LangfuseFetchCheckpoint(
                mode="legacy_traces",
                legacy_page=cast(int, next_page),
            )
        ),
    )


def _fetch_observations_v2_batch(
    *,
    client: _LangfuseClient,
    endpoint: str,
    from_timestamp: datetime,
    to_timestamp: datetime,
    trace_limit: int,
    checkpoint: LangfuseFetchCheckpoint,
) -> LangfuseFetchBatch:
    imported_rows: list[dict[str, Any]] = []
    imported_trace_count = 0
    cursor = checkpoint.observations_cursor
    position = checkpoint.observations_position
    seen_trace_ids = dict.fromkeys(checkpoint.observations_seen_trace_ids)
    while True:
        try:
            current_cursor = cursor
            discovery = _langfuse_response_envelope(
                _call_sdk(
                    lambda current_cursor=current_cursor: client.api.observations.get_many(
                        fields=_OBSERVATIONS_V2_DISCOVERY_FIELDS,
                        limit=_OBSERVATION_PAGE_LIMIT,
                        cursor=current_cursor,
                        from_start_time=from_timestamp,
                        to_start_time=to_timestamp,
                        request_options=_request_options(),
                    ),
                    label="observations",
                    endpoint=endpoint,
                ),
                label="observations",
            )
        except LangfuseFetchBudgetError:
            if not imported_rows:
                raise
            return _observations_batch_result(
                imported_rows,
                imported_trace_count,
                cursor=cursor,
                position=position,
                seen_trace_ids=seen_trace_ids,
            )

        trace_ids = _ordered_trace_ids(discovery["data"])
        next_cursor = _response_cursor(discovery)
        if position > len(trace_ids):
            raise ValueError("Langfuse checkpoint position exceeds its observations page")
        if position == len(trace_ids):
            if next_cursor is None:
                return _observations_batch_result(
                    imported_rows,
                    imported_trace_count,
                    complete=True,
                )
            cursor = next_cursor
            position = 0
            continue

        for index in range(position, len(trace_ids)):
            trace_id = trace_ids[index]
            if trace_id in seen_trace_ids:
                continue
            try:
                raw_trace_rows = _fetch_observation_rows(
                    client=client,
                    endpoint=endpoint,
                    trace_id=trace_id,
                    fields=_OBSERVATIONS_V2_FIELDS,
                )
                _validate_observation_trace_rows(raw_trace_rows, trace_id)
                trace_rows = _parse_observations_v2_io(raw_trace_rows)
            except LangfuseFetchBudgetError:
                if not imported_rows:
                    raise
                return _observations_batch_result(
                    imported_rows,
                    imported_trace_count,
                    cursor=cursor,
                    position=index,
                    seen_trace_ids=seen_trace_ids,
                )
            imported_rows.extend(trace_rows)
            imported_trace_count += 1
            seen_trace_ids[trace_id] = None
            if imported_trace_count >= trace_limit:
                if index + 1 < len(trace_ids):
                    return _observations_batch_result(
                        imported_rows,
                        imported_trace_count,
                        cursor=cursor,
                        position=index + 1,
                        seen_trace_ids=seen_trace_ids,
                    )
                if next_cursor is not None:
                    return _observations_batch_result(
                        imported_rows,
                        imported_trace_count,
                        cursor=next_cursor,
                        position=0,
                        seen_trace_ids=seen_trace_ids,
                    )
                return _observations_batch_result(
                    imported_rows,
                    imported_trace_count,
                    complete=True,
                )

        if next_cursor is None:
            return _observations_batch_result(
                imported_rows,
                imported_trace_count,
                complete=True,
            )
        cursor = next_cursor
        position = 0


def _observations_batch_result(
    rows: list[dict[str, Any]],
    trace_count: int,
    *,
    cursor: str | None = None,
    position: int = 0,
    seen_trace_ids: dict[str, None] | None = None,
    complete: bool = False,
) -> LangfuseFetchBatch:
    return LangfuseFetchBatch(
        payload={"data": rows, "meta": {}},
        trace_count=trace_count,
        complete=complete,
        next_checkpoint=(
            None
            if complete
            else LangfuseFetchCheckpoint(
                mode="observations_v2",
                observations_cursor=cursor,
                observations_position=position,
                observations_seen_trace_ids=tuple(seen_trace_ids or ()),
            )
        ),
    )


def _ordered_trace_ids(rows: list[dict[str, Any]]) -> list[str]:
    trace_ids: dict[str, None] = {}
    for row in rows:
        value = row.get("traceId") or row.get("trace_id")
        if value is not None and str(value) != "":
            trace_ids.setdefault(str(value), None)
    return list(trace_ids)


def sdk_to_plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [sdk_to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [sdk_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: sdk_to_plain(item) for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        with contextlib.suppress(TypeError):
            return sdk_to_plain(model_dump(mode="json", by_alias=True))
        with contextlib.suppress(TypeError):
            return sdk_to_plain(model_dump(by_alias=True))
    dict_dump = getattr(value, "dict", None)
    if callable(dict_dump):
        with contextlib.suppress(TypeError):
            return sdk_to_plain(dict_dump(by_alias=True))
        return sdk_to_plain(dict_dump())
    return value


def _validate_batch_arguments(
    *,
    endpoint: str,
    public_key: str,
    secret_key: str,
    from_timestamp: datetime,
    to_timestamp: datetime,
    trace_limit: int,
    request_limit: int,
    response_byte_limit: int,
    checkpoint: LangfuseFetchCheckpoint | None,
    import_mode: LangfuseImportMode,
) -> None:
    for label, value in (
        ("endpoint", endpoint),
        ("public key", public_key),
        ("secret key", secret_key),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Langfuse {label} must be a non-empty string")
    if not _aware_datetime(from_timestamp) or not _aware_datetime(to_timestamp):
        raise ValueError("Langfuse batch timestamps must be timezone-aware")
    if from_timestamp >= to_timestamp:
        raise ValueError("Langfuse batch start timestamp must be before its end timestamp")
    for label, value in (
        ("trace_limit", trace_limit),
        ("request_limit", request_limit),
        ("response_byte_limit", response_byte_limit),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"Langfuse {label} must be a positive integer")
    if import_mode not in {"legacy_traces", "observations_v2", "auto"}:
        raise ValueError(f"Unsupported Langfuse import mode: {import_mode}")
    if checkpoint is None:
        return
    if not isinstance(checkpoint, LangfuseFetchCheckpoint):
        raise ValueError("Langfuse checkpoint has an invalid type")
    if checkpoint.mode not in {"legacy_traces", "observations_v2"}:
        raise ValueError("Langfuse checkpoint has an invalid import mode")
    if import_mode != "auto" and checkpoint.mode != import_mode:
        raise ValueError("Langfuse checkpoint mode does not match the requested import mode")
    if (
        not isinstance(checkpoint.legacy_page, int)
        or isinstance(checkpoint.legacy_page, bool)
        or checkpoint.legacy_page <= 0
    ):
        raise ValueError("Langfuse checkpoint legacy page must be a positive integer")
    if (
        not isinstance(checkpoint.observations_position, int)
        or isinstance(checkpoint.observations_position, bool)
        or checkpoint.observations_position < 0
    ):
        raise ValueError("Langfuse checkpoint observations position must be a non-negative integer")
    if checkpoint.observations_cursor is not None and not checkpoint.observations_cursor:
        raise ValueError("Langfuse checkpoint observations cursor cannot be empty")
    if (
        not isinstance(checkpoint.observations_seen_trace_ids, tuple)
        or any(
            not isinstance(trace_id, str) or not trace_id
            for trace_id in checkpoint.observations_seen_trace_ids
        )
        or len(set(checkpoint.observations_seen_trace_ids))
        != len(checkpoint.observations_seen_trace_ids)
    ):
        raise ValueError("Langfuse checkpoint seen trace IDs must be unique non-empty strings")
    if checkpoint.mode == "legacy_traces" and (
        checkpoint.observations_cursor is not None or checkpoint.observations_position != 0
    ):
        raise ValueError("Legacy Langfuse checkpoints cannot include observations state")
    if checkpoint.mode == "legacy_traces" and checkpoint.observations_seen_trace_ids:
        raise ValueError("Legacy Langfuse checkpoints cannot include seen trace IDs")
    if checkpoint.mode == "observations_v2" and checkpoint.legacy_page != 1:
        raise ValueError("Observations-v2 Langfuse checkpoints cannot include a legacy page")


def _aware_datetime(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


@contextmanager
def _batch_client(
    *,
    endpoint: str,
    public_key: str,
    secret_key: str,
    request_limit: int,
    response_byte_limit: int,
) -> Iterator[_BatchLangfuseClient]:
    transport = _BudgetedTransport(
        request_limit=request_limit,
        response_byte_limit=response_byte_limit,
    )
    with httpx.Client(
        transport=transport,
        timeout=_CLIENT_TIMEOUT_SECONDS,
    ) as http_client:
        api = LangfuseAPI(
            base_url=endpoint.rstrip("/"),
            username=public_key,
            password=secret_key,
            x_langfuse_sdk_name="python",
            x_langfuse_sdk_version=_LANGFUSE_SDK_VERSION,
            x_langfuse_public_key=public_key,
            httpx_client=http_client,
            timeout=_CLIENT_TIMEOUT_SECONDS,
        )
        yield _BatchLangfuseClient(api=api)


def _build_client(
    *,
    endpoint: str,
    public_key: str,
    secret_key: str,
) -> Langfuse:
    return Langfuse(
        base_url=endpoint.rstrip("/"),
        public_key=public_key,
        secret_key=secret_key,
        tracing_enabled=False,
        timeout=_CLIENT_TIMEOUT_SECONDS,
    )


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    with contextlib.suppress(ValueError):
        length = int(value)
        return length if length >= 0 else None
    return None


def _request_options(
    additional_query_parameters: dict[str, Any] | None = None,
) -> RequestOptions:
    options: dict[str, Any] = {
        "timeout_in_seconds": _CLIENT_TIMEOUT_SECONDS,
        "max_retries": _SDK_MAX_RETRIES,
    }
    if additional_query_parameters:
        options["additional_query_parameters"] = additional_query_parameters
    return cast(RequestOptions, options)


def _call_sdk(
    request: Callable[[], _T],
    *,
    label: str,
    endpoint: str,
) -> _T:
    try:
        return request()
    except ApiError as exc:
        raise _provider_error_from_api_error(exc, label=label, endpoint=endpoint) from exc
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        raise _provider_error_from_transport_error(exc, label=label, endpoint=endpoint) from exc


def _provider_error_from_api_error(
    exc: ApiError,
    *,
    label: str,
    endpoint: str,
) -> LangfuseProviderError:
    status_code = exc.status_code
    response_body = exc.body
    body_hint = _langfuse_http_error_body_hint(status_code, response_body)
    message = _langfuse_failure_message(
        reason=_langfuse_http_error_reason(status_code),
        label=label,
        endpoint=endpoint,
        body_hint=body_hint,
        next_step=_langfuse_http_error_next_step(status_code, label=label),
    )
    return LangfuseProviderError(
        message,
        label=label,
        status_code=status_code,
        endpoint=endpoint,
        response_body=response_body,
    )


def _provider_error_from_transport_error(
    exc: Exception,
    *,
    label: str,
    endpoint: str,
) -> LangfuseProviderError:
    return LangfuseProviderError(
        _langfuse_failure_message(
            reason="could not be reached",
            label=label,
            endpoint=endpoint,
            body_hint=_langfuse_transport_hint(exc),
            next_step="Check your internet connection, proxy or VPN, and selected Langfuse region.",
        ),
        label=label,
        endpoint=endpoint,
    )


def _fetch_legacy_trace_export(
    *,
    client: _LangfuseClient,
    endpoint: str,
    since_filter: _SinceFilter,
    limit: int,
) -> dict[str, Any]:
    traces, trace_meta = _fetch_trace_rows(
        client=client,
        endpoint=endpoint,
        since_filter=since_filter,
        limit=limit,
    )
    observations: list[dict[str, Any]] = []
    for trace in traces:
        observations.extend(
            _fetch_legacy_observation_rows(
                client=client,
                endpoint=endpoint,
                trace_id=_langfuse_trace_id(trace),
            )
        )
    return {"traces": traces, "observations": observations, "meta": trace_meta}


def _fetch_trace_rows(
    *,
    client: _LangfuseClient,
    endpoint: str,
    since_filter: _SinceFilter,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if limit <= 0:
        return [], {}
    rows: list[dict[str, Any]] = []
    page = 1
    page_limit = min(limit, _TRACE_PAGE_LIMIT)
    meta: dict[str, Any] = {}
    while len(rows) < limit:
        current_page = page
        payload = _langfuse_response_envelope(
            _call_sdk(
                lambda current_page=current_page: client.api.trace.list(
                    page=current_page,
                    limit=page_limit,
                    fields=_LEGACY_TRACE_FIELDS,
                    from_timestamp=since_filter.parsed,
                    request_options=_request_options(_legacy_since_query(since_filter)),
                ),
                label="traces",
                endpoint=endpoint,
            ),
            label="traces",
        )
        page_rows = payload["data"]
        rows.extend(page_rows)
        meta = payload["meta"]
        if not page_rows or _trace_page_is_last(meta, page):
            break
        page += 1
    return rows[:limit], meta


def _fetch_legacy_observation_rows(
    *,
    client: _LangfuseClient,
    endpoint: str,
    trace_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        current_page = page
        payload = _langfuse_response_envelope(
            _call_sdk(
                lambda current_page=current_page: client.api.legacy.observations_v1.get_many(
                    page=current_page,
                    limit=_LEGACY_OBSERVATION_PAGE_LIMIT,
                    trace_id=trace_id,
                    request_options=_request_options(),
                ),
                label="observations",
                endpoint=endpoint,
            ),
            label="observations",
        )
        page_rows = payload["data"]
        rows.extend(page_rows)
        if not page_rows or _trace_page_is_last(payload["meta"], page):
            break
        page += 1
    return rows


def _fetch_observations_v2_export(
    *,
    client: _LangfuseClient,
    endpoint: str,
    since_filter: _SinceFilter,
    limit: int,
) -> dict[str, Any]:
    trace_ids = _discover_observation_trace_ids(
        client=client,
        endpoint=endpoint,
        since_filter=since_filter,
        limit=limit,
    )
    rows: list[dict[str, Any]] = []
    for trace_id in trace_ids:
        rows.extend(
            _parse_observations_v2_io(
                _fetch_observation_rows(
                    client=client,
                    endpoint=endpoint,
                    trace_id=trace_id,
                    fields=_OBSERVATIONS_V2_FIELDS,
                )
            )
        )
    return {"data": rows, "meta": {}}


def _discover_observation_trace_ids(
    *,
    client: _LangfuseClient,
    endpoint: str,
    since_filter: _SinceFilter,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []
    trace_ids: dict[str, None] = {}
    cursor: str | None = None
    page_limit = min(limit, _OBSERVATION_PAGE_LIMIT)
    while len(trace_ids) < limit:
        current_cursor = cursor
        payload = _langfuse_response_envelope(
            _call_sdk(
                lambda current_cursor=current_cursor: client.api.observations.get_many(
                    fields=_OBSERVATIONS_V2_DISCOVERY_FIELDS,
                    limit=page_limit,
                    cursor=current_cursor,
                    from_start_time=since_filter.parsed,
                    request_options=_request_options(_observations_since_query(since_filter)),
                ),
                label="observations",
                endpoint=endpoint,
            ),
            label="observations",
        )
        page_rows = payload["data"]
        for row in page_rows:
            trace_id = row.get("traceId") or row.get("trace_id")
            if trace_id is not None and str(trace_id) != "":
                trace_ids.setdefault(str(trace_id), None)
        cursor = _response_cursor(payload)
        if cursor is None or not page_rows:
            break
    return list(trace_ids)[:limit]


def _fetch_observation_rows(
    *,
    client: _LangfuseClient,
    endpoint: str,
    trace_id: str,
    fields: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        current_cursor = cursor
        payload = _langfuse_response_envelope(
            _call_sdk(
                lambda current_cursor=current_cursor: client.api.observations.get_many(
                    trace_id=trace_id,
                    fields=fields,
                    limit=_OBSERVATION_PAGE_LIMIT,
                    cursor=current_cursor,
                    request_options=_request_options(),
                ),
                label="observations",
                endpoint=endpoint,
            ),
            label="observations",
        )
        page_rows = payload["data"]
        rows.extend(page_rows)
        cursor = _response_cursor(payload)
        if cursor is None or not page_rows:
            break
    return rows


def _validate_observation_trace_rows(
    rows: list[dict[str, Any]],
    trace_id: str,
    *,
    allow_empty: bool = False,
) -> None:
    if not rows and not allow_empty:
        raise ValueError("Langfuse observation response omitted the requested trace")
    for row in rows:
        value = row.get("traceId") or row.get("trace_id")
        if value is None or str(value) != trace_id:
            raise ValueError("Langfuse observation response included a different trace")


def _provider_since_filter(since: str | None) -> _SinceFilter:
    if since is None:
        return _SinceFilter()
    stripped = since.strip()
    match = _SINCE_WINDOW.fullmatch(stripped)
    if match is None:
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return _SinceFilter(parsed=parsed.replace(microsecond=0))
        return _SinceFilter(raw=stripped)
    seconds = (
        int(match.group("count"))
        * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group("unit")]
    )
    parsed = (datetime.now(UTC) - timedelta(seconds=seconds)).replace(microsecond=0)
    return _SinceFilter(parsed=parsed)


def _legacy_since_query(since_filter: _SinceFilter) -> dict[str, Any] | None:
    return {"fromTimestamp": since_filter.raw} if since_filter.raw is not None else None


def _observations_since_query(since_filter: _SinceFilter) -> dict[str, Any] | None:
    return {"fromStartTime": since_filter.raw} if since_filter.raw is not None else None


def _langfuse_response_envelope(value: Any, *, label: str) -> dict[str, Any]:
    payload = sdk_to_plain(value)
    if not isinstance(payload, dict):
        raise ValueError("Langfuse connected import response must be a JSON object")
    data = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(data, list):
        raise ValueError(f"Langfuse connected import response must include a data list for {label}")
    if not all(isinstance(row, dict) for row in data):
        raise ValueError(f"Langfuse connected import response must include object rows for {label}")
    if not isinstance(meta, dict):
        raise ValueError(
            f"Langfuse connected import response must include a meta object for {label}"
        )
    return {"data": data, "meta": meta}


def _response_cursor(payload: dict[str, Any]) -> str | None:
    next_cursor = payload["meta"].get("cursor")
    return str(next_cursor) if next_cursor else None


def _trace_page_is_last(meta: dict[str, Any], page: int) -> bool:
    total_pages = meta.get("totalPages") or meta.get("total_pages")
    if total_pages is None:
        return False
    return page >= int(total_pages)


def _langfuse_trace_id(trace: dict[str, Any]) -> str:
    value = trace.get("id") or trace.get("traceId") or trace.get("trace_id")
    if value is None or str(value) == "":
        raise ValueError("Langfuse trace response row is missing a trace id")
    return str(value)


def _parse_observations_v2_io(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        parsed_row = dict(row)
        for key in ("input", "output"):
            if key in parsed_row:
                parsed_row[key] = _parse_observation_io_value(parsed_row[key])
        parsed_rows.append(parsed_row)
    return parsed_rows


def _parse_observation_io_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, (dict, list)):
        return parsed
    return value


def _langfuse_http_error_body_hint(status_code: int | None, body: Any) -> str | None:
    text = _response_body_text(body)
    if text is None:
        return None
    if "events_only" in text.lower():
        return (
            "Langfuse returned an events_only hint; this deployment may expose ingestion-only "
            "event APIs instead of trace reads."
        )
    if status_code is not None and 400 <= status_code < 500:
        return _langfuse_response_body_hint(text)
    return None


def _response_body_text(body: Any) -> str | None:
    if body is None:
        return None
    if isinstance(body, str):
        text = body
    else:
        text = None
        with contextlib.suppress(TypeError):
            text = json.dumps(body, sort_keys=True)
        if not isinstance(text, str):
            text = str(body)
    text = " ".join(text.split())
    return text or None


def _langfuse_response_body_hint(text: str) -> str | None:
    sanitized = "".join(ch for ch in " ".join(text.split()) if ch.isprintable())
    if not sanitized:
        return None
    if len(sanitized) > _RESPONSE_HINT_MAX_CHARS:
        sanitized = f"{sanitized[: _RESPONSE_HINT_MAX_CHARS - 3]}..."
    return f"Langfuse response: {sanitized}"


def _langfuse_http_error_reason(status_code: int | None) -> str:
    if status_code == 401:
        return "rejected the credentials"
    if status_code == 403:
        return "denied access"
    if status_code == 404:
        return "could not find the requested endpoint"
    if status_code == 429:
        return "rate limited Kensa"
    return f"returned HTTP {status_code}" if status_code is not None else "returned an error"


def _langfuse_http_error_next_step(status_code: int | None, *, label: str | None = None) -> str:
    if status_code in {401, 403}:
        return "Check LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and the selected Langfuse region."
    if status_code == 429:
        return "Wait a minute, then retry the Langfuse connection."
    if status_code == 400:
        return (
            "Kensa reached Langfuse, but Langfuse rejected request parameters. "
            "Upgrade Kensa if this persists, or file an issue with the response hint."
        )
    if status_code == 404 and label == "traces":
        return (
            "Kensa reached Langfuse, but the trace read API was unavailable.\n"
            "This Langfuse deployment does not expose the trace read API required by "
            "kensa import.\n"
            "Check that this deployment supports GET /api/public/traces, or retry with "
            "kensa import --from langfuse --langfuse-mode observations_v2."
        )
    if status_code == 404:
        return "Check the selected Langfuse region or custom base URL."
    return "Check the selected Langfuse region or retry after Langfuse is healthy."


def _langfuse_failure_message(
    *,
    reason: str,
    label: str,
    endpoint: str,
    next_step: str,
    body_hint: str | None = None,
) -> str:
    lines = [f"Langfuse {reason} while fetching {label}.", f"Endpoint: {endpoint}"]
    if body_hint is not None:
        lines.append(body_hint)
    lines.append(next_step)
    lines.append("Then run: kensa connect langfuse")
    return "\n".join(lines)


def _langfuse_transport_hint(exc: Exception) -> str:
    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return "The request timed out before Langfuse responded."
    if "name resolution" in text or "nodename" in text or "getaddrinfo" in text:
        return "Kensa could not resolve the Langfuse host."
    if "certificate" in text or "tls" in text or "ssl" in text:
        return "TLS certificate verification failed for the Langfuse host."
    return "Kensa could not reach Langfuse."

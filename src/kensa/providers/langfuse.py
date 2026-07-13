"""Langfuse SDK provider adapter."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar, cast

import httpx
from langfuse import Langfuse
from langfuse.api.core import ApiError, RequestOptions

LangfuseImportMode = Literal["legacy_traces", "observations_v2", "auto"]

_CLIENT_TIMEOUT_SECONDS = 30
_SDK_MAX_RETRIES = 3
_TRACE_PAGE_LIMIT = 100
_OBSERVATION_PAGE_LIMIT = 1000
_LEGACY_TRACE_FIELDS = "core,io"
_OBSERVATIONS_V2_DISCOVERY_FIELDS = "core"
_OBSERVATIONS_V2_FIELDS = "core,basic,io,model,usage,trace_context"
_SINCE_WINDOW = re.compile(r"^(?P<count>\d+)(?P<unit>[mhdw])$")
_RESPONSE_HINT_MAX_CHARS = 300

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


def _build_client(*, endpoint: str, public_key: str, secret_key: str) -> Langfuse:
    return Langfuse(
        base_url=endpoint.rstrip("/"),
        public_key=public_key,
        secret_key=secret_key,
        tracing_enabled=False,
        timeout=_CLIENT_TIMEOUT_SECONDS,
    )


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
    client: Langfuse,
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
    client: Langfuse,
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
    client: Langfuse,
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
                    limit=_OBSERVATION_PAGE_LIMIT,
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
    client: Langfuse,
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
    client: Langfuse,
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
    client: Langfuse,
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

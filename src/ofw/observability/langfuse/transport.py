"""Narrow GET-only HTTP transport for Langfuse v4 data APIs."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from ofw.observability.langfuse.contracts import (
    CollectionError,
    CollectionErrorCode,
    LangfuseProject,
    TraceWindow,
)
from ofw.observability.langfuse.domain import (
    ObservationPage,
    PageCursor,
    ScorePage,
    TraceId,
)
from ofw.observability.langfuse.trace_query import (
    ObservationRead,
    SpanTextField,
    SpanTextFilter,
    SpanTextMatch,
)
from ofw.observability.langfuse.wire import (
    HealthWire,
    ObservationResponseWire,
    ScoreResponseWire,
)


class LangfuseEndpoint(StrEnum):
    HEALTH = "/api/public/health"
    OBSERVATIONS = "/api/public/v2/observations"
    SCORES = "/api/public/v3/scores"


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class ObservationFilterColumn(StrEnum):
    ENVIRONMENT = "environment"
    TRACE_ID = "traceId"
    OBSERVATION_ID = "id"
    SESSION_ID = "sessionId"
    NAME = "name"
    TYPE = "type"
    LEVEL = "level"
    PARENT_ID = "parentObservationId"
    RELEASE = "release"
    INPUT = "input"
    OUTPUT = "output"
    METADATA = "metadata"


class ObservationFilterOperator(StrEnum):
    EQUALS = "="
    NOT_EQUALS = "<>"
    MATCHES = "matches"


class StringObservationFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["string"] = "string"
    column: ObservationFilterColumn
    operator: ObservationFilterOperator
    value: str


class MetadataObservationFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["stringObject"] = "stringObject"
    column: ObservationFilterColumn = ObservationFilterColumn.METADATA
    key: str
    operator: ObservationFilterOperator
    value: str


ObservationFilter: TypeAlias = (
    StringObservationFilter | MetadataObservationFilter
)
_FILTERS_ADAPTER = TypeAdapter(tuple[ObservationFilter, ...])

_OBSERVATION_FIELD_GROUPS = frozenset(
    {
        "core",
        "basic",
        "time",
        "io",
        "metadata",
        "model",
        "usage",
        "prompt",
        "metrics",
        "trace_context",
    }
)


@dataclass(frozen=True, slots=True)
class ObservationOptions:
    window: TraceWindow | None
    cursor: PageCursor | None
    trace_id: TraceId | None
    observation_id: str | None
    session_id: str | None
    environment: str | None
    name: str | None
    observation_type: str | None
    error: bool | None
    text_filter: SpanTextFilter | None
    parent_observation_id: str | None
    release: str | None
    fields: tuple[str, ...]
    limit: int


class LangfuseHttpClient:
    """Only the two GET operations required by PR2 are exposed."""

    def __init__(
        self,
        project: LangfuseProject,
        *,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES:
            raise CollectionError(
                CollectionErrorCode.INVALID_CONTENT_QUERY,
                "max_response_bytes",
            )
        manifest = project.manifest()
        credentials = project.credentials()
        _validate_dns(manifest.base_url.value, manifest.allow_private_network)
        self._base_url = manifest.base_url.value
        self._allow_private_network = manifest.allow_private_network
        self._client = httpx.Client(
            base_url=manifest.base_url.value,
            auth=httpx.BasicAuth(credentials.public_key, credentials.secret_key),
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self._environment = manifest.environment.value
        self._max_response_bytes = max_response_bytes

    def close(self) -> None:
        self._client.close()

    def check_health(self) -> None:
        response = self._get(
            LangfuseEndpoint.HEALTH,
            (),
            TypeAdapter(HealthWire),
        )
        response.validate_server_version()

    def get_observations(
        self,
        window: TraceWindow | None = None,
        cursor: PageCursor | None = None,
        *,
        trace_id: TraceId | None = None,
        observation_id: str | None = None,
        session_id: str | None = None,
        environment: str | None = None,
        name: str | None = None,
        observation_type: str | None = None,
        error: bool | None = None,
        text_filter: SpanTextFilter | None = None,
        parent_observation_id: str | None = None,
        release: str | None = None,
        fields: tuple[str, ...] = (
            "core",
            "basic",
            "time",
            "io",
            "metadata",
            "model",
            "usage",
            "prompt",
            "metrics",
            "trace_context",
        ),
        limit: int = 1000,
    ) -> ObservationPage:
        options = ObservationOptions(
            window=window,
            cursor=cursor,
            trace_id=trace_id,
            observation_id=observation_id,
            session_id=session_id,
            environment=environment,
            name=name,
            observation_type=observation_type,
            error=error,
            text_filter=text_filter,
            parent_observation_id=parent_observation_id,
            release=release,
            fields=fields,
            limit=limit,
        )
        _validate_options(options)
        response = self._get(
            LangfuseEndpoint.OBSERVATIONS,
            _observation_parameters(options),
            TypeAdapter(ObservationResponseWire),
        )
        return response.normalize()

    def read_observations(self, query: ObservationRead) -> ObservationPage:
        return self.get_observations(
            query.window,
            query.cursor,
            trace_id=query.trace_id,
            observation_id=query.observation_id,
            session_id=query.session_id,
            environment=query.environment,
            name=query.name,
            observation_type=query.observation_type,
            error=query.error,
            text_filter=query.text_filter,
            parent_observation_id=query.parent_observation_id,
            release=query.release,
            fields=tuple(group.value for group in query.fields),
            limit=query.limit,
        )

    def get_scores(
        self,
        window: TraceWindow,
        cursor: PageCursor | None = None,
    ) -> ScorePage:
        parameters = (
            ("fields", "details,subject,annotation"),
            ("limit", "100"),
            ("environment", self._environment),
            ("fromTimestamp", _utc_text(window.start)),
            ("toTimestamp", _utc_text(window.end)),
        ) + (() if cursor is None else (("cursor", cursor.value),))
        response = self._get(
            LangfuseEndpoint.SCORES,
            parameters,
            TypeAdapter(ScoreResponseWire),
        )
        return response.normalize()

    def _get(
        self,
        endpoint: LangfuseEndpoint,
        parameters: tuple[tuple[str, str], ...],
        adapter: TypeAdapter[ResponseModel],
    ) -> ResponseModel:
        _validate_dns(self._base_url, self._allow_private_network)
        try:
            with self._client.stream("GET", endpoint.value, params=parameters) as response:
                _validate_response_status(response, endpoint)
                content = _bounded_content(response, endpoint, self._max_response_bytes)
        except httpx.TimeoutException as error:
            raise CollectionError(CollectionErrorCode.REQUEST_TIMEOUT, endpoint.value) from error
        except httpx.HTTPError as error:
            raise CollectionError(CollectionErrorCode.REQUEST_FAILED, endpoint.value) from error
        try:
            return adapter.validate_json(content)
        except ValidationError as error:
            raise CollectionError(CollectionErrorCode.INVALID_RESPONSE, endpoint.value) from error


def _validate_response_status(response: httpx.Response, endpoint: LangfuseEndpoint) -> None:
    if 300 <= response.status_code < 400:
        raise CollectionError(CollectionErrorCode.REDIRECT_BLOCKED, endpoint.value)
    if response.status_code != 200:
        raise CollectionError(
            CollectionErrorCode.HTTP_STATUS,
            f"{endpoint.value}:{response.status_code}",
        )


def _bounded_content(
    response: httpx.Response,
    endpoint: LangfuseEndpoint,
    maximum_bytes: int,
) -> bytes:
    content = bytearray()
    for chunk in response.iter_bytes():
        if len(content) + len(chunk) > maximum_bytes:
            raise CollectionError(CollectionErrorCode.RESPONSE_TOO_LARGE, endpoint.value)
        content.extend(chunk)
    return bytes(content)


def _validate_options(options: ObservationOptions) -> None:
    _validate_limit(options.limit)
    _validate_fields(options.fields)


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise CollectionError(CollectionErrorCode.INVALID_CONTENT_QUERY, str(limit))


def _validate_fields(fields: tuple[str, ...]) -> None:
    if not fields or not set(fields) <= _OBSERVATION_FIELD_GROUPS:
        raise CollectionError(CollectionErrorCode.INVALID_CONTENT_QUERY, "fields")


def _observation_parameters(options: ObservationOptions) -> tuple[tuple[str, str], ...]:
    base = (
        ("fields", ",".join(options.fields)),
        ("limit", str(options.limit)),
    )
    return (
        base
        + _window_parameters(options.window)
        + _scope_parameters(options)
        + _cursor_parameter(options.cursor)
    )


def _window_parameters(window: TraceWindow | None) -> tuple[tuple[str, str], ...]:
    if window is None:
        return ()
    return (
        ("fromStartTime", _utc_text(window.start)),
        ("toStartTime", _utc_text(window.end)),
    )


def _scope_parameters(options: ObservationOptions) -> tuple[tuple[str, str], ...]:
    if _uses_advanced_filter(options):
        filters = _advanced_filters(options.environment, options)
        return (("filter", _FILTERS_ADAPTER.dump_json(filters).decode()),)
    return _direct_parameters(options.environment, options)


def _uses_advanced_filter(options: ObservationOptions) -> bool:
    return any(
        value is not None
        for value in (
            options.observation_id,
            options.error,
            options.text_filter,
            options.session_id,
            options.release,
        )
    )


def _direct_parameters(
    environment: str | None,
    options: ObservationOptions,
) -> tuple[tuple[str, str], ...]:
    return (
        _parameter("environment", environment)
        + _parameter("traceId", _trace_id(options.trace_id))
        + _parameter("name", options.name)
        + _parameter("type", options.observation_type)
        + _parameter("parentObservationId", options.parent_observation_id)
    )


def _trace_id(trace_id: TraceId | None) -> str | None:
    return None if trace_id is None else trace_id.value


def _parameter(name: str, value: str | None) -> tuple[tuple[str, str], ...]:
    return () if value is None else ((name, value),)


def _cursor_parameter(cursor: PageCursor | None) -> tuple[tuple[str, str], ...]:
    return _parameter("cursor", None if cursor is None else cursor.value)


def _advanced_filters(
    environment: str | None,
    options: ObservationOptions,
) -> tuple[ObservationFilter, ...]:
    candidates = (
        _filter(ObservationFilterColumn.ENVIRONMENT, environment),
        _filter(ObservationFilterColumn.TRACE_ID, _trace_id(options.trace_id)),
        _filter(ObservationFilterColumn.OBSERVATION_ID, options.observation_id),
        _filter(ObservationFilterColumn.SESSION_ID, options.session_id),
        _filter(ObservationFilterColumn.NAME, options.name),
        _filter(ObservationFilterColumn.TYPE, options.observation_type),
        _error_filter(options.error),
        _filter(ObservationFilterColumn.PARENT_ID, options.parent_observation_id),
        _filter(ObservationFilterColumn.RELEASE, options.release),
        _text_filter(options.text_filter),
    )
    return tuple(candidate for candidate in candidates if candidate is not None)


def _filter(
    column: ObservationFilterColumn,
    value: str | None,
) -> ObservationFilter | None:
    if value is None:
        return None
    return StringObservationFilter(
        column=column,
        operator=ObservationFilterOperator.EQUALS,
        value=value,
    )


def _error_filter(error: bool | None) -> ObservationFilter | None:
    if error is None:
        return None
    return StringObservationFilter(
        column=ObservationFilterColumn.LEVEL,
        operator=(
            ObservationFilterOperator.EQUALS
            if error
            else ObservationFilterOperator.NOT_EQUALS
        ),
        value="ERROR",
    )


def _text_filter(text_filter: SpanTextFilter | None) -> ObservationFilter | None:
    if text_filter is None:
        return None
    if text_filter.field is SpanTextField.METADATA:
        return MetadataObservationFilter(
            key=_metadata_key(text_filter),
            operator=_text_operator(text_filter.match),
            value=text_filter.text,
        )
    return StringObservationFilter(
        column=_text_column(text_filter.field),
        operator=_text_operator(text_filter.match),
        value=text_filter.text,
    )


def _metadata_key(text_filter: SpanTextFilter) -> str:
    if text_filter.metadata_key is None:
        raise ValueError("metadata text filter requires metadata_key")
    return text_filter.metadata_key


def _text_column(field: SpanTextField) -> ObservationFilterColumn:
    if field is SpanTextField.INPUT:
        return ObservationFilterColumn.INPUT
    return ObservationFilterColumn.OUTPUT


def _text_operator(match: SpanTextMatch) -> ObservationFilterOperator:
    if match is SpanTextMatch.EXACT:
        return ObservationFilterOperator.EQUALS
    return ObservationFilterOperator.MATCHES


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _validate_dns(base_url: str, allow_private_network: bool) -> None:
    if allow_private_network:
        return
    host = urlsplit(base_url).hostname
    if host is None:
        raise CollectionError(CollectionErrorCode.UNSAFE_HOST, base_url)
    try:
        port = urlsplit(base_url).port or (443 if base_url.startswith("https:") else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise CollectionError(CollectionErrorCode.DNS_RESOLUTION_FAILED, host) from error
    if not addresses:
        raise CollectionError(CollectionErrorCode.DNS_RESOLUTION_FAILED, host)
    for address in addresses:
        raw_address = address[4][0]
        resolved_address = ipaddress.ip_address(raw_address)
        if (
            resolved_address.is_private
            or resolved_address.is_loopback
            or resolved_address.is_link_local
            or resolved_address.is_reserved
            or resolved_address.is_unspecified
            or resolved_address.is_multicast
        ):
            raise CollectionError(CollectionErrorCode.PRIVATE_RESOLUTION, host)

"""Narrow GET-only HTTP transport for Langfuse v4 data APIs."""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime
from enum import StrEnum
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

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


class LangfuseHttpClient:
    """Only the two GET operations required by PR2 are exposed."""

    def __init__(self, project: LangfuseProject, *, timeout_seconds: float = 15.0) -> None:
        manifest = project.manifest()
        credentials = project.credentials()
        _validate_dns(manifest.base_url.value, manifest.allow_private_network)
        self._client = httpx.Client(
            base_url=manifest.base_url.value,
            auth=httpx.BasicAuth(credentials.public_key, credentials.secret_key),
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self._environment = manifest.environment.value

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
        window: TraceWindow,
        cursor: PageCursor | None = None,
    ) -> ObservationPage:
        parameters = (
            ("fields", "core,basic,time,io,metadata,usage,trace_context"),
            ("expandMetadata", "ofw.harness.revision"),
            ("limit", "1000"),
            ("environment", self._environment),
            ("fromStartTime", _utc_text(window.start)),
            ("toStartTime", _utc_text(window.end)),
        ) + (() if cursor is None else (("cursor", cursor.value),))
        response = self._get(
            LangfuseEndpoint.OBSERVATIONS,
            parameters,
            TypeAdapter(ObservationResponseWire),
        )
        return response.normalize()

    def get_scores(
        self,
        window: TraceWindow,
        cursor: PageCursor | None = None,
    ) -> ScorePage:
        parameters = (
            ("fields", "details,subject"),
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
        try:
            response = self._client.get(endpoint.value, params=parameters)
        except httpx.TimeoutException as error:
            raise CollectionError(CollectionErrorCode.REQUEST_TIMEOUT, endpoint.value) from error
        except httpx.HTTPError as error:
            raise CollectionError(CollectionErrorCode.REQUEST_FAILED, endpoint.value) from error
        if 300 <= response.status_code < 400:
            raise CollectionError(CollectionErrorCode.REDIRECT_BLOCKED, endpoint.value)
        if response.status_code != 200:
            raise CollectionError(
                CollectionErrorCode.HTTP_STATUS,
                f"{endpoint.value}:{response.status_code}",
            )
        try:
            return adapter.validate_json(response.content)
        except ValidationError as error:
            raise CollectionError(CollectionErrorCode.INVALID_RESPONSE, endpoint.value) from error


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _validate_dns(base_url: str, allow_private_network: bool) -> None:
    if allow_private_network:
        return
    host = urlsplit(base_url).hostname
    if host is None:
        raise CollectionError(CollectionErrorCode.UNSAFE_HOST, base_url)
    try:
        _, _, addresses = socket.gethostbyname_ex(host)
    except OSError as error:
        raise CollectionError(CollectionErrorCode.DNS_RESOLUTION_FAILED, host) from error
    if not addresses:
        raise CollectionError(CollectionErrorCode.DNS_RESOLUTION_FAILED, host)
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            raise CollectionError(CollectionErrorCode.PRIVATE_RESOLUTION, host)

"""Typed Langfuse connection and collection boundary contracts."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"[A-Z_][A-Z0-9_]*")
LANGFUSE_CONNECTION_SCHEMA_VERSION = 2


class CollectionErrorCode(StrEnum):
    UNSAFE_HOST = "unsafe_host"
    INVALID_ENVIRONMENT = "invalid_environment"
    INVALID_ENVIRONMENT_VARIABLE = "invalid_environment_variable"
    INVALID_WINDOW = "invalid_window"
    OBSERVABILITY_NOT_CONNECTED = "observability_not_connected"
    MISSING_CREDENTIAL = "missing_credential"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    PRIVATE_RESOLUTION = "private_resolution"
    REDIRECT_BLOCKED = "redirect_blocked"
    REQUEST_TIMEOUT = "request_timeout"
    REQUEST_FAILED = "request_failed"
    HTTP_STATUS = "http_status"
    INVALID_RESPONSE = "invalid_response"
    DATABASE_ERROR = "database_error"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    CURSOR_LOOP = "cursor_loop"
    UNSUPPORTED_LANGFUSE_VERSION = "unsupported_langfuse_version"
    INVALID_CONTENT_POLICY = "invalid_content_policy"
    INVALID_CONTENT_QUERY = "invalid_content_query"
    CONTENT_NOT_CAPTURED = "content_not_captured"
    MISSING_REDACTION_VALUE = "missing_redaction_value"


class CollectionError(Exception):
    """Typed Langfuse collection boundary failure."""

    __slots__ = ("code", "subject")

    def __init__(self, code: CollectionErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class EnvironmentName:
    value: str

    def __post_init__(self) -> None:
        if _NAME_PATTERN.fullmatch(self.value) is None:
            raise CollectionError(CollectionErrorCode.INVALID_ENVIRONMENT, self.value)


class ContentCaptureMode(StrEnum):
    METADATA_ONLY = "metadata_only"
    REDACTED = "redacted"


@dataclass(frozen=True, slots=True)
class SecretEnvironmentVariable:
    value: str

    def __post_init__(self) -> None:
        if _ENVIRONMENT_VARIABLE_PATTERN.fullmatch(self.value) is None:
            raise CollectionError(CollectionErrorCode.INVALID_ENVIRONMENT_VARIABLE, self.value)


@dataclass(frozen=True, slots=True)
class ObservationContentPolicy:
    mode: ContentCaptureMode
    maximum_bytes_per_field: int
    secret_environment_variables: tuple[SecretEnvironmentVariable, ...]

    def __post_init__(self) -> None:
        typed_variables = all(
            isinstance(variable, SecretEnvironmentVariable)
            for variable in self.secret_environment_variables
        )
        if (
            not isinstance(self.mode, ContentCaptureMode)
            or not 16 <= self.maximum_bytes_per_field <= 1_048_576
            or not typed_variables
            or len(set(self.secret_environment_variables))
            != len(self.secret_environment_variables)
            or (
                self.mode is ContentCaptureMode.METADATA_ONLY
                and self.secret_environment_variables
            )
        ):
            subject = (
                self.mode.value
                if isinstance(self.mode, ContentCaptureMode)
                else repr(self.mode)
            )
            raise CollectionError(
                CollectionErrorCode.INVALID_CONTENT_POLICY,
                subject,
            )

    @classmethod
    def metadata_only(cls) -> ObservationContentPolicy:
        return cls(ContentCaptureMode.METADATA_ONLY, 16, ())

    @classmethod
    def redacted(
        cls,
        *,
        maximum_bytes_per_field: int,
        secret_environment_variables: tuple[SecretEnvironmentVariable, ...],
    ) -> ObservationContentPolicy:
        return cls(
            ContentCaptureMode.REDACTED,
            maximum_bytes_per_field,
            secret_environment_variables,
        )

    def canonical_json(self) -> str:
        variables = ",".join(
            _quote(variable.value) for variable in self.secret_environment_variables
        )
        return (
            "{"
            f'"mode":{_quote(self.mode.value)},'
            f'"maximum_bytes_per_field":{self.maximum_bytes_per_field},'
            f'"secret_environment_variables":[{variables}]'
            "}"
        )


@dataclass(frozen=True, slots=True)
class LangfuseConnectionId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class LangfuseBaseUrl:
    value: str

    @classmethod
    def parse(cls, value: str, *, allow_private_network: bool) -> LangfuseBaseUrl:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise CollectionError(CollectionErrorCode.UNSAFE_HOST, value)
        if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise CollectionError(CollectionErrorCode.UNSAFE_HOST, value)
        if parsed.hostname is None:
            raise CollectionError(CollectionErrorCode.UNSAFE_HOST, value)
        private_host = _is_private_host(parsed.hostname)
        if parsed.scheme == "https":
            if private_host and not allow_private_network:
                raise CollectionError(CollectionErrorCode.UNSAFE_HOST, value)
        elif not (parsed.scheme == "http" and allow_private_network and private_host):
            raise CollectionError(CollectionErrorCode.UNSAFE_HOST, value)
        return cls(value.rstrip("/"))


@dataclass(frozen=True, slots=True)
class LangfuseConnectionManifest:
    id: LangfuseConnectionId
    base_url: LangfuseBaseUrl
    environment: EnvironmentName
    public_key_environment: SecretEnvironmentVariable
    secret_key_environment: SecretEnvironmentVariable
    allow_private_network: bool
    content_policy: ObservationContentPolicy

    def canonical_json(self) -> str:
        return (
            "{"
            f'"schema_version":{LANGFUSE_CONNECTION_SCHEMA_VERSION},'
            f'"base_url":{_quote(self.base_url.value)},'
            f'"environment":{_quote(self.environment.value)},'
            f'"public_key_environment":{_quote(self.public_key_environment.value)},'
            f'"secret_key_environment":{_quote(self.secret_key_environment.value)},'
            f'"allow_private_network":{_boolean(self.allow_private_network)},'
            f'"content_policy":{self.content_policy.canonical_json()}'
            "}"
        )

    def to_json(self) -> str:
        return f'{{"id":{_quote(str(self.id))},{self.canonical_json()[1:]}'


@dataclass(frozen=True, slots=True)
class LangfuseProject:
    """Runtime connection using environment-backed credentials only."""

    _manifest: LangfuseConnectionManifest

    @classmethod
    def from_env(
        cls,
        *,
        environment: str,
        base_url: str | None = None,
        public_key_environment: str = "LANGFUSE_PUBLIC_KEY",
        secret_key_environment: str = "LANGFUSE_SECRET_KEY",
        allow_private_network: bool = False,
        content_policy: ObservationContentPolicy | None = None,
    ) -> LangfuseProject:
        selected_base_url = (
            base_url
            or os.environ.get("LANGFUSE_BASE_URL")
            or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        )
        url = LangfuseBaseUrl.parse(
            selected_base_url,
            allow_private_network=allow_private_network,
        )
        environment_name = EnvironmentName(environment)
        public_ref = SecretEnvironmentVariable(public_key_environment)
        secret_ref = SecretEnvironmentVariable(secret_key_environment)
        selected_content_policy = content_policy or ObservationContentPolicy.metadata_only()
        payload = (
            f"{LANGFUSE_CONNECTION_SCHEMA_VERSION}\0{url.value}\0"
            f"{environment_name.value}\0"
            f"{public_ref.value}\0{secret_ref.value}\0{allow_private_network}\0"
            f"{selected_content_policy.canonical_json()}"
        )
        connection_id = LangfuseConnectionId(f"lf_{hashlib.sha256(payload.encode()).hexdigest()}")
        return cls(
            LangfuseConnectionManifest(
                id=connection_id,
                base_url=url,
                environment=environment_name,
                public_key_environment=public_ref,
                secret_key_environment=secret_ref,
                allow_private_network=allow_private_network,
                content_policy=selected_content_policy,
            )
        )

    @classmethod
    def from_manifest(cls, manifest: LangfuseConnectionManifest) -> LangfuseProject:
        return cls(manifest)

    def manifest(self) -> LangfuseConnectionManifest:
        return self._manifest

    def credentials(self) -> BasicCredentials:
        public_key = os.environ.get(self._manifest.public_key_environment.value)
        secret_key = os.environ.get(self._manifest.secret_key_environment.value)
        if not public_key:
            raise CollectionError(
                CollectionErrorCode.MISSING_CREDENTIAL,
                self._manifest.public_key_environment.value,
            )
        if not secret_key:
            raise CollectionError(
                CollectionErrorCode.MISSING_CREDENTIAL,
                self._manifest.secret_key_environment.value,
            )
        return BasicCredentials(public_key=public_key, secret_key=secret_key)

    def redaction_values(self) -> tuple[str, ...]:
        credential_variables = (
            self._manifest.public_key_environment,
            self._manifest.secret_key_environment,
        )
        values: list[str] = []
        for variable in credential_variables:
            value = os.environ.get(variable.value)
            if value and value not in values:
                values.append(value)
        for variable in self._manifest.content_policy.secret_environment_variables:
            value = os.environ.get(variable.value)
            if not value:
                raise CollectionError(
                    CollectionErrorCode.MISSING_REDACTION_VALUE,
                    variable.value,
                )
            if value not in values:
                values.append(value)
        return tuple(values)


@dataclass(frozen=True, slots=True)
class TraceWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if (
            self.start.utcoffset() != timedelta(0)
            or self.end.utcoffset() != timedelta(0)
            or self.start >= self.end
        ):
            raise CollectionError(CollectionErrorCode.INVALID_WINDOW, "UTC start must precede end")


@dataclass(frozen=True, slots=True, repr=False)
class BasicCredentials:
    public_key: str
    secret_key: str


def _is_private_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _boolean(value: bool) -> str:
    return "true" if value else "false"

"""GET-only Langfuse v4 transport behavior."""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import pytest

from ofw import (
    CollectionError,
    CollectionErrorCode,
    LangfuseProject,
    ObservationContentPolicy,
    SecretEnvironmentVariable,
    TraceWindow,
)
from ofw.observability.langfuse.domain import ObservationType, ScoreDataType, ScoreSubjectKind
from ofw.observability.langfuse.transport import LangfuseHttpClient
from ofw.observability.langfuse.wire import ObservationResponseWire, ScoreResponseWire

OBSERVATIONS_RESPONSE = r"""
{
  "data": [
    {
      "id": "obs-agent",
      "traceId": "trace-1",
      "startTime": "2026-08-22T00:00:00Z",
      "endTime": "2026-08-22T00:00:05Z",
      "projectId": "project-1",
      "parentObservationId": null,
      "type": "AGENT",
      "isRootObservation": true,
      "name": "backend-engineer",
      "level": "DEFAULT",
      "version": "v17",
      "environment": "production",
      "userId": "user-1",
      "sessionId": "session-1",
      "createdAt": "2026-08-22T00:00:06Z",
      "updatedAt": "2026-08-22T00:00:07Z",
      "input": "{\"task\":\"ship\"}",
      "output": "{\"status\":\"done\"}",
      "metadata": {"ofw.harness.revision": "ofw-revision-1"},
      "usageDetails": {"input": 10, "output": 4},
      "costDetails": {"total": 0.02},
      "totalCost": 0.02,
      "modelId": null,
      "inputPrice": null,
      "outputPrice": null,
      "totalPrice": null,
      "tags": ["production", "chorus"],
      "release": "chorus-17",
      "traceName": "employee-run"
    }
  ],
  "meta": {"cursor": "next-observation-page"}
}
"""

SCORES_RESPONSE = """
{
  "data": [
    {
      "id": "score-1",
      "projectId": "project-1",
      "name": "correctness",
      "value": true,
      "dataType": "BOOLEAN",
      "source": "ANNOTATION",
      "timestamp": "2026-08-22T00:01:00Z",
      "environment": "production",
      "createdAt": "2026-08-22T00:01:01Z",
      "updatedAt": "2026-08-22T00:01:02Z",
      "comment": "reviewed",
      "subject": {"kind": "trace", "id": "trace-1"}
    }
  ],
  "meta": {"limit": 100, "cursor": null}
}
"""


@dataclass(frozen=True, slots=True)
class RequestRecord:
    path: str
    query: tuple[tuple[str, str], ...]
    authorization: str | None


@dataclass(slots=True)
class FixtureState:
    requests: list[RequestRecord] = field(default_factory=list)
    writes: int = 0
    redirect_observations: bool = False
    malformed_observations: bool = False
    observation_delay_seconds: float = 0.0
    health_version: str = "4.7.0"


@dataclass(frozen=True, slots=True)
class FixtureServer:
    base_url: str
    state: FixtureState
    server: ThreadingHTTPServer
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _handler(state: FixtureState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            state.requests.append(
                RequestRecord(
                    path=parsed.path,
                    query=tuple(parse_qsl(parsed.query)),
                    authorization=self.headers.get("Authorization"),
                )
            )
            if parsed.path == "/api/public/v2/observations" and state.redirect_observations:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/private")
                self.end_headers()
                return
            if parsed.path == "/api/public/health":
                payload = f'{{"version":"{state.health_version}","status":"OK"}}'
            elif parsed.path == "/api/public/v2/observations":
                if state.observation_delay_seconds:
                    time.sleep(state.observation_delay_seconds)
                payload = "not-json" if state.malformed_observations else OBSERVATIONS_RESPONSE
            elif parsed.path == "/api/public/v3/scores":
                payload = SCORES_RESPONSE
            else:
                self.send_response(404)
                self.end_headers()
                return
            encoded = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                return

        def do_POST(self) -> None:
            state.writes += 1
            self.send_response(405)
            self.end_headers()

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            del code, size

    return Handler


@pytest.fixture()
def langfuse_server() -> Iterator[FixtureServer]:
    state = FixtureState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture = FixtureServer(f"http://127.0.0.1:{port}", state, server, thread)
    try:
        yield fixture
    finally:
        fixture.stop()


def _project(
    server: FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
    content_policy: ObservationContentPolicy | None = None,
) -> LangfuseProject:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    return LangfuseProject.from_env(
        environment="production",
        base_url=server.base_url,
        allow_private_network=True,
        content_policy=content_policy,
    )


def _window() -> TraceWindow:
    start = datetime(2026, 8, 22, tzinfo=UTC)
    return TraceWindow(start=start, end=start + timedelta(hours=1))


def test_reads_typed_observation_and_score_pages_with_bounded_queries(
    langfuse_server: FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LangfuseHttpClient(_project(langfuse_server, monkeypatch))
    try:
        client.check_health()
        observations = client.get_observations(_window())
        scores = client.get_scores(_window())
    finally:
        client.close()

    observation = observations.records[0]
    score = scores.records[0]
    expected_auth = "Basic " + base64.b64encode(b"pk-test:sk-test").decode()
    observation_query = next(
        request.query
        for request in langfuse_server.state.requests
        if request.path == "/api/public/v2/observations"
    )
    score_query = next(
        request.query
        for request in langfuse_server.state.requests
        if request.path == "/api/public/v3/scores"
    )

    assert observation.type is ObservationType.AGENT
    assert observation.trace_id is not None
    assert observation.trace_id.value == "trace-1"
    assert observation.metadata is not None
    assert observation.metadata.canonical == '{"ofw.harness.revision":"ofw-revision-1"}'
    assert observations.cursor is not None
    assert observations.cursor.value == "next-observation-page"
    assert score.data_type is ScoreDataType.BOOLEAN
    assert score.value is True
    assert score.subject is not None
    assert score.subject.kind is ScoreSubjectKind.TRACE
    assert score.subject.id == "trace-1"
    assert score.comment is None
    assert score.metadata is None
    assert ("fromStartTime", "2026-08-22T00:00:00Z") in observation_query
    assert ("toStartTime", "2026-08-22T01:00:00Z") in observation_query
    assert ("fields", "core,basic,time,metadata,usage,trace_context") in observation_query
    assert ("expandMetadata", "ofw.harness.revision") in observation_query
    assert ("fromTimestamp", "2026-08-22T00:00:00Z") in score_query
    assert ("fields", "subject") in score_query
    assert all(request.authorization == expected_auth for request in langfuse_server.state.requests)
    assert langfuse_server.state.writes == 0


def test_opted_in_io_is_redacted_truncated_and_requested_explicitly(
    langfuse_server: FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACE_SECRET", "ship")
    policy = ObservationContentPolicy.redacted(
        maximum_bytes_per_field=32,
        secret_environment_variables=(SecretEnvironmentVariable("TRACE_SECRET"),),
    )
    client = LangfuseHttpClient(_project(langfuse_server, monkeypatch, policy))
    try:
        page = client.get_observations(_window())
        scores = client.get_scores(_window())
    finally:
        client.close()

    observation = page.records[0]
    assert observation.input_content is not None
    assert observation.output_content is not None
    assert len(page.contents) == 2
    input_content = next(
        content for content in page.contents if content.reference == observation.input_content
    )
    query = next(
        request.query
        for request in langfuse_server.state.requests
        if request.path == "/api/public/v2/observations"
    )
    score_query = next(
        request.query
        for request in langfuse_server.state.requests
        if request.path == "/api/public/v3/scores"
    )
    assert input_content.text == '{"task":"[REDACTED_SECRET]"}'
    assert "ship" not in input_content.text
    assert ("fields", "core,basic,time,io,metadata,usage,trace_context") in query
    assert scores.records[0].comment == "reviewed"
    assert ("fields", "details,subject") in score_query


def test_score_details_follow_content_policy_and_redaction() -> None:
    response = SCORES_RESPONSE.replace(
        '"value": true',
        '"value": "ship dev@example.com"',
    ).replace(
        '"dataType": "BOOLEAN"',
        '"dataType": "TEXT"',
    ).replace(
        '"comment": "reviewed"',
        '"comment": "ship dev@example.com Bearer abcdef123456", '
        '"metadata": {"secret": "ship"}',
    )
    policy = ObservationContentPolicy.redacted(
        maximum_bytes_per_field=128,
        secret_environment_variables=(),
    )

    metadata_only = ScoreResponseWire.model_validate_json(response).normalize().records[0]
    redacted = ScoreResponseWire.model_validate_json(response).normalize(
        policy,
        ("ship",),
    ).records[0]

    assert metadata_only.comment is None
    assert metadata_only.metadata is None
    assert metadata_only.value == "[CONTENT_NOT_CAPTURED]"
    assert redacted.value == "[REDACTED_SECRET] [REDACTED_EMAIL]"
    assert redacted.comment == (
        "[REDACTED_SECRET] [REDACTED_EMAIL] Bearer [REDACTED_TOKEN]"
    )
    assert redacted.metadata is not None
    assert redacted.metadata.canonical == '{"secret":"[REDACTED_SECRET]"}'


def test_wire_redacts_common_identifiers_before_content_addressing() -> None:
    response = OBSERVATIONS_RESPONSE.replace(
        '"input": "{\\"task\\":\\"ship\\"}"',
        '"input": "email dev@example.com Bearer abcdef123456"',
    )
    policy = ObservationContentPolicy.redacted(
        maximum_bytes_per_field=24,
        secret_environment_variables=(),
    )

    page = ObservationResponseWire.model_validate_json(response).normalize(policy, ())

    content = page.contents[0]
    assert "dev@example.com" not in content.text
    assert "abcdef123456" not in content.text
    assert content.reference.truncated
    assert content.reference.byte_count == len(content.text.encode())


def test_persisted_observation_and_score_changes_produce_new_digests() -> None:
    details_policy = ObservationContentPolicy.redacted(
        maximum_bytes_per_field=1024,
        secret_environment_variables=(),
    )
    first_observation = (
        ObservationResponseWire.model_validate_json(OBSERVATIONS_RESPONSE).normalize().records[0]
    )
    changed_observation = (
        ObservationResponseWire.model_validate_json(
            OBSERVATIONS_RESPONSE.replace(
                '"name": "backend-engineer"',
                '"name": "backend-engineer-v2"',
            )
        )
        .normalize()
        .records[0]
    )
    first_score = ScoreResponseWire.model_validate_json(SCORES_RESPONSE).normalize(
        details_policy,
    ).records[0]
    changed_score = (
        ScoreResponseWire.model_validate_json(
            SCORES_RESPONSE.replace('"comment": "reviewed"', '"comment": "reviewed again"')
        )
        .normalize(details_policy)
        .records[0]
    )

    assert first_observation.digest != changed_observation.digest
    assert first_score.digest != changed_score.digest


def test_enrichment_field_changes_produce_new_observation_digests() -> None:
    first_observation = (
        ObservationResponseWire.model_validate_json(OBSERVATIONS_RESPONSE).normalize().records[0]
    )
    enriched_observation = (
        ObservationResponseWire.model_validate_json(
            OBSERVATIONS_RESPONSE.replace('"modelId": null', '"modelId": "gpt-5"')
        )
        .normalize()
        .records[0]
    )

    assert enriched_observation.model_id == "gpt-5"
    assert first_observation.digest != enriched_observation.digest


def test_unknown_upstream_fields_are_ignored() -> None:
    augmented = OBSERVATIONS_RESPONSE.replace(
        '"traceName": "employee-run"',
        '"traceName": "employee-run",\n      "brandNewField": {"nested": true}',
    )
    records = ObservationResponseWire.model_validate_json(augmented).normalize().records

    assert records[0].name == "backend-engineer"


def test_catalog_keeps_only_revision_metadata() -> None:
    augmented = OBSERVATIONS_RESPONSE.replace(
        '"ofw.harness.revision": "ofw-revision-1"',
        '"ofw.harness.revision": "ofw-revision-1", "secret": "discard-me"',
    )
    observation = ObservationResponseWire.model_validate_json(augmented).normalize().records[0]

    assert observation.metadata is not None
    assert observation.metadata.canonical == '{"ofw.harness.revision":"ofw-revision-1"}'


def test_public_ipv6_literal_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    project = LangfuseProject.from_env(
        environment="production",
        base_url="https://[2001:4860:4860::8888]",
    )

    client = LangfuseHttpClient(project)
    client.close()


def test_missing_credential_fails_before_request(
    langfuse_server: FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    project = LangfuseProject.from_env(
        environment="production",
        base_url=langfuse_server.base_url,
        allow_private_network=True,
    )

    with pytest.raises(CollectionError) as raised:
        LangfuseHttpClient(project)

    assert raised.value.code is CollectionErrorCode.MISSING_CREDENTIAL
    assert not langfuse_server.state.requests


def test_redirect_and_malformed_response_fail_closed(
    langfuse_server: FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(langfuse_server, monkeypatch)
    langfuse_server.state.redirect_observations = True
    redirecting = LangfuseHttpClient(project)
    try:
        with pytest.raises(CollectionError) as redirect:
            redirecting.get_observations(_window())
    finally:
        redirecting.close()
    langfuse_server.state.redirect_observations = False
    langfuse_server.state.malformed_observations = True
    malformed = LangfuseHttpClient(project)
    try:
        with pytest.raises(CollectionError) as response:
            malformed.get_observations(_window())
    finally:
        malformed.close()

    assert redirect.value.code is CollectionErrorCode.REDIRECT_BLOCKED
    assert response.value.code is CollectionErrorCode.INVALID_RESPONSE


def test_timeout_is_typed_and_does_not_leak_credentials(
    langfuse_server: FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    langfuse_server.state.observation_delay_seconds = 0.05
    client = LangfuseHttpClient(
        _project(langfuse_server, monkeypatch),
        timeout_seconds=0.01,
    )
    try:
        with pytest.raises(CollectionError) as raised:
            client.get_observations(_window())
    finally:
        client.close()

    assert raised.value.code is CollectionErrorCode.REQUEST_TIMEOUT
    assert "pk-test" not in str(raised.value)
    assert "sk-test" not in str(raised.value)


def test_langfuse_v3_fails_with_explicit_version_error(
    langfuse_server: FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    langfuse_server.state.health_version = "3.141.0"
    client = LangfuseHttpClient(_project(langfuse_server, monkeypatch))
    try:
        with pytest.raises(CollectionError) as raised:
            client.check_health()
    finally:
        client.close()

    assert raised.value.code is CollectionErrorCode.UNSUPPORTED_LANGFUSE_VERSION

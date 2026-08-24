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
from pydantic import JsonValue, TypeAdapter

from ofw import (
    CollectionError,
    CollectionErrorCode,
    LangfuseProject,
    TraceWindow,
)
from ofw.observability.langfuse.domain import ObservationType, ScoreDataType, ScoreSubjectKind
from ofw.observability.langfuse.transport import LangfuseHttpClient
from ofw.observability.langfuse.wire import ObservationResponseWire, ScoreResponseWire

_JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

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
      "metadata": {"ofw.harness.revision": "ofw-revision-1", "customer": "acme"},
      "providedModelName": "gpt-5",
      "internalModelId": "model-1",
      "modelParameters": {"temperature": 0.2},
      "usageDetails": {"input": 10, "output": 4},
      "costDetails": {"total": 0.02},
      "totalCost": 0.02,
      "inputUsage": 10,
      "outputUsage": 4,
      "totalUsage": 14,
      "promptId": "prompt-1",
      "promptName": "support-agent",
      "promptVersion": 7,
      "latency": 5.0,
      "timeToFirstToken": 0.4,
      "modelId": "model-1",
      "inputPrice": "0.01",
      "outputPrice": "0.02",
      "totalPrice": null,
      "tags": ["production", "chorus"],
      "release": "chorus-17",
      "traceName": "employee-run",
      "futureLangfuseField": {"preserved": true}
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
      "configId": "config-1",
      "metadata": {"rubric": "strict"},
      "authorUserId": "reviewer-1",
      "queueId": "queue-1",
      "subject": {"kind": "trace", "id": "trace-1"},
      "futureScoreField": ["preserved"]
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
) -> LangfuseProject:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    return LangfuseProject.from_env(
        environment="production",
        base_url=server.base_url,
        allow_private_network=True,
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
    assert observation.metadata.canonical == (
        '{"customer":"acme","ofw.harness.revision":"ofw-revision-1"}'
    )
    assert observations.cursor is not None
    assert observations.cursor.value == "next-observation-page"
    assert score.data_type is ScoreDataType.BOOLEAN
    assert score.value is True
    assert score.subject is not None
    assert score.subject.kind is ScoreSubjectKind.TRACE
    assert score.subject.id == "trace-1"
    assert score.comment == "reviewed"
    assert score.metadata is not None
    observation_raw = _JSON_OBJECT_ADAPTER.validate_json(observation.raw.canonical)
    score_raw = _JSON_OBJECT_ADAPTER.validate_json(score.raw.canonical)
    assert observation_raw["futureLangfuseField"] == {"preserved": True}
    assert observation_raw["promptName"] == "support-agent"
    assert score_raw["futureScoreField"] == ["preserved"]
    assert score_raw["authorUserId"] == "reviewer-1"
    assert ("fromStartTime", "2026-08-22T00:00:00Z") in observation_query
    assert ("toStartTime", "2026-08-22T01:00:00Z") in observation_query
    assert (
        "fields",
        "core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context",
    ) in observation_query
    assert ("fromTimestamp", "2026-08-22T00:00:00Z") in score_query
    assert ("fields", "details,subject,annotation") in score_query
    assert all(request.authorization == expected_auth for request in langfuse_server.state.requests)
    assert langfuse_server.state.writes == 0


def test_full_input_and_output_are_preserved_without_redaction_or_truncation(
    langfuse_server: FixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LangfuseHttpClient(_project(langfuse_server, monkeypatch))
    try:
        page = client.get_observations(_window())
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
    assert input_content.text == '{"task":"ship"}'
    assert (
        "fields",
        "core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context",
    ) in query
    assert input_content.reference.byte_count == len(input_content.text.encode())


def test_persisted_observation_and_score_changes_produce_new_digests() -> None:
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
    first_score = ScoreResponseWire.model_validate_json(SCORES_RESPONSE).normalize().records[0]
    changed_score = (
        ScoreResponseWire.model_validate_json(
            SCORES_RESPONSE.replace('"comment": "reviewed"', '"comment": "reviewed again"')
        )
        .normalize()
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
            OBSERVATIONS_RESPONSE.replace('"modelId": "model-1"', '"modelId": "gpt-5"')
        )
        .normalize()
        .records[0]
    )

    assert enriched_observation.model_id == "gpt-5"
    assert first_observation.digest != enriched_observation.digest


def test_unknown_upstream_fields_are_preserved_in_raw_record() -> None:
    augmented = OBSERVATIONS_RESPONSE.replace(
        '"traceName": "employee-run"',
        '"traceName": "employee-run",\n      "brandNewField": {"nested": true}',
    )
    records = ObservationResponseWire.model_validate_json(augmented).normalize().records

    raw = _JSON_OBJECT_ADAPTER.validate_json(records[0].raw.canonical)
    assert raw["brandNewField"] == {"nested": True}


def test_catalog_keeps_all_observation_metadata() -> None:
    augmented = OBSERVATIONS_RESPONSE.replace(
        '"ofw.harness.revision": "ofw-revision-1"',
        '"ofw.harness.revision": "ofw-revision-1", "secret": "discard-me"',
    )
    observation = ObservationResponseWire.model_validate_json(augmented).normalize().records[0]

    assert observation.metadata is not None
    assert observation.metadata.canonical == (
        '{"customer":"acme","ofw.harness.revision":"ofw-revision-1",'
        '"secret":"discard-me"}'
    )


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

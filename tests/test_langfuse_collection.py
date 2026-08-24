"""End-to-end read-only Langfuse collection behavior."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from ofw import (
    CollectionError,
    CollectionErrorCode,
    ContentCaptureMode,
    Harness,
    HarnessRevision,
    LangfuseProject,
    ObservationContentField,
    ObservationContentMatch,
    ObservationContentPolicy,
    ObservationContentQuery,
    Tool,
    TraceWindow,
    ofw,
)
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    CollectionCapabilityReason,
    CollectionSyncId,
    SyncStream,
    TraceId,
)
from ofw.observability.langfuse.store import CollectionStore


@dataclass(frozen=True, slots=True)
class RequestRecord:
    path: str
    cursor: str | None


@dataclass(slots=True)
class CollectionFixtureState:
    observation_count: int = 1001
    score_count: int = 1
    revision_id: str | None = None
    requests: list[RequestRecord] = field(default_factory=list)
    writes: int = 0
    fail_second_observation_page_once: bool = False
    second_page_failed: bool = False
    repeat_observation_cursor: bool = False


@dataclass(frozen=True, slots=True)
class CollectionFixtureServer:
    base_url: str
    state: CollectionFixtureState
    server: ThreadingHTTPServer
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _observation_json(index: int, revision_id: str | None) -> str:
    observation_id = f"obs-{index:04d}"
    parent = "null" if index == 0 else '"obs-0000"'
    observation_type = "AGENT" if index == 0 else "TOOL"
    root = "true" if index == 0 else "false"
    metadata = (
        "null"
        if revision_id is None or index != 0
        else f'{{"ofw.harness.revision":"{revision_id}"}}'
    )
    return (
        "{"
        f'"id":"{observation_id}",'
        '"traceId":"trace-1",'
        f'"startTime":"2026-08-22T00:00:{index % 60:02d}Z",'
        '"endTime":"2026-08-22T00:01:00Z",'
        '"projectId":"project-1",'
        f'"parentObservationId":{parent},'
        f'"type":"{observation_type}",'
        f'"isRootObservation":{root},'
        '"environment":"production",'
        '"sessionId":"session-1",'
        f'"metadata":{metadata},'
        f'"input":"request {observation_id} refund failed",'
        f'"output":"result {observation_id}",'
        '"release":"chorus-17",'
        '"modelId":null,'
        '"inputPrice":null,'
        '"outputPrice":null,'
        '"totalPrice":null'
        "}"
    )


def _observations_response(state: CollectionFixtureState, cursor: str | None) -> str:
    if cursor is None:
        end = min(state.observation_count, 1000)
        records = ",".join(_observation_json(index, state.revision_id) for index in range(end))
        if state.repeat_observation_cursor:
            next_cursor = '"repeated-cursor"'
        else:
            next_cursor = '"obs-page-2"' if state.observation_count > 1000 else "null"
    else:
        records = ",".join(
            _observation_json(index, state.revision_id)
            for index in range(1000, state.observation_count)
        )
        next_cursor = '"repeated-cursor"' if state.repeat_observation_cursor else "null"
    return f'{{"data":[{records}],"meta":{{"cursor":{next_cursor}}}}}'


def _scores_response(state: CollectionFixtureState, cursor: str | None) -> str:
    if cursor is not None or state.score_count == 0:
        return '{"data":[],"meta":{"limit":100,"cursor":null}}'
    return """
    {
      "data": [{
        "id": "score-1",
        "projectId": "project-1",
        "name": "correctness",
        "value": true,
        "dataType": "BOOLEAN",
        "source": "ANNOTATION",
        "timestamp": "2026-08-22T00:02:00Z",
        "environment": "production",
        "createdAt": "2026-08-22T00:02:01Z",
        "updatedAt": "2026-08-22T00:02:02Z",
        "subject": {"kind": "trace", "id": "trace-1"}
      }],
      "meta": {"limit": 100, "cursor": "score-page-2"}
    }
    """


def _handler(state: CollectionFixtureState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            cursor = next(
                (value for key, value in parse_qsl(parsed.query) if key == "cursor"),
                None,
            )
            state.requests.append(RequestRecord(parsed.path, cursor))
            if (
                parsed.path == "/api/public/v2/observations"
                and cursor == "obs-page-2"
                and state.fail_second_observation_page_once
                and not state.second_page_failed
            ):
                state.second_page_failed = True
                self.send_response(503)
                self.end_headers()
                return
            if parsed.path == "/api/public/health":
                payload = '{"version":"4.7.0","status":"OK"}'
            elif parsed.path == "/api/public/v2/observations":
                payload = _observations_response(state, cursor)
            elif parsed.path == "/api/public/v3/scores":
                payload = _scores_response(state, cursor)
            else:
                self.send_response(404)
                self.end_headers()
                return
            encoded = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            state.writes += 1
            self.send_response(405)
            self.end_headers()

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            del code, size

    return Handler


@pytest.fixture()
def collection_server() -> Iterator[CollectionFixtureServer]:
    state = CollectionFixtureState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture = CollectionFixtureServer(f"http://127.0.0.1:{port}", state, server, thread)
    try:
        yield fixture
    finally:
        fixture.stop()


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _revision(
    tmp_path: Path,
    server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
    content_policy: ObservationContentPolicy | None = None,
) -> HarnessRevision:
    root = tmp_path / "agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    (root / "tool.py").write_text("def run(): pass\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    project = LangfuseProject.from_env(
        environment="production",
        base_url=server.base_url,
        allow_private_network=True,
        content_policy=content_policy,
    )
    harness = Harness("fixture-agent", root=root)
    harness.connect_prompt(ofw.editable(Path("prompt.md")))
    harness.connect_tools(Tool(name="run", source=ofw.editable(Path("tool.py"))))
    harness.connect_observability(project)
    return harness.process()


def _window() -> TraceWindow:
    start = datetime(2026, 8, 22, tzinfo=UTC)
    return TraceWindow(start, start + timedelta(hours=1))


def test_refreshes_completed_window_without_stale_membership(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision(tmp_path, collection_server, monkeypatch)
    collection_server.state.revision_id = str(revision.id)
    store_path = tmp_path / "collection.sqlite"

    first = ofw.collect(revision, window=_window(), store_path=store_path)
    collection_server.state.observation_count = 1002
    repeated = ofw.collect(revision, window=_window(), store_path=store_path)
    collection_server.state.observation_count = 1
    collection_server.state.score_count = 0
    reduced = ofw.collect(revision, window=_window(), store_path=store_path)

    assert first.observation_count == 1001
    assert repeated.observation_count == 1002
    assert first.score_count == 1
    assert len(first.traces) == 1
    assert len(first.traces[0].observation_ids) == 1001
    assert first.traces[0].root_observation_ids[0].value == "obs-0000"
    assert first.traces[0].score_ids[0].value == "score-1"
    assert first.traces[0].attribution is AttributionLevel.EXACT
    assert first.capability is CollectionCapabilityReason.READY
    assert reduced.observation_count == 1
    assert reduced.score_count == 0
    assert collection_server.state.writes == 0


def test_missing_revision_metadata_is_collected_but_not_fit_ready(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_server.state.observation_count = 2
    revision = _revision(tmp_path, collection_server, monkeypatch)

    result = ofw.collect(
        revision,
        window=_window(),
        store_path=tmp_path / "collection.sqlite",
    )

    assert result.observation_count == 2
    assert result.capability is CollectionCapabilityReason.MISSING_REVISION_ATTRIBUTION


def test_wrong_revision_metadata_is_ambiguous_and_not_fit_ready(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_server.state.observation_count = 2
    revision = _revision(tmp_path, collection_server, monkeypatch)
    collection_server.state.revision_id = "ofw-different-revision"

    result = ofw.collect(
        revision,
        window=_window(),
        store_path=tmp_path / "collection.sqlite",
    )

    assert result.traces[0].attribution is AttributionLevel.AMBIGUOUS
    assert result.capability is CollectionCapabilityReason.AMBIGUOUS_REVISION_ATTRIBUTION


def test_empty_window_reports_no_traces(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_server.state.observation_count = 0
    revision = _revision(tmp_path, collection_server, monkeypatch)

    result = ofw.collect(
        revision,
        window=_window(),
        store_path=tmp_path / "collection.sqlite",
    )

    assert result.observation_count == 0
    assert not result.traces
    assert result.capability is CollectionCapabilityReason.NO_TRACES


def test_failed_second_page_resumes_from_committed_cursor(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_server.state.fail_second_observation_page_once = True
    revision = _revision(tmp_path, collection_server, monkeypatch)
    collection_server.state.revision_id = str(revision.id)
    store_path = tmp_path / "collection.sqlite"

    with pytest.raises(CollectionError):
        ofw.collect(revision, window=_window(), store_path=store_path)

    store = CollectionStore(store_path)
    try:
        observation_sync = CollectionSyncId.for_collection(
            revision,
            _window(),
            SyncStream.OBSERVATIONS,
        )
        checkpoint = store.checkpoint(observation_sync, SyncStream.OBSERVATIONS)
        assert checkpoint is not None
        assert checkpoint.cursor is not None
        assert checkpoint.cursor.value == "obs-page-2"
        assert len(store.observations(observation_sync)) == 1000
    finally:
        store.close()

    resumed = ofw.collect(revision, window=_window(), store_path=store_path)
    observation_requests = tuple(
        request
        for request in collection_server.state.requests
        if request.path == "/api/public/v2/observations"
    )
    assert resumed.observation_count == 1001
    assert observation_requests[-1].cursor == "obs-page-2"


def test_failed_refresh_keeps_previous_snapshot_and_resumes(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision(tmp_path, collection_server, monkeypatch)
    collection_server.state.revision_id = str(revision.id)
    store_path = tmp_path / "collection.sqlite"
    first = ofw.collect(revision, window=_window(), store_path=store_path)
    collection_server.state.observation_count = 1002
    collection_server.state.fail_second_observation_page_once = True

    with pytest.raises(CollectionError):
        ofw.collect(revision, window=_window(), store_path=store_path)

    store = CollectionStore(store_path)
    try:
        assert len(store.observations(first.observation_sync_id)) == 1001
    finally:
        store.close()

    refreshed = ofw.collect(revision, window=_window(), store_path=store_path)

    assert refreshed.observation_count == 1002


def test_repeated_cursor_fails_instead_of_looping(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_server.state.observation_count = 1
    collection_server.state.repeat_observation_cursor = True
    revision = _revision(tmp_path, collection_server, monkeypatch)

    with pytest.raises(CollectionError) as raised:
        ofw.collect(
            revision,
            window=_window(),
            store_path=tmp_path / "collection.sqlite",
        )

    assert raised.value.code is CollectionErrorCode.CURSOR_LOOP


def test_permissioned_content_can_be_searched_and_read_through_bounded_api(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_server.state.observation_count = 2
    policy = ObservationContentPolicy.redacted(
        maximum_bytes_per_field=4096,
        secret_environment_variables=(),
    )
    revision = _revision(tmp_path, collection_server, monkeypatch, policy)
    collection_server.state.revision_id = str(revision.id)
    result = ofw.collect(
        revision,
        window=_window(),
        store_path=tmp_path / "collection.sqlite",
    )

    hits = ofw.search_observation_content(
        result,
        ObservationContentQuery(
            "refund failed",
            ObservationContentMatch.TOKEN_PHRASE,
            ObservationContentField.INPUT,
            TraceId("trace-1"),
            10,
            100,
        ),
    )
    trajectory = ofw.read_trace_observations(result, TraceId("trace-1"), 10)
    content = ofw.read_observation_content(result, hits[0].reference)

    assert result.content_policy.mode is ContentCaptureMode.REDACTED
    assert len(hits) == 2
    assert len(trajectory) == 2
    assert content.text.startswith("request obs-")


def test_metadata_only_collection_denies_content_search(
    tmp_path: Path,
    collection_server: CollectionFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_server.state.observation_count = 1
    revision = _revision(tmp_path, collection_server, monkeypatch)
    collection_server.state.revision_id = str(revision.id)
    result = ofw.collect(
        revision,
        window=_window(),
        store_path=tmp_path / "collection.sqlite",
    )

    with pytest.raises(CollectionError) as raised:
        ofw.search_observation_content(
            result,
            ObservationContentQuery(
                "refund failed",
                ObservationContentMatch.TOKEN_PHRASE,
                ObservationContentField.ANY,
                None,
                10,
                100,
            ),
        )

    assert raised.value.code is CollectionErrorCode.CONTENT_NOT_CAPTURED

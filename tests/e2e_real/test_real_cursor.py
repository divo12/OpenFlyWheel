"""Opt-in real-environment E2E (never runs in default CI)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tests.agent_helpers import episode_request, setup_agent_pipeline
from tests.book_helpers import (
    boundary_id_for_slug,
    list_proposals,
    owner_identity,
    promote_proposal,
)
from tests.e2e_real.helpers import (
    anchor_ids_for_episode,
    assert_episode_exists,
    bounded_hash_inventory,
    copy_bounded_source_tree,
    count_table_rows,
    discover_compatible_project_hooks,
    discover_real_cursor_transcript,
    episode_ids_for_workspace,
    foreign_cursor_hooks_fixture_bytes,
    proposals_linked_to_episode,
    real_arceus_root,
    real_cursor_home,
    real_cursor_hooks_guard_path,
    require_e2e_real,
    sha256_file,
    transcript_has_deterministic_proposal_signal,
)
from tests.helpers import _lock_slugs_for_fixture, onboard_and_lock

from openflywheel.application.agent_worker import BackgroundWorkerService
from openflywheel.application.book_app import BookApplication
from openflywheel.application.ingest_app import IngestApplication
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.connectors.agents.cursor_installer import CursorInstaller
from openflywheel.connectors.agents.path_guard import resolve_trusted_transcript_roots
from openflywheel.connectors.agents.platform import generated_marker
from openflywheel.connectors.agents.transcript import load_canonical_session
from openflywheel.contracts.book import BookContextRequest
from openflywheel.contracts.enums import DeploymentMode, PlatformKind, TruthSection
from openflywheel.contracts.ids import AgentSessionId
from openflywheel.contracts.pydantic_json import model_dump_object_dict
from openflywheel.contracts.workspace import WorkspaceInitRequest
from openflywheel.mcp.server import McpBookServer
from openflywheel.onboarding.locate import scan_fixture_root
from openflywheel.store.repos.claim_repo import SqliteClaimRepository

TINY_SYSTEM_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "tiny-system"
_CURSOR_MARKER = generated_marker(PlatformKind.CURSOR)


@pytest.mark.e2e_real
def test_real_cursor_install_cycle_is_read_only_on_source_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_e2e_real()
    cursor_home = real_cursor_home()
    if cursor_home is None:
        pytest.skip("No real Cursor home found")

    guard_path = real_cursor_hooks_guard_path(cursor_home)
    if guard_path is None:
        pytest.skip("No real ~/.cursor/hooks.json to guard")

    guard_before_hash = sha256_file(guard_path)

    temp_project = tmp_path / "project"
    temp_ofw_home = tmp_path / "ofw-home"
    temp_cursor_home = tmp_path / "unused-cursor-home"
    temp_project.mkdir()
    temp_ofw_home.mkdir()
    temp_cursor_home.mkdir()
    project_hooks = temp_project / ".cursor" / "openflywheel-hooks.json"
    project_hooks.parent.mkdir(parents=True)

    real_project_hooks = discover_compatible_project_hooks()
    if real_project_hooks is not None:
        shutil.copy2(real_project_hooks, project_hooks)
    else:
        project_hooks.write_bytes(foreign_cursor_hooks_fixture_bytes())

    monkeypatch.setenv("OFW_HOME", str(temp_ofw_home))
    monkeypatch.setenv("CURSOR_HOME", str(temp_cursor_home))
    monkeypatch.setenv("CLAUDE_CONFIG_HOME", str(tmp_path / "unused-claude-home"))
    (tmp_path / "unused-claude-home").mkdir()

    cursor_installer = CursorInstaller()
    with patch.dict(
        os.environ, {"OFW_HOME": str(temp_ofw_home), "OFW_PROJECT_ROOT": str(temp_project)}
    ):
        install_result = cursor_installer.install(
            target_home=str(temp_cursor_home),
            project_root=str(temp_project),
        )
    assert install_result.error is None

    installed_diag = cursor_installer.diagnostics(
        target_home=str(temp_cursor_home),
        project_root=str(temp_project),
    )
    assert installed_diag.error is None
    assert installed_diag.data is not None
    assert installed_diag.data.installed is True

    skill_path = temp_project / ".cursor" / "skills" / "openflywheel" / "SKILL.md"
    rule_path = temp_project / ".cursor" / "rules" / "openflywheel.mdc"
    assert skill_path.is_file()
    assert rule_path.is_file()
    assert _CURSOR_MARKER in skill_path.read_text(encoding="utf-8")
    assert _CURSOR_MARKER in rule_path.read_text(encoding="utf-8")

    uninstall_result = cursor_installer.uninstall(
        target_home=str(temp_cursor_home),
        project_root=str(temp_project),
    )
    assert uninstall_result.error is None

    removed_diag = cursor_installer.diagnostics(
        target_home=str(temp_cursor_home),
        project_root=str(temp_project),
    )
    assert removed_diag.error is None
    assert removed_diag.data is not None
    assert removed_diag.data.installed is False
    assert not skill_path.exists()
    assert not rule_path.exists()

    hooks_after = json.loads(project_hooks.read_text(encoding="utf-8"))
    assert hooks_after["hooks"][0]["command"] == "echo foreign-hook"
    assert "ofw agent hook" not in json.dumps(hooks_after)

    guard_after_hash = sha256_file(guard_path)
    assert guard_before_hash == guard_after_hash


@pytest.mark.e2e_real
def test_real_cursor_transcript_projection_and_episode_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_e2e_real()
    cursor_home = real_cursor_home()
    if cursor_home is None:
        pytest.skip("No real Cursor home found")

    source_transcript = discover_real_cursor_transcript(cursor_home)
    if source_transcript is None:
        pytest.skip("No Cursor transcript jsonl files found")

    before_hash = sha256_file(source_transcript)
    expect_worker_proposals = transcript_has_deterministic_proposal_signal(source_transcript)

    temp_ofw_home = tmp_path / "ofw-home"
    temp_agent_home = tmp_path / "cursor-home"
    temp_project = tmp_path / "project"
    temp_ofw_home.mkdir()
    temp_agent_home.mkdir()
    temp_project.mkdir()
    temp_transcript = temp_agent_home / ".cursor" / "projects" / "real-session" / "transcript.jsonl"
    temp_transcript.parent.mkdir(parents=True)
    shutil.copy2(source_transcript, temp_transcript)

    monkeypatch.setenv("OFW_HOME", str(temp_ofw_home))

    ws = WorkspaceService()
    init = ws.init_workspace(
        WorkspaceInitRequest(
            name="RealCursorCo",
            home=str(temp_ofw_home),
            deployment_mode=DeploymentMode.LOCAL,
        )
    )
    assert init.error is None

    fixture_root = TINY_SYSTEM_FIXTURE
    workspace_id, book, home = setup_agent_pipeline(temp_ofw_home, fixture_root)
    database = WorkspaceService().load_database(home)

    episodes_before = count_table_rows(database, "episodes", workspace_id)
    proposals_before = count_table_rows(database, "proposals", workspace_id)
    with database.read() as conn:
        claims_before = len(SqliteClaimRepository().list_active_for_workspace(conn, workspace_id))
    episode_ids_before = episode_ids_for_workspace(database, workspace_id)

    roots = resolve_trusted_transcript_roots(
        agent_home=str(temp_agent_home),
        project_root=str(temp_project),
    )
    assert roots.error is None
    assert roots.data is not None
    loaded = load_canonical_session(
        platform=PlatformKind.CURSOR,
        transcript_path=temp_transcript,
        session_ref="real-session",
        session_id=AgentSessionId("real-session-id"),
        project_root=str(temp_project),
        allowed_roots=roots.data,
    )
    assert loaded.error is None
    assert loaded.data is not None
    roles = {message.role for message in loaded.data.messages}
    assert roles <= {"user", "assistant"}
    assert len(loaded.data.messages) >= 1

    request = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CURSOR,
        session_ref="real-session",
        transcript_path=temp_transcript,
        agent_home=str(temp_agent_home),
        project_root=str(temp_project),
        fixture_root=fixture_root,
    )
    recorded = book.episode_record(request)
    assert recorded.error is None
    assert recorded.data is not None
    new_episode_id = recorded.data.episode_id
    assert_episode_exists(database, new_episode_id)

    episodes_after_record = count_table_rows(database, "episodes", workspace_id)
    proposals_after_record = count_table_rows(database, "proposals", workspace_id)
    with database.read() as conn:
        claims_after_record = len(
            SqliteClaimRepository().list_active_for_workspace(conn, workspace_id)
        )
    episode_ids_after_record = episode_ids_for_workspace(database, workspace_id)

    assert episodes_after_record - episodes_before == 1
    assert new_episode_id in episode_ids_after_record
    assert new_episode_id not in episode_ids_before
    assert proposals_after_record - proposals_before == 0
    assert claims_after_record - claims_before == 0

    worker = BackgroundWorkerService(database)
    processed = worker.process_next()
    assert processed.error is None
    assert processed.data is not None
    worker_created = processed.data

    proposals_after_worker = count_table_rows(database, "proposals", workspace_id)
    proposal_delta = proposals_after_worker - proposals_before
    assert proposal_delta >= 0
    if expect_worker_proposals:
        assert proposal_delta >= 1
        assert worker_created >= 1
    else:
        assert proposal_delta == 0 or worker_created == 0

    linked = proposals_linked_to_episode(database, workspace_id, new_episode_id)
    if worker_created > 0:
        assert linked
        anchors = anchor_ids_for_episode(database, new_episode_id)
        assert anchors
        for proposal_id in linked:
            proposal = next(p for p in list_proposals(home, workspace_id) if p.id == proposal_id)
            assert proposal.anchor_ids
            assert any(aid in anchors for aid in proposal.anchor_ids)

    with database.read() as conn:
        claims_after_worker = len(
            SqliteClaimRepository().list_active_for_workspace(conn, workspace_id)
        )
    assert claims_after_worker - claims_before == 0

    after_hash = sha256_file(source_transcript)
    assert before_hash == after_hash


@pytest.mark.e2e_real
def test_real_arceus_root_locate_ingest_is_read_only(tmp_path: Path) -> None:
    require_e2e_real()
    arceus_root = real_arceus_root()
    if arceus_root is None:
        pytest.skip("Set OFW_ARCEUS_ROOT to a checkout directory")

    before = bounded_hash_inventory(arceus_root, max_files=500)

    temp_ofw_home = tmp_path / "ofw-home"
    temp_fixture = tmp_path / "fixture-copy"
    temp_ofw_home.mkdir()
    copied = copy_bounded_source_tree(arceus_root, temp_fixture, max_files=50)
    if copied == 0:
        pytest.skip("No locateable repos copied from OFW_ARCEUS_ROOT")

    candidates = scan_fixture_root(temp_fixture)
    assert len(candidates) >= 2

    ws = WorkspaceService()
    init = ws.init_workspace(
        WorkspaceInitRequest(
            name="RealRootCo",
            home=str(temp_ofw_home),
            deployment_mode=DeploymentMode.LOCAL,
        )
    )
    assert init.error is None
    onboard_and_lock(temp_ofw_home, temp_fixture)

    database = ws.load_database(temp_ofw_home)
    config = ws.read_config(temp_ofw_home)
    ingest = IngestApplication(database)
    book = BookApplication(database)
    ingested = ingest.run_fixture_ingest(
        workspace_id=config.workspace_id,
        fixture_root=temp_fixture,
    )
    assert ingested.error is None
    assert book.extract(workspace_id=config.workspace_id).error is None

    proposals = list_proposals(temp_ofw_home, config.workspace_id)
    assert len(proposals) >= 1
    u3_u4 = {p.section for p in proposals if p.section in (TruthSection.U3, TruthSection.U4)}
    assert TruthSection.U3 in u3_u4 or TruthSection.U4 in u3_u4

    gaps = book.coverage_gaps(workspace_id=config.workspace_id)
    assert gaps.error is None
    assert gaps.data is not None
    gap_sections = {g.section for g in gaps.data.gaps if g.section is not None}
    assert TruthSection.U5 in gap_sections
    assert TruthSection.U6 in gap_sections
    assert TruthSection.U7 in gap_sections

    owner_alpha = owner_identity(temp_ofw_home, config.workspace_id, "Owner Alpha")
    alpha_slug, _beta_slug = _lock_slugs_for_fixture(temp_fixture)
    alpha_boundary = boundary_id_for_slug(temp_ofw_home, config.workspace_id, alpha_slug)
    alpha_prop = next(
        p
        for p in proposals
        if p.boundary_id == alpha_boundary and p.section in (TruthSection.U3, TruthSection.U4)
    )
    verified = promote_proposal(
        book,
        workspace_id=config.workspace_id,
        proposal_id=alpha_prop.id,
        verifier_id=owner_alpha,
    )
    assert verified.error is None

    ctx = book.book_context(
        BookContextRequest(
            workspace_id=config.workspace_id,
            identity_id=owner_alpha,
            query="package",
        )
    )
    assert ctx.error is None
    assert ctx.data is not None
    assert len(ctx.data.packet.claims) >= 1
    assert ctx.data.packet.anchors

    episode_count = count_table_rows(database, "episodes", config.workspace_id)
    claim_count = count_table_rows(database, "claims", config.workspace_id)
    assert episode_count >= 1
    assert claim_count >= 1

    after = bounded_hash_inventory(arceus_root, max_files=500)
    assert before == after


@pytest.mark.e2e_real
def test_real_arceus_workspace_mcp_book_context_parity(tmp_path: Path) -> None:
    require_e2e_real()
    arceus_root = real_arceus_root()
    if arceus_root is None:
        pytest.skip("Set OFW_ARCEUS_ROOT to a checkout directory")

    before_inventory = bounded_hash_inventory(arceus_root, max_files=500)

    temp_ofw_home = tmp_path / "ofw-home"
    temp_fixture = tmp_path / "fixture-copy"
    temp_ofw_home.mkdir()
    copied = copy_bounded_source_tree(arceus_root, temp_fixture, max_files=50)
    if copied == 0:
        pytest.skip("No locateable repos copied from OFW_ARCEUS_ROOT")

    ws = WorkspaceService()
    init = ws.init_workspace(
        WorkspaceInitRequest(
            name="RealMcpCo",
            home=str(temp_ofw_home),
            deployment_mode=DeploymentMode.LOCAL,
        )
    )
    assert init.error is None
    onboard_and_lock(temp_ofw_home, temp_fixture)

    database = ws.load_database(temp_ofw_home)
    config = ws.read_config(temp_ofw_home)
    ingest = IngestApplication(database)
    book = BookApplication(database)
    assert (
        ingest.run_fixture_ingest(
            workspace_id=config.workspace_id,
            fixture_root=temp_fixture,
        ).error
        is None
    )
    assert book.extract(workspace_id=config.workspace_id).error is None

    owner_alpha = owner_identity(temp_ofw_home, config.workspace_id, "Owner Alpha")
    alpha_slug, _beta_slug = _lock_slugs_for_fixture(temp_fixture)
    alpha_boundary = boundary_id_for_slug(temp_ofw_home, config.workspace_id, alpha_slug)
    proposals = list_proposals(temp_ofw_home, config.workspace_id)
    alpha_prop = next(
        p
        for p in proposals
        if p.boundary_id == alpha_boundary and p.section in (TruthSection.U3, TruthSection.U4)
    )
    verified = promote_proposal(
        book,
        workspace_id=config.workspace_id,
        proposal_id=alpha_prop.id,
        verifier_id=owner_alpha,
    )
    assert verified.error is None

    request = BookContextRequest(
        workspace_id=config.workspace_id,
        identity_id=owner_alpha,
        query="package",
    )
    app_result = book.book_context(request)
    assert app_result.error is None

    server = McpBookServer(book)
    direct_envelope = model_dump_object_dict(server.call_tool("book_context", request))

    async def _run_stdio() -> dict[str, object]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "openflywheel.cli.main",
                "serve",
                "--surface",
                "verbs",
                "--home",
                str(temp_ofw_home),
            ],
        )
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            mcp_result = await session.call_tool(
                "book_context",
                arguments=request.model_dump(mode="json"),
            )
            assert mcp_result.is_error is False
            assert mcp_result.content
            mcp_text = mcp_result.content[0].text
            assert isinstance(mcp_text, str)
            return json.loads(mcp_text)

    mcp_payload = asyncio.run(_run_stdio())
    assert mcp_payload == direct_envelope
    assert mcp_payload.get("data") is not None
    assert app_result.data is not None
    assert mcp_payload["data"]["markdown"] == app_result.data.markdown

    after_inventory = bounded_hash_inventory(arceus_root, max_files=500)
    assert before_inventory == after_inventory

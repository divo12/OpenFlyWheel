"""Shared helpers for opt-in real-environment E2E tests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from pydantic import TypeAdapter

from openflywheel.connectors.agents.settings_models import CursorHooksConfig
from openflywheel.contracts.ids import EpisodeId, EvidenceAnchorId, ProposalId

_CURSOR_HOOKS_ADAPTER: TypeAdapter[CursorHooksConfig] = TypeAdapter(CursorHooksConfig)
_FOREIGN_CURSOR_HOOKS_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "agent-transcripts"
    / "cursor-openflywheel-hooks-foreign.json"
)
_REAL_PROJECT_HOOK_SEARCH_ROOTS: tuple[Path, ...] = (
    Path("/Users/divyansh/OpenFlyWheel"),
    Path("/Users/divyansh/Arceus"),
    Path("/Users/divyansh/Downloads/ai-job-search"),
)
_PROPOSAL_SIGNAL = re.compile(r"\b(should|recommend|propose|must)\b", re.IGNORECASE)
_TRANSCRIPT_PREFER_MARKERS: tuple[str, ...] = ("OpenFlyWheel", "openflywheel", "Arceus", "arceus")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bounded_hash_inventory(root: Path, *, max_files: int = 500) -> dict[str, str]:
    """Hash up to max_files regular files under root (read-only inventory)."""
    inventory: dict[str, str] = {}
    if not root.exists():
        return inventory
    for index, path in enumerate(sorted(p for p in root.rglob("*") if p.is_file())):
        if index >= max_files:
            break
        rel = str(path.relative_to(root))
        inventory[rel] = sha256_file(path)
    return inventory


def require_e2e_real() -> None:
    if os.environ.get("OFW_RUN_E2E_REAL") != "1":
        import pytest

        pytest.skip("Set OFW_RUN_E2E_REAL=1 to run real E2E")


def real_cursor_home() -> Path | None:
    env = os.environ.get("CURSOR_HOME")
    if env:
        path = Path(env)
        return path if path.is_dir() else None
    default = Path.home() / ".cursor"
    return default if default.is_dir() else None


def real_cursor_settings_path(cursor_home: Path) -> Path | None:
    candidates: Iterable[Path] = (
        cursor_home / "hooks.json",
        cursor_home / "settings.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def is_cursor_openflywheel_hooks_compatible(path: Path) -> bool:
    """True when path matches CursorInstaller openflywheel-hooks.json schema."""
    if not path.is_file():
        return False
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    try:
        _CURSOR_HOOKS_ADAPTER.validate_python(parsed)
    except Exception:
        return False
    return True


def discover_compatible_project_hooks() -> Path | None:
    """Find a real project-local .cursor/openflywheel-hooks.json CursorInstaller can read."""
    for root in _REAL_PROJECT_HOOK_SEARCH_ROOTS:
        if not root.is_dir():
            continue
        candidate = root / ".cursor" / "openflywheel-hooks.json"
        if is_cursor_openflywheel_hooks_compatible(candidate):
            return candidate
    return None


def foreign_cursor_hooks_fixture_bytes() -> bytes:
    """Schema-valid foreign hook seed derived from typed test fixture (not claimed real)."""
    return _FOREIGN_CURSOR_HOOKS_FIXTURE.read_bytes()


def real_cursor_hooks_guard_path(cursor_home: Path) -> Path | None:
    """Real ~/.cursor file hashed read-only; may be incompatible with CursorInstaller."""
    path = cursor_home / "hooks.json"
    return path if path.is_file() else None


def discover_real_cursor_transcript(cursor_home: Path) -> Path | None:
    """Deterministically pick one parent-session Cursor transcript (not subagent)."""
    transcripts_dir = cursor_home / "projects"
    if not transcripts_dir.is_dir():
        return None
    candidates = sorted(
        p
        for p in transcripts_dir.rglob("*.jsonl")
        if "agent-transcripts" in p.parts and "subagents" not in p.parts
    )
    if not candidates:
        return None
    preferred = [p for marker in _TRANSCRIPT_PREFER_MARKERS for p in candidates if marker in str(p)]
    return preferred[0] if preferred else candidates[0]


def transcript_has_deterministic_proposal_signal(transcript_path: Path) -> bool:
    """Mirror BackgroundWorkerService assistant-line proposal detector."""
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        role = parsed.get("role")
        if role is None:
            kind = parsed.get("type")
            if kind == "assistant":
                role = "assistant"
        if role != "assistant":
            continue
        text = _extract_transcript_text(parsed)
        if text and _PROPOSAL_SIGNAL.search(text):
            return True
    return False


def _extract_transcript_text(raw: dict[str, object]) -> str:
    for key in ("text", "content", "message"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("content")
            if isinstance(nested, str):
                return nested
            if isinstance(nested, list):
                parts: list[str] = []
                for item in nested:
                    if isinstance(item, dict):
                        part = item.get("text")
                        if isinstance(part, str):
                            parts.append(part)
                return "\n".join(parts)
    return ""


def episode_ids_for_workspace(database, workspace_id) -> set[str]:
    with database.read() as conn:
        rows = conn.execute(
            "SELECT id FROM episodes WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchall()
    return {str(row["id"]) for row in rows}


def proposals_linked_to_episode(
    database,
    workspace_id,
    episode_id: EpisodeId,
) -> tuple[ProposalId, ...]:
    prefix = f"transcript:{episode_id}:"
    with database.read() as conn:
        rows = conn.execute(
            """
            SELECT id FROM proposals
            WHERE workspace_id = ? AND idempotency_key LIKE ?
            ORDER BY created_at
            """,
            (str(workspace_id), f"{prefix}%"),
        ).fetchall()
    return tuple(ProposalId(str(row["id"])) for row in rows)


def anchor_ids_for_episode(database, episode_id: EpisodeId) -> tuple[EvidenceAnchorId, ...]:
    with database.read() as conn:
        rows = conn.execute(
            "SELECT id FROM evidence_anchors WHERE episode_id = ? ORDER BY id",
            (str(episode_id),),
        ).fetchall()
    return tuple(EvidenceAnchorId(str(row["id"])) for row in rows)


def assert_episode_exists(database, episode_id: EpisodeId) -> None:
    with database.read() as conn:
        row = conn.execute("SELECT id FROM episodes WHERE id = ?", (str(episode_id),)).fetchone()
    if row is None:
        msg = f"Episode {episode_id} not found in database"
        raise AssertionError(msg)


def real_arceus_root() -> Path | None:
    raw = os.environ.get("OFW_ARCEUS_ROOT")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


_SKIP_PARTS = frozenset({".git", ".venv", "node_modules", "__pycache__"})


def _path_has_skipped_part(path: Path) -> bool:
    return any(part in _SKIP_PARTS or part.startswith(".git") for part in path.parts)


def is_locateable_repo(path: Path) -> bool:
    """True when path is a Python repo root the onboard locate scanner accepts."""
    return path.is_dir() and (path / "pyproject.toml").is_file() and (path / "src").is_dir()


def discover_locateable_repos(source_root: Path) -> tuple[Path, ...]:
    """Find repo roots (pyproject.toml + src/) under source_root, read-only."""
    found: dict[str, Path] = {}
    for pyproject in sorted(source_root.rglob("pyproject.toml")):
        if _path_has_skipped_part(pyproject):
            continue
        repo = pyproject.parent
        if not is_locateable_repo(repo):
            continue
        found[str(repo.resolve())] = repo
    return tuple(found[path] for path in sorted(found))


def _copy_repo_bounded(source_repo: Path, dest_repo: Path, *, max_files: int) -> int:
    """Copy essential repo files into dest_repo; return files copied."""
    copied = 0
    priority = (source_repo / "pyproject.toml", source_repo / "README.md")
    for src in priority:
        if not src.is_file() or copied >= max_files:
            continue
        target = dest_repo / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1

    for path in sorted(p for p in source_repo.rglob("*") if p.is_file()):
        if copied >= max_files:
            break
        if _path_has_skipped_part(path):
            continue
        rel = path.relative_to(source_repo)
        if rel.name in {"pyproject.toml", "README.md"}:
            continue
        target = dest_repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def copy_locateable_repo_subset(
    source_root: Path,
    dest_root: Path,
    *,
    min_repos: int = 2,
    max_files: int = 50,
) -> int:
    """Copy locateable Python repos as direct children of dest_root for onboard locate."""
    repos = discover_locateable_repos(source_root)
    if len(repos) < min_repos:
        return 0

    dest_root.mkdir(parents=True, exist_ok=True)
    per_repo = max(1, max_files // min_repos)
    copied = 0
    used_names: set[str] = set()
    for repo in repos:
        if len(used_names) >= min_repos or copied >= max_files:
            break
        slug = repo.name
        if slug in used_names:
            slug = f"{slug}-{sha256_file(repo / 'pyproject.toml')[:8]}"
        used_names.add(slug)
        budget = min(per_repo, max_files - copied)
        copied += _copy_repo_bounded(repo, dest_root / slug, max_files=budget)
    return copied if len(used_names) >= min_repos else 0


def copy_bounded_source_tree(source_root: Path, dest_root: Path, *, max_files: int = 50) -> int:
    """Copy locateable repo subset when possible; otherwise bounded flat tree copy."""
    located = copy_locateable_repo_subset(source_root, dest_root, max_files=max_files)
    if located > 0:
        return located

    dest_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in sorted(p for p in source_root.rglob("*") if p.is_file()):
        if copied >= max_files:
            break
        if _path_has_skipped_part(path):
            continue
        rel = path.relative_to(source_root)
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def count_table_rows(database, table: str, workspace_id) -> int:
    with database.read() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM {table} WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchone()
    return int(row["cnt"]) if row is not None else 0

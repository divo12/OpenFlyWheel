-- 001_foundation.sql — tables through wave C

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    deployment_mode TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    admin_identity_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identities (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    acl_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boundaries (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    component_paths_json TEXT NOT NULL,
    manifest_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, slug)
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    kind TEXT NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    capability_json TEXT NOT NULL,
    root_path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, slug)
);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    content_text TEXT NOT NULL,
    acl_json TEXT NOT NULL,
    event_time TEXT NOT NULL,
    ingest_time TEXT NOT NULL,
    checksum TEXT NOT NULL,
    content_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_anchors (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(id),
    locator_kind TEXT NOT NULL,
    locator_value TEXT NOT NULL,
    label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    what TEXT NOT NULL,
    how TEXT NOT NULL,
    section TEXT NOT NULL,
    proposer_identity_id TEXT NOT NULL REFERENCES identities(id),
    anchor_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    boundary_id TEXT NOT NULL REFERENCES boundaries(id),
    what TEXT NOT NULL,
    how TEXT NOT NULL,
    section TEXT NOT NULL,
    state TEXT NOT NULL,
    authority_identity_id TEXT NOT NULL REFERENCES identities(id),
    acl_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    from_claim_id TEXT NOT NULL REFERENCES claims(id),
    to_claim_id TEXT NOT NULL REFERENCES claims(id),
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_requirements (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    boundary_id TEXT NOT NULL REFERENCES boundaries(id),
    section TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    description TEXT NOT NULL,
    required_for_shape TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE REFERENCES sources(id),
    cursor_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    platform TEXT NOT NULL,
    session_ref TEXT NOT NULL,
    transcript_pointer TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pins (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    boundary_id TEXT NOT NULL REFERENCES boundaries(id),
    manifest_version INTEGER NOT NULL,
    claim_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_rejects (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    rejected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_runs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    stage TEXT NOT NULL,
    connect_json TEXT,
    locate_json TEXT,
    lock_json TEXT,
    updated_at TEXT NOT NULL
);

-- FTS5-ready virtual table placeholder for later waves
-- CREATE VIRTUAL TABLE IF NOT EXISTS claim_fts USING fts5(...);

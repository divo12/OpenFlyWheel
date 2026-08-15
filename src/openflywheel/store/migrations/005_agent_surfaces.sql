-- 005_agent_surfaces.sql — background job queue for agent write-back

CREATE TABLE IF NOT EXISTS background_jobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_background_jobs_pending
    ON background_jobs(status, lease_expires_at);

-- Dedupe agent_sessions before unique index (keep earliest rowid per key)
DELETE FROM agent_sessions
WHERE rowid NOT IN (
    SELECT MIN(rowid)
    FROM agent_sessions
    GROUP BY workspace_id, platform, session_ref
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_sessions_platform_ref
    ON agent_sessions(workspace_id, platform, session_ref);

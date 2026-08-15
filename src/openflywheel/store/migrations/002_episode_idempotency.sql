-- 002_episode_idempotency.sql — idempotency key and audit dedupe

CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_source_external_checksum
    ON episodes (source_id, external_id, checksum);

CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_rejects_source_external_reason
    ON audit_rejects (source_id, external_id, reason);

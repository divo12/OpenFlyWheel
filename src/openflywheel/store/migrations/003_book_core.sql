-- 003_book_core.sql — proposals idempotency, claim provenance, FTS5

ALTER TABLE proposals ADD COLUMN boundary_id TEXT REFERENCES boundaries(id);
ALTER TABLE proposals ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE proposals ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_idempotency
    ON proposals (workspace_id, idempotency_key)
    WHERE length(idempotency_key) > 0;

ALTER TABLE claims ADD COLUMN source_proposal_id TEXT REFERENCES proposals(id);

CREATE INDEX IF NOT EXISTS idx_claims_boundary_state
    ON claims (boundary_id, state);

CREATE VIRTUAL TABLE IF NOT EXISTS claim_fts USING fts5(
    claim_id UNINDEXED,
    boundary_id UNINDEXED,
    acl_json UNINDEXED,
    what,
    how,
    tokenize='porter'
);

-- 004_book_hardening.sql — pin snapshots, coverage uniqueness, FTS triggers, immutability

CREATE UNIQUE INDEX IF NOT EXISTS idx_coverage_boundary_slot
    ON coverage_requirements (boundary_id, slot_key);

CREATE TABLE IF NOT EXISTS pin_claim_snapshots (
    pin_id TEXT NOT NULL REFERENCES pins(id),
    claim_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    boundary_id TEXT NOT NULL,
    what TEXT NOT NULL,
    how TEXT NOT NULL,
    section TEXT NOT NULL,
    state TEXT NOT NULL,
    authority_identity_id TEXT NOT NULL,
    acl_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_proposal_id TEXT,
    anchor_ids_json TEXT NOT NULL,
    PRIMARY KEY (pin_id, claim_id)
);

CREATE TABLE IF NOT EXISTS pin_anchor_snapshots (
    pin_id TEXT NOT NULL REFERENCES pins(id),
    anchor_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    locator_kind TEXT NOT NULL,
    locator_value TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (pin_id, claim_id, anchor_id)
);

CREATE TABLE IF NOT EXISTS pin_edge_snapshots (
    pin_id TEXT NOT NULL REFERENCES pins(id),
    edge_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_claim_id TEXT NOT NULL,
    to_claim_id TEXT NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (pin_id, edge_id)
);

CREATE TRIGGER IF NOT EXISTS pins_no_update BEFORE UPDATE ON pins BEGIN SELECT RAISE(ABORT, 'pins are immutable'); END;

CREATE TRIGGER IF NOT EXISTS pins_no_delete BEFORE DELETE ON pins BEGIN SELECT RAISE(ABORT, 'pins are immutable'); END;

CREATE TRIGGER IF NOT EXISTS pin_claim_snapshots_no_update BEFORE UPDATE ON pin_claim_snapshots BEGIN SELECT RAISE(ABORT, 'pin_claim_snapshots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS pin_claim_snapshots_no_delete BEFORE DELETE ON pin_claim_snapshots BEGIN SELECT RAISE(ABORT, 'pin_claim_snapshots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS pin_anchor_snapshots_no_update BEFORE UPDATE ON pin_anchor_snapshots BEGIN SELECT RAISE(ABORT, 'pin_anchor_snapshots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS pin_anchor_snapshots_no_delete BEFORE DELETE ON pin_anchor_snapshots BEGIN SELECT RAISE(ABORT, 'pin_anchor_snapshots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS pin_edge_snapshots_no_update BEFORE UPDATE ON pin_edge_snapshots BEGIN SELECT RAISE(ABORT, 'pin_edge_snapshots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS pin_edge_snapshots_no_delete BEFORE DELETE ON pin_edge_snapshots BEGIN SELECT RAISE(ABORT, 'pin_edge_snapshots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS claim_fts_ai AFTER INSERT ON claims WHEN NEW.state = 'active' AND NEW.valid_to IS NULL BEGIN INSERT INTO claim_fts (claim_id, boundary_id, acl_json, what, how) VALUES (NEW.id, NEW.boundary_id, NEW.acl_json, NEW.what, NEW.how); END;

CREATE TRIGGER IF NOT EXISTS claim_fts_ad AFTER DELETE ON claims BEGIN DELETE FROM claim_fts WHERE claim_id = OLD.id; END;

CREATE TRIGGER IF NOT EXISTS claim_fts_au AFTER UPDATE ON claims BEGIN DELETE FROM claim_fts WHERE claim_id = OLD.id; INSERT INTO claim_fts (claim_id, boundary_id, acl_json, what, how) SELECT NEW.id, NEW.boundary_id, NEW.acl_json, NEW.what, NEW.how WHERE NEW.state = 'active' AND NEW.valid_to IS NULL; END;

CREATE TABLE IF NOT EXISTS proposals_strict (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    boundary_id TEXT NOT NULL REFERENCES boundaries(id),
    what TEXT NOT NULL,
    how TEXT NOT NULL,
    section TEXT NOT NULL,
    proposer_identity_id TEXT NOT NULL REFERENCES identities(id),
    anchor_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    idempotency_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

INSERT INTO proposals_strict (
    id, workspace_id, boundary_id, what, how, section,
    proposer_identity_id, anchor_ids_json, status, idempotency_key, created_at
)
SELECT
    id, workspace_id, boundary_id, what, how, section,
    proposer_identity_id, anchor_ids_json, status, idempotency_key, created_at
FROM proposals
WHERE boundary_id IS NOT NULL AND length(boundary_id) > 0;

DROP TABLE proposals;

ALTER TABLE proposals_strict RENAME TO proposals;

CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_idempotency
    ON proposals (workspace_id, idempotency_key)
    WHERE length(idempotency_key) > 0;

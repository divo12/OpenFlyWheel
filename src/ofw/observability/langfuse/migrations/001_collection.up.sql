BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS ofw_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE langfuse_observations (
    connection_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    trace_id TEXT,
    start_time TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (connection_id, observation_id, content_digest)
);

CREATE INDEX langfuse_observations_trace_time
    ON langfuse_observations (connection_id, trace_id, start_time, observation_id);

CREATE TABLE langfuse_scores (
    connection_id TEXT NOT NULL,
    score_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (connection_id, score_id, content_digest)
);

CREATE TABLE collection_observations (
    sync_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    PRIMARY KEY (sync_id, observation_id),
    FOREIGN KEY (connection_id, observation_id, content_digest)
        REFERENCES langfuse_observations (connection_id, observation_id, content_digest)
        ON DELETE CASCADE
);

CREATE TABLE collection_scores (
    sync_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    score_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    PRIMARY KEY (sync_id, score_id),
    FOREIGN KEY (connection_id, score_id, content_digest)
        REFERENCES langfuse_scores (connection_id, score_id, content_digest)
        ON DELETE CASCADE
);

CREATE TABLE collection_checkpoints (
    sync_id TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('observations', 'scores')),
    cursor TEXT,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    page_count INTEGER NOT NULL CHECK (page_count > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sync_id, stream)
);

INSERT INTO ofw_schema_migrations (version) VALUES (1);
PRAGMA user_version = 1;

COMMIT;

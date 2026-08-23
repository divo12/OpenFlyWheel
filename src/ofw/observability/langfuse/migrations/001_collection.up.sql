BEGIN IMMEDIATE;

CREATE TABLE observation_content (
    content_digest TEXT PRIMARY KEY,
    content_text TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
    truncated INTEGER NOT NULL CHECK (truncated IN (0, 1))
);

CREATE VIRTUAL TABLE observation_content_fts USING fts5(
    content_digest UNINDEXED,
    content_text,
    tokenize = 'unicode61'
);

CREATE TABLE langfuse_observations (
    connection_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    trace_id TEXT,
    start_time TEXT NOT NULL,
    input_content_digest TEXT,
    output_content_digest TEXT,
    content_digest TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (connection_id, observation_id, content_digest),
    FOREIGN KEY (input_content_digest)
        REFERENCES observation_content (content_digest),
    FOREIGN KEY (output_content_digest)
        REFERENCES observation_content (content_digest)
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
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sync_id, stream)
);

PRAGMA user_version = 1;

COMMIT;

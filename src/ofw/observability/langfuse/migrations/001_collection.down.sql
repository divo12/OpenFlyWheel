BEGIN IMMEDIATE;

DROP TABLE IF EXISTS collection_scores;
DROP TABLE IF EXISTS collection_observations;
DROP TABLE IF EXISTS collection_checkpoints;
DROP TABLE IF EXISTS langfuse_scores;
DROP TABLE IF EXISTS langfuse_observations;
DELETE FROM ofw_schema_migrations WHERE version = 1;
PRAGMA user_version = 0;

COMMIT;

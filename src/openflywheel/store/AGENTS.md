# store/

SQLite persistence layer.

- `db.py` — ConnectionFactory, Database (`read()` never commits; `write()` is all-or-nothing)
- `migrate.py` — atomic numbered SQL migrations (no executescript)
- `migrations/` — `001_foundation.sql`, `002_episode_idempotency.sql`
- `rows.py` — typed SQL row dataclasses
- `sqlite_access.py` — typed sqlite3.Row cell accessors (used by repos)
- `internal_records.py` — store-layer DTOs (CheckpointRecord, AuditRejectRecord)
- `migration_hook.py` — test-only migration abort hook (NoOp default)
- `repos/` — protocol + SQLite implementations per aggregate
- `uow.py` — UnitOfWork coordinating episode + anchors + checkpoint
- `checkpoint_hook.py` — test-only checkpoint abort hook (NoOp default)
- `exceptions.py` — DomainError hierarchy mapped to OperationResult

Repository methods accept and return contract models or `internal_records` DTOs only.

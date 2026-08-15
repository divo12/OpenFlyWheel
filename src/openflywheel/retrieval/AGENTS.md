# retrieval/ — Agent Guidelines

Deterministic ACL → FTS → edge expansion → Markdown packet.

## Modules

- `acl.py` — fail-closed visibility checks
- `fts.py` — SQLite FTS5 over active claim what/how
- `expand.py` — direct neighbor edges only
- `packet.py` — compact Markdown + `ContextPacket`
- `service.py` — `book_context`, `book_get`

## Rules

1. Unknown identity → refuse before ranking.
2. Pin reads use frozen claim-id set (no query narrowing).
3. Packet includes gaps; never gold Why.
4. Embeddings deferred.

## CLI

- `ofw book context "…"`
- `ofw book get <claim_id>`

## Tests

- `tests/integration/test_retrieval.py`

# application/ — Agent Guidelines

Orchestration layer; CLI and MCP stdio call these services.

## Modules

- `workspace_service.py` — init/open workspace
- `ingest_app.py` — fixture GitHub ingest
- `book_app.py` — frozen book verbs (D–H)
- `agent_worker.py` — background transcript extract with lease/retry
- `agent_authorization.py` — episode/correction identity/source/boundary gates
- `identity_gate.py` — dashboard and notes identity resolution
- `recursion.py` — ContextVar guard for nested job scheduling

## BookApplication verbs

| Verb | Status |
|------|--------|
| `extract` | SaO proposals |
| `claim_propose` | manual proposal |
| `book_verify` | human verification |
| `book_pin` | snapshot |
| `coverage_gaps` | org + boundary reports |
| `book_context` | retrieval packet |
| `book_get` | single claim detail |
| `episode_record` | agent transcript write-back (wave G) |
| `correction_record` | high-authority correction episode (wave G) |

MCP exposes the same verbs via `ofw serve --surface verbs`.

## Tests

Integration tests use `tests/book_helpers.py` and `tests/agent_helpers.py`.

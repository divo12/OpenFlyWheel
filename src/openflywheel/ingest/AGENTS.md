# ingest/ — Agent Guidelines

Admission, episodes, and SaO extraction.

## Modules

- `admission.py` — fail-closed admission policy
- `episode_service.py` — GitHub fixture ingest orchestration
- `scope.py` — component path and exclusion merge helpers
- `sao/` — deterministic System-as-Oracle extractors (U3/U4 only)

## Rules

1. SaO emits **proposals only**; never insert `claims` rows.
2. Idempotency key = hash(extractor, boundary, what, anchor, content fingerprint).
3. Every proposal requires at least one `EvidenceAnchor` with `file:line` locator.
4. Extraction requires locked boundaries (`EXTRACT_BEFORE_LOCK` otherwise).

## CLI

- `ofw ingest run` — admit episodes from fixture GitHub root
- `ofw ingest extract` — run SaO over admitted episodes

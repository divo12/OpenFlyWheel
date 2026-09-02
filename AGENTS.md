# OpenFlyWheel Engineering Rules

These instructions apply to the entire repository.

## Product boundaries

- Langfuse is the source of truth for traces and spans.
- Trace-query code is read-only. Do not add write, update, or delete calls to observability providers.
- Do not persist Langfuse trace payloads in SQLite or another local database.
- Do not replace persistent storage with exhaustive in-memory hydration. Use bounded pages and opaque continuation cursors.
- Keep trace retrieval separate from judging, classification, summarization, semantic indexing, and vector search.

## Required workflow

1. Restate the requested outcome and explicit non-goals.
2. Inspect the relevant definitions and every caller before editing.
3. Draw the smallest data flow and trust boundaries. Prefer `controller -> service -> gateway`.
4. Define immutable input, output, error, ordering, pagination, and size contracts.
5. Write a failing test for each observable requirement and boundary condition.
6. Implement the smallest change that makes the tests pass.
7. Refactor only measured complexity or duplication in the changed path.
8. Run formatting, linting, strict typing, tests, coverage, and plugin validation when applicable.
9. Review the final diff against the base branch for scope creep, generated files, secrets, and unrelated changes.
10. Report exact verification evidence. Never describe a local command as CI evidence.

## Python design

- Support Python 3.11 or newer and keep strict mypy clean.
- Use frozen dataclasses for domain values and strict Pydantic models at external boundaries.
- Use named objects instead of generic dictionaries in domain, service, and gateway code.
- Do not introduce `Any`, `getattr`, `setattr`, reflective dispatch, or unchecked casts.
- A dictionary is allowed only at an unavoidable JSON/protocol boundary owned by an external SDK. Convert it immediately into a typed object.
- Prefer tuples and immutable values in public contracts. Keep mutation local and private when required for a bounded reducer.
- Validate identifiers, timestamps, limits, cursors, response bytes, and array sizes at the boundary.
- Use typed error codes. Do not leak credentials, raw provider errors, or secrets.

## Backend boundaries

- Contracts define data; services implement use cases; gateways perform provider I/O; MCP tools adapt protocol calls.
- Services depend on gateway protocols, not concrete HTTP clients.
- Gateways select only required Langfuse field groups and expose only GET operations.
- Preserve Langfuse cursors as opaque values. Reuse a cursor only with the same query that produced it.
- Declare deterministic ordering, including a stable secondary key.
- Every page needs a count bound. Every response body needs a byte bound.
- If complete coverage needs multiple pages, return `next_cursor`; never silently call a partial sample complete.

## Complexity and maintainability

- Measure changed Python with `radon cc -s -a`.
- Keep new or changed functions at cyclomatic complexity 5 or lower.
- Use guard clauses and small named helpers. Do not hide branches in dense expressions.
- Do not add factories, registries, interfaces, configuration, or dependencies for hypothetical future needs.
- Comments explain constraints and reasons, not obvious mechanics.

## Tests

- Follow red, green, refactor.
- Cover strict schema rejection, empty and ambiguous input, ordering ties, pagination, late-page data, byte limits, and provider failures.
- Integration tests must exercise the service through the real gateway against a current provider response contract.
- Keep an opt-in live Langfuse test for production response compatibility.
- Assert read-only behavior by checking that ambiguous requests make no provider call and executed calls use GET only.
- Maintain at least 90% project coverage and 90% coverage for new modules.

## Verification commands

```bash
uv sync --extra dev --extra plugin
uv run ruff check src tests plugins/openflywheel/scripts/mcp_server.py
uv run mypy src tests plugins/openflywheel/scripts/mcp_server.py
uv run pytest --cov=ofw --cov-report=term-missing --cov-fail-under=90 -q
uvx --from radon radon cc -s -a src tests plugins/openflywheel/scripts/mcp_server.py
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/openflywheel/skills/trace-query-planner
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/openflywheel/skills/outcome-recorder
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/openflywheel/skills/failure-miner
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/openflywheel/skills/failure-pattern-miner
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/openflywheel
```

## Repository hygiene

- Preserve unrelated user changes and untracked local artifacts.
- Do not commit `.env`, credentials, trace dumps, generated reports, caches, or temporary benchmark data.
- Do not change collection, storage, judging, or benchmark behavior in a trace-reader PR unless the task explicitly requires it.
- Use one architectural change per PR and describe breaking public API changes explicitly.

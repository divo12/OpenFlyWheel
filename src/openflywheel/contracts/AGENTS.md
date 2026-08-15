# contracts/

I/O-free typed records. No imports from store, connectors, or CLI.

- `ids.py` — NewType branded string IDs
- `enums.py` — shared enumerations
- `operation_result.py` — OperationResult[T], OperationError
- Domain modules: workspace, identity, boundary, source, episode, evidence, proposal, claim, edges, coverage, pin, onboarding, acl, agent_events, retrieval

Contracts must round-trip through Pydantic and use `model_config = ConfigDict(frozen=True)` where immutability is required (episodes, pins).

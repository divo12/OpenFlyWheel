# OpenFlyWheel offline end-to-end release proof

The permanent release gate is:

```bash
uv run pytest -q tests/test_e2e_release.py
```

The fixture uses no external provider account or production credentials. A loopback HTTP server exposes the read-only Langfuse health, observation, and score endpoints. One stamped harness revision then completes this exact lineage:

```text
Langfuse-compatible traces
  → revision-attributed collection
  → Mine admission and immutable snapshots
  → evidence-bound diagnosis and clusters
  → leakage-safe training/eval/selection/admission exports
  → controlled tool-file candidate
  → paired baseline/candidate gates and one-shot admission
  → durable scheduler PROMOTE job
  → isolated Git commit, review PR reference, and reverse patch
```

The planted fixture contains one verified-good trace and four failures assigned to frontier, regression, selection, and admission partitions. The candidate fixes frontier and sealed holdouts while preserving the regression case. The test proves one winner, one PR, no deploy, no Langfuse write, and a non-empty rollback artifact.

This is the local-v0 release boundary. Distributed scheduling, cloud workspaces, provider-backed candidate generation, and production deployment remain explicit adapters rather than hidden behavior in the offline proof.

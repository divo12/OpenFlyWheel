---
name: failure-pattern-miner
description: Group explicit compact OpenFlywheel failure diagnoses by issue type and exact normalized root cause. Use after failure-miner records diagnoses for a bounded comparison scope; do not use to diagnose traces, infer outcomes, create semantic clusters, or edit a harness.
---

# Failure Pattern Miner

Mine repeated patterns from diagnoses already recorded by `$failure-miner`. Use only
artifact IDs returned by `record_failure` from the same prepared workspace and the same
comparison scope, such as one baseline or candidate run. Pass one to fifty unique artifact
IDs to `mine_failure_patterns`; never scan the workspace or substitute trace IDs and paths.

The tool groups supported diagnoses by failure type plus exact normalized root cause. Its
normalizer masks volatile absolute paths, long opaque identifiers, and numbers before
fingerprinting. Results are deterministic exact matches, not semantic clusters: similar
wording may remain separate, and matching wording does not prove one repair will fix every
occurrence. Inconclusive diagnoses remain separate and must not be forced into a pattern.

Read patterns in their declared order: occurrence count descending, distinct task count
descending, latest occurrence descending, then fingerprint ascending. Preserve the returned
fingerprints, normalized causes, task IDs, trace IDs, artifact IDs, and time bounds. A
repeated pattern may prioritize later hypothesis work, but reread its compact diagnosis
artifacts before proposing a harness change.

Do not query Langfuse, call `record_outcome` or `record_failure`, modify diagnosis artifacts,
merge results from unrelated experiment scopes, generate embeddings, infer a broader cause,
or edit the harness while following this skill.

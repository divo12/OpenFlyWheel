# Fit experience index

Each completed `FitCampaign` writes one content-bound `experience.json` beside its result manifest. The index is the provider-neutral observation surface for the next candidate proposer. It does not choose edits or summarize away source evidence.

## Developer-visible evidence

For every attempted candidate, the index preserves:

- the frozen candidate manifest and prediction attribution;
- final candidate status and gate reason;
- the candidate diff artifact reference;
- content-digested references to the raw champion and candidate developer benchmark results;
- each developer case’s cluster family, source trace, trace family, snapshot reference, and partition; and
- every paired champion/candidate run result, verifier verdict, score, textual feedback, metric, evidence reference, and case delta.

Textual verifier feedback is stored unchanged. The compact index is navigational: the raw developer snapshot, benchmark results, and candidate diff remain available for drill-down.

## Holdout boundary

Selection and admission cases are evaluator-only. The proposer-visible index records only whether each stage ran, its completion status, and whether its frozen threshold passed. It never stores holdout case IDs, trace IDs, family IDs, snapshot references, prompts, outputs, verifier feedback, or raw benchmark paths.

This asymmetry is intentional. Developer evidence teaches the next proposer; holdouts decide whether a frozen candidate survives. Returning holdout diagnostics would turn repeated selection into training and invalidate the gate.

## Integrity

`FitResult.experience_digest` binds the exact index bytes. `read_fit_experience(result)` validates campaign, export bundle, input digest, harness revision, candidate ordering, statuses, gate reasons, prediction attribution, developer-result linkage, and every referenced developer artifact digest. Cached `FitCampaign.run()` performs the same validation before returning a prior result.

Tampering with the index, candidate diff, or raw developer benchmark result therefore fails with `FitErrorCode.RESULT_INVALID`. Existing candidate/revision validation remains authoritative for the harness, export snapshots, and candidate manifest.

## Why this is the minimum useful shape

A scalar leaderboard cannot tell an optimizer which tool, prompt, skill, subagent, or middleware behavior caused a failure. Copying all artifacts into a second store adds drift without adding evidence. The index instead supplies AHE-style component and decision observability while retaining Meta-Harness-style drill-down to raw developer traces. A filesystem manifest and exact identifiers are sufficient for local v0; no vector database, summarizer, or provider-specific proposer belongs in this PR.

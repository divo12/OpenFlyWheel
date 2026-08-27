---
name: trace-query-planner
description: Plan minimal read-only structural queries for a known ITSMBench Langfuse trace. Use for selecting spans by IDs, tools, types, UTC ranges, or error flags; do not use for judging, summarizing, or semantic search.
---

# TraceQueryPlanner

Accept a `trace_id` and optional deterministic filters. Choose the smallest tool sequence that returns the requested evidence.

- For an exact `observation_id`, call `query_spans` directly, then `get_span_context` only if raw span content or neighboring spans are needed.
- For `tool_name`, `span_type`, UTC `start_time` plus `end_time`, or `error`, call `query_spans` directly.
- When no usable filter is present, call `get_trace_schema` once to skim IDs and labels. If the request is still ambiguous, ask for exactly the single field named in `missing_fields`.
- Stop when the returned spans answer the structural request. Do not fetch a full trace blob or expand every match.

Only use `get_trace_schema`, `query_spans`, and `get_span_context`. They are read-only. Never call write or update APIs, judge correctness, infer failure types, summarize beyond returned span content, or add semantic/vector search.

Preserve the tool observation as returned: `status`, `summary`, `next_actions`, `artifacts`, `trace_id`, `filters_applied`, `spans_found`, `missing_fields`, and `truncated`.

---
name: trace-query-planner
description: Plan minimal read-only structural queries for an ITSMBench Langfuse session or trace. Use for selecting traces and spans by IDs, tools, types, UTC ranges, or error flags; do not use for judging, summarizing, or semantic search.
---

# TraceQueryPlanner

Accept either a session query or a `trace_id` with optional deterministic filters. Choose the smallest tool sequence that returns the requested evidence.

- When given a `session_id`, call `list_traces` with an explicit UTC time range plus optional environment and release. It returns exported logical-root traces; a missing or duplicate logical root is an instrumentation issue. Continue with `next_cursor` only until the requested trace is found.
- For an exact `observation_id`, call `query_spans` directly, then `get_span_context` only if raw span content or neighboring spans are needed.
- For `tool_name`, `span_type`, UTC `start_time` plus `end_time`, `error`, or a typed input/output/metadata text filter, call `query_spans` directly.
- Metadata text filters require a metadata key. Use exact matching for known values and token-phrase matching for indexed search.
- When no usable filter is present, call `get_trace_schema` to skim IDs, labels, and span types. Continue with `next_cursor` until it is absent before claiming type coverage; each call remains bounded.
- Continue `query_spans` or `get_span_context` with `next_cursor` only when the requested evidence was not present on the current page.
- Reuse a cursor only with the same trace ID and filters that produced it. Treat each page's declared `ordering` as authoritative.
- If the request is ambiguous, ask for exactly the single field named in `missing_fields`.
- Stop when the returned spans answer the structural request. Do not fetch a full trace blob or expand every match.

Only use `list_traces`, `get_trace_schema`, `query_spans`, and `get_span_context`. They are read-only. Never call write or update APIs, judge correctness, infer failure types, summarize beyond returned span content, or add semantic/vector search.

Preserve every tool observation as returned. All tools return `status`, `summary`, `next_actions`, `artifacts`, `ordering`, `filters_applied`, `next_cursor`, and `truncated`. `list_traces` additionally returns `traces_found`; the trace-span tools additionally return `trace_id`, `spans_found`, `span_types`, and `missing_fields`.

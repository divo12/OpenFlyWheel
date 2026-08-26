---
name: integrate-ofw
description: Integrates any agent harness already connected to Langfuse with OpenFlyWheel by declaring versioned harness components, attributing real runs to an immutable revision, and verifying full trace collection. Use for onboarding an agent system to OFW. Do not use for failure mining, diagnosis, harness optimization, or replacing Langfuse.
---

# Integrate an agent harness with OpenFlyWheel

Add OFW to the real agent execution path without changing harness behavior.
Read [references/python-api.md](references/python-api.md) before writing OFW
code; it contains the exact supported API.

## Principles

1. **Instrument the real path.** Find the command, service, or worker that
   actually starts the agent harness. Do not create a parallel demonstration agent.
2. **Discover components for the user.** Locate the active system prompt, tool
   implementations, skills, subagent definitions, and middleware. The user
   should not have to translate their repository into OFW schemas manually.
3. **Do not change behavior.** This workflow may add an OFW declaration and
   revision attribution. It must not rewrite prompts, tools, skills, or harness
   control flow.
4. **Keep secrets in the environment.** Never place Langfuse keys in code,
   manifests, plugin files, logs, or chat. OFW records only environment-variable
   names.
5. **Preserve Langfuse.** Connect the application's existing Langfuse project;
   do not replace its SDK, exporter, or trace hierarchy.
6. **Verify with a real run.** Integration is complete only when one real agent
   trajectory is attributed to the generated revision and collected with full
   input/output content into local SQLite.

## Workflow

1. Inspect the repository and its dependency manager. Confirm that
   `openflywheel` is available from the user's chosen package source. If it is
   not resolvable, stop and ask for the intended install source; do not invent a
   Git URL or published version.
2. Identify the git root and the active harness assets:
   - one or more prompt files;
   - named tool implementation files;
   - skill files;
   - subagent definitions, if present;
   - middleware or lifecycle files, if present.
3. Add one small `ofw_harness.py` declaration at the git root. Follow the
   reference exactly. Register every discovered active asset once.
4. Default only the primary prompt to `ofw.editable`. Keep tools, skills,
   subagents, and middleware frozen unless the user explicitly authorizes OFW
   to optimize them later.
5. Connect `LangfuseProject.from_env` using the deployment's real environment
   name and existing Langfuse environment variables. Never read or copy their
   values.
6. Process the harness to produce an immutable revision and manifest.
7. Add revision attribution around the real agent run using
   `propagate_attributes` and metadata key `ofw.harness.revision`. Do not add a
   second root trace.
8. Run one real agent request, flush the existing Langfuse client as required
   by its runtime, then collect a UTC window containing that run with
   `ofw.collect`.
9. Verify that the collection belongs to the revision, contains the expected
   trace and ordered observations, has no completeness gaps, and that captured
   input/output content can be read from the SQLite snapshot.

## Stop conditions

- Stop before editing when the active agent entry point or prompt cannot be
  identified confidently.
- Stop before a live run when credentials are unavailable; report the exact
  environment-variable names needed without exposing values.
- Do not report success from a generated manifest alone.
- Do not weaken full-trace capture, redact content, or silently accept missing
  revision attribution.

## Output

Report:

- the OFW declaration and execution path changed;
- the discovered component-to-file mapping and which files are editable;
- the immutable revision ID and manifest path;
- the real agent command/request used for verification;
- the collected trace ID, observation count, capability/gap status, and SQLite
  path;
- any missing component, credential, attribution, or content evidence.

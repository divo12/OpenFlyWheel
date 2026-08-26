# Supported Python integration

Use only this API surface. Adapt paths and names to the inspected agent-harness
repository; do not invent components that are not active.

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ofw import Harness, LangfuseProject, Tool, TraceWindow, ofw, propagate_attributes

ROOT = Path(__file__).resolve().parent

project = LangfuseProject.from_env(environment="production")
harness = Harness("agent-harness", root=ROOT)
harness.connect_prompt(ofw.editable(Path("path/to/system-prompt.md")))
harness.connect_tools(
    Tool(name="search", source=Path("path/to/search-tool.py")),
)
harness.connect_skills(Path("path/to/skill/SKILL.md"))
harness.connect_middleware(Path("path/to/middleware.py"))
harness.connect_observability(project)
revision = harness.process()
```

Register only component kinds that exist. For named subagents use
`Subagent(name=..., source=...)` with `connect_subagents`. Tool and subagent
names must be lowercase identifiers accepted by OFW. Every registered path is
relative to the git root and may belong to only one component.

Run the real agent entry point inside revision attribution:

```python
with propagate_attributes(
    metadata={"ofw.harness.revision": str(revision.id)},
):
    run_real_agent_request()
```

The surrounding application remains responsible for its existing Langfuse
client lifecycle and flush behavior.

Collect the verified run after Langfuse ingestion:

```python
end = datetime.now(UTC)
collection = ofw.collect(
    revision,
    window=TraceWindow(end - timedelta(minutes=30), end),
)
```

The default snapshot path is `<git-root>/.ofw/collection.sqlite`. A ready
integration has an exactly attributed trace, ordered observations, captured
input/output content, and no trace gaps. Use OFW's public
`search_observation_content`, `read_trace_observations`, and
`read_observation_content` helpers to prove content is queryable.

Required environment variables normally remain:

```text
LANGFUSE_PUBLIC_KEY
LANGFUSE_SECRET_KEY
LANGFUSE_BASE_URL
```

`LangfuseProject.from_env` accepts alternate environment-variable names when
the deployment already uses them. Pass the names, never their secret values.

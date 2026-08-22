# Hermes as an OFW failure-diagnosis agent

Hermes is an optional diagnosis proposer. It is not a failure oracle, eval writer, cluster reviewer, or promotion authority.

## Security boundary

For each verified failure, OFW:

1. reads only the immutable trace snapshot and files registered in the current `HarnessRevision`;
2. serializes that evidence into one size-bounded prompt and sends it to a pinned Hermes-Python bridge over stdin, never a process argument;
3. gives the bridge a disposable `HERMES_HOME`, disables rules, plugins, MCP, skills, and memory, pins the built-in `compressor` context engine, and selects the `context_engine` toolset, which exposes no model tools in the audited Hermes 0.20.0 runtime;
4. validates stdout as one typed `TraceDiagnosis`;
5. converts timeout, oversized evidence, process failure, malformed JSON, wrong trace identity, invalid anchors, or attribution to an unconnected component into an abstention; and
6. destroys the sandbox.

The Hermes process has no model-visible path to the source harness. This is stricter than a copied workspace: Hermes file tools accept absolute paths, so a disposable current directory alone is not an isolation boundary. OFW also does not use Hermes CLI `-z`, because that would publish the full evidence packet in the child process argument list. The bridge verifies the installed Hermes version before making a model call. Its proposed clusters still require a content-bound `ClusterReview` before entering an eval or holdout.

## Azure configuration

OFW does not read or persist Chorus credentials. Start OFW from a process where the approved secret manager or operator has loaded the Chorus Azure variables, then map their names to Hermes’s Azure Foundry provider contract without printing their values:

```bash
export AZURE_FOUNDRY_API_KEY="$AZURE_OPENAI_API_KEY"
export AZURE_FOUNDRY_BASE_URL="$AZURE_OPENAI_BASE_URL"
```

Pass the deployment as the model in the typed adapter:

```python
from datetime import timedelta
from pathlib import Path

from ofw import (
    HermesAgentVersion,
    HermesDiagnoser,
    ModelFingerprint,
    ProcessLimits,
    hermes_python_command,
)

diagnoser = HermesDiagnoser(
    command=hermes_python_command(
        Path.home() / ".hermes/hermes-agent/venv/bin/python"
    ),
    model=ModelFingerprint(
        provider="azure-foundry",
        model="<AZURE_OPENAI_DEPLOYMENT>",
        reasoning="high",
    ),
    agent_version=HermesAgentVersion.V0_20_0,
    limits=ProcessLimits(timedelta(minutes=5)),
    maximum_prompt_bytes=128_000,
)
```

The provider, deployment, reasoning level, bridge command, timeout, prompt budget, prompt protocol, and Hermes version are included in the diagnoser fingerprint. Secret values and evidence content are not process arguments.

## Why this shape

Judgment Labs’s Agent Judge pattern is useful because it treats evaluation as targeted investigation: search relevant trajectory evidence, inspect harness context, verify claims, and abstain when evidence is incomplete. Hermes supplies the bounded reasoning pass. OFW supplies the immutable evidence packet, schema, lineage, review gate, eval ledger, and promotion controls.

This keeps both systems at their narrow waist. OFW does not import Hermes internals, and Hermes receives no OFW holdout or production-write authority.

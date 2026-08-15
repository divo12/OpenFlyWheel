# connectors/

Source adapters. Each connector produces typed `ConnectorEnvelope` records.

- `protocols.py` — ConnectorProtocol, capability reports
- `envelope.py` — normalized source item envelope
- `github/` — GitHubClient Protocol, fixture adapter, live stub
- `agents/` — Claude, Cursor connect stubs (capability only in A–C)
- `notes/` — expert notes stub

Live GitHub tests use `@pytest.mark.live_github`. Production code uses fixture adapter by default.

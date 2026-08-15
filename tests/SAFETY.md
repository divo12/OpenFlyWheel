# Live and Real-System Test Safety

## Markers

| Marker | Purpose | Default CI |
|--------|---------|------------|
| `e2e_real` | End-to-end against real external systems | Excluded |
| `live_github` | GitHub API with credentials | Excluded |

## live_github requirements

1. Set `OFW_GITHUB_TOKEN` to a token with minimal read scope.
2. Run explicitly: `pytest -m live_github`
3. Never commit tokens or persist credentials in workspace SQLite files.
4. Use a dedicated test org; do not point at production company repos without approval.

## e2e_real requirements

1. Set `OFW_RUN_E2E_REAL=1` — tests skip cleanly without it.
2. All writes go under pytest `tmp_path`; real Cursor/Claude config is **copied** into temp homes.
3. Real config files on disk are hashed before/after; tests fail if bytes change.
4. Transcript tests copy real `*.jsonl` into temp trusted roots; source transcript bytes must not change.
5. Optional `OFW_ARCEUS_ROOT` — bounded copy (50 files) for ingest; bounded SHA-256 inventory (max 500 files) on the real root before/after must be unchanged.
6. Optional `CURSOR_HOME` — defaults to `~/.cursor` when present.
7. Do **not** run in default CI. Operator approval required.

## Fail-closed defaults

- Missing ACL metadata → refuse operation
- Admission rejects secrets and excluded paths without persisting body content
- Episode/note/correction bundles commit atomically (single SQLite transaction); partial episode/anchor/proposal/session/job rows are never left on failure
- Connector checkpoint unchanged when episode transaction fails
- Dashboard binds loopback only (`127.0.0.1`, `localhost`, `::1`); pin snapshots filter claim IDs by identity visibility
- MCP unknown tools return protocol-level `is_error=True` (not in frozen verb list)
- Installer foreign skill/rule collisions fail; malformed hooks JSON preserved byte-for-byte on failure

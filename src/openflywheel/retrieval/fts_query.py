"""FTS5 query tokenization and escaping."""


def escape_fts_query(raw: str) -> str:
    """Quote each whitespace token so FTS operators cannot change semantics."""
    tokens: list[str] = []
    for part in raw.split():
        stripped = part.strip()
        if not stripped:
            continue
        escaped = stripped.replace('"', '""')
        tokens.append(f'"{escaped}"')
    return " ".join(tokens)

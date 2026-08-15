"""Ingest path scoping and exclusion merge helpers."""

from __future__ import annotations


def merge_exclusions(
    locked_exclusions: tuple[str, ...],
    cli_exclusions: tuple[str, ...],
) -> tuple[str, ...]:
    merged = set(locked_exclusions)
    merged.update(cli_exclusions)
    return tuple(sorted(merged))


def is_within_component_paths(external_id: str, component_paths: frozenset[str]) -> bool:
    for prefix in component_paths:
        if external_id == prefix or external_id.startswith(f"{prefix}/"):
            return True
    return False

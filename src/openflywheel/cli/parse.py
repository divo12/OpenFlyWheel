"""CLI parsing helpers."""

from __future__ import annotations

import typer

from openflywheel.contracts.boundary import SourceAuthorityRule
from openflywheel.contracts.enums import SystemShape


def parse_boundary_exclusion(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise typer.BadParameter("boundary-exclusion must use slug:prefix format")
    slug, prefix = value.split(":", maxsplit=1)
    if not slug or not prefix:
        raise typer.BadParameter("boundary-exclusion must use slug:prefix format")
    return slug, prefix


def parse_authority(value: str) -> SourceAuthorityRule:
    if ":" not in value:
        raise typer.BadParameter("authority must use slug:rank format")
    slug, rank_text = value.split(":", maxsplit=1)
    try:
        rank = int(rank_text)
    except ValueError as exc:
        raise typer.BadParameter("authority rank must be an integer") from exc
    return SourceAuthorityRule(source_slug=slug, authority_rank=rank)


def parse_system_shape(value: str) -> SystemShape:
    try:
        return SystemShape(value)
    except ValueError as exc:
        raise typer.BadParameter(f"Unknown system shape: {value}") from exc

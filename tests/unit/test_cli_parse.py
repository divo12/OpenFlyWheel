"""CLI parsing helper tests."""

from __future__ import annotations

import pytest
from typer import BadParameter

from openflywheel.cli.parse import parse_authority, parse_boundary_exclusion, parse_system_shape
from openflywheel.contracts.enums import SystemShape


def test_parse_boundary_exclusion_valid() -> None:
    slug, prefix = parse_boundary_exclusion("repo-beta:secrets/")
    assert slug == "repo-beta"
    assert prefix == "secrets/"


def test_parse_boundary_exclusion_rejects_missing_colon() -> None:
    with pytest.raises(BadParameter, match="slug:prefix"):
        parse_boundary_exclusion("repo-beta-secrets")


def test_parse_boundary_exclusion_rejects_empty_slug() -> None:
    with pytest.raises(BadParameter, match="slug:prefix"):
        parse_boundary_exclusion(":secrets/")


def test_parse_authority_valid() -> None:
    rule = parse_authority("github:1")
    assert rule.source_slug == "github"
    assert rule.authority_rank == 1


def test_parse_system_shape_valid() -> None:
    assert parse_system_shape("multi_repo") == SystemShape.MULTI_REPO

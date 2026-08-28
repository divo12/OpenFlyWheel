"""Plugin and Python-package program templates remain byte-identical."""

from importlib.resources import files
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ("base.md", "itsm.md"))
def test_packaged_program_template_matches_plugin_asset(name: str) -> None:
    plugin_path = Path(__file__).parents[1] / "plugins/openflywheel/program_templates" / name
    packaged = files("ofw.preparation.templates").joinpath(name).read_bytes()

    assert packaged == plugin_path.read_bytes()


def test_itsm_program_routes_failure_mining_to_local_workspace_artifacts() -> None:
    content = files("ofw.preparation.templates").joinpath("itsm.md").read_text(encoding="utf-8")

    assert "$failure-miner" in content
    assert "record_failure" in content
    assert ".workspace/failures/" in content
    assert "Do not copy Langfuse trace payloads" in content

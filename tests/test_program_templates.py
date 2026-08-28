"""Plugin and Python-package program templates remain byte-identical."""

from importlib.resources import files
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ("base.md", "itsm.md"))
def test_packaged_program_template_matches_plugin_asset(name: str) -> None:
    plugin_path = Path(__file__).parents[1] / "plugins/openflywheel/program_templates" / name
    packaged = files("ofw.preparation.templates").joinpath(name).read_bytes()

    assert packaged == plugin_path.read_bytes()

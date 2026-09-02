"""Plugin and Python-package program templates remain byte-identical."""

from importlib.resources import files
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ("base.md", "itsm.md"))
def test_packaged_program_template_matches_plugin_asset(name: str) -> None:
    plugin_path = Path(__file__).parents[1] / "plugins/openflywheel/program_templates" / name
    packaged = files("ofw.preparation.templates").joinpath(name).read_bytes()

    assert packaged == plugin_path.read_bytes()


@pytest.mark.parametrize(
    "required_instruction",
    (
        "$failure-miner",
        "record_failure",
        ".workspace/failures/",
        "$failure-curator",
        "record_failure_curation",
        ".workspace/failure-curations/",
        "Do not glob",
        "stop before forming a harness hypothesis",
        "Do not copy Langfuse trace payloads",
    ),
)
def test_itsm_program_routes_failure_mining_to_local_workspace_artifacts(
    required_instruction: str,
) -> None:
    content = files("ofw.preparation.templates").joinpath("itsm.md").read_text(encoding="utf-8")

    assert required_instruction in content


@pytest.mark.parametrize(
    "required_instruction",
    (
        "$failure-pattern-miner",
        "mine_failure_patterns",
        "exact normalized root cause",
        "not semantic clusters",
    ),
)
def test_itsm_program_routes_recorded_diagnoses_to_bounded_pattern_mining(
    required_instruction: str,
) -> None:
    content = files("ofw.preparation.templates").joinpath("itsm.md").read_text(encoding="utf-8")

    assert required_instruction in content


def test_failure_pattern_miner_skill_is_packaged() -> None:
    skill = Path(__file__).parents[1] / "plugins/openflywheel/skills/failure-pattern-miner/SKILL.md"

    assert skill.is_file()
    assert "mine_failure_patterns" in skill.read_text(encoding="utf-8")


def test_hypothesis_former_skill_and_program_stop_before_candidate_editing() -> None:
    root = Path(__file__).parents[1]
    skill = root / "plugins/openflywheel/skills/hypothesis-former/SKILL.md"
    program = files("ofw.preparation.templates").joinpath("base.md").read_text(encoding="utf-8")

    assert skill.is_file()
    assert "record_hypothesis" in skill.read_text(encoding="utf-8")
    assert "$hypothesis-former" in program
    assert "stable hypothesis receipt" in program
    assert "stop before candidate editing" in program


def test_base_program_stops_after_repeated_managed_mcp_timeout() -> None:
    content = files("ofw.preparation.templates").joinpath("base.md").read_text(encoding="utf-8")
    content = " ".join(content.split())

    assert "unknown operation status" in content
    assert "Never terminate a managed MCP or agent process" in content
    assert "stop and report the timeout" in content

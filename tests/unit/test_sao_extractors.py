"""SaO pure extractor unit tests."""

from openflywheel.contracts.enums import TruthSection
from openflywheel.ingest.sao.extractors import extract_all, extract_constants, extract_pyproject


def test_extract_constants_u3() -> None:
    content = 'SERVICE_NAME = "repo-alpha"\n'
    drafts = extract_constants(external_id="repo-alpha/src/config.py", content=content)
    assert len(drafts) == 1
    assert drafts[0].section == TruthSection.U3
    assert drafts[0].locator.value.endswith(":1")


def test_extract_pyproject_u3_u4() -> None:
    content = """
[project]
name = "alphacore"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""
    drafts = extract_pyproject(external_id="repo-alpha/pyproject.toml", content=content)
    sections = {d.section for d in drafts}
    assert TruthSection.U3 in sections
    assert TruthSection.U4 in sections


def test_extract_all_never_u5_u7() -> None:
    content = 'SERVICE_NAME = "x"\n'
    drafts = extract_all(external_id="repo-a/src/x.py", content=content)
    for draft in drafts:
        assert draft.section in (TruthSection.U3, TruthSection.U4)

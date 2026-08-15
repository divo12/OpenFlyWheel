"""Deterministic System-as-Oracle extractors over admitted code/config."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from openflywheel.contracts.enums import LocatorKind, TruthSection
from openflywheel.contracts.evidence import EvidenceLocator
from openflywheel.ingest.sao.models import SaOProposalDraft

_CONSTANT_PATTERN = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.+)$")
_PROJECT_NAME_PATTERN = re.compile(r'^\s*name\s*=\s*"([^"]+)"', re.MULTILINE)
_REQUIRES_PYTHON_PATTERN = re.compile(r"^\s*requires-python\s*=\s*\"([^\"]+)\"", re.MULTILINE)
_TESTPATHS_PATTERN = re.compile(r'^\s*testpaths\s*=\s*\[\s*"([^"]+)"\s*\]', re.MULTILINE)


def _fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _file_line_locator(external_id: str, line_no: int) -> EvidenceLocator:
    return EvidenceLocator(kind=LocatorKind.FILE_LINE, value=f"{external_id}:{line_no}")


def extract_constants(
    *,
    external_id: str,
    content: str,
) -> tuple[SaOProposalDraft, ...]:
    if not external_id.endswith(".py"):
        return tuple()
    drafts: list[SaOProposalDraft] = []
    fingerprint = _fingerprint(content)
    for line_no, line in enumerate(content.splitlines(), start=1):
        match = _CONSTANT_PATTERN.match(line.strip())
        if match is None:
            continue
        name = str(match.group(1))
        value = str(match.group(2)).strip()
        drafts.append(
            SaOProposalDraft(
                extractor="constants",
                what=f"Configuration constant {name} is defined",
                how=f"{name} = {value}",
                section=TruthSection.U3,
                locator=_file_line_locator(external_id, line_no),
                anchor_label=f"{PurePosixPath(external_id).name}:{name}",
                content_fingerprint=fingerprint,
            )
        )
    return tuple(drafts)


def extract_pyproject(
    *,
    external_id: str,
    content: str,
) -> tuple[SaOProposalDraft, ...]:
    if not external_id.endswith("pyproject.toml"):
        return tuple()
    drafts: list[SaOProposalDraft] = []
    fingerprint = _fingerprint(content)
    for pattern, label, what_tpl in (
        (_PROJECT_NAME_PATTERN, "project.name", "Package name is {value}"),
        (_REQUIRES_PYTHON_PATTERN, "requires-python", "Requires Python {value}"),
    ):
        for match in pattern.finditer(content):
            value = str(match.group(1))
            line_no = content[: match.start()].count("\n") + 1
            drafts.append(
                SaOProposalDraft(
                    extractor="pyproject",
                    what=what_tpl.format(value=value),
                    how=f"{label}={value}",
                    section=TruthSection.U3,
                    locator=_file_line_locator(external_id, line_no),
                    anchor_label=f"pyproject:{label}",
                    content_fingerprint=fingerprint,
                )
            )
    testpaths = _TESTPATHS_PATTERN.search(content)
    if testpaths is not None:
        value = str(testpaths.group(1))
        line_no = content[: testpaths.start()].count("\n") + 1
        drafts.append(
            SaOProposalDraft(
                extractor="pyproject",
                what=f"Tests are discovered under {value}",
                how=f'testpaths=["{value}"]',
                section=TruthSection.U4,
                locator=_file_line_locator(external_id, line_no),
                anchor_label="pyproject:testpaths",
                content_fingerprint=fingerprint,
            )
        )
    return tuple(drafts)


def extract_all(
    *,
    external_id: str,
    content: str,
) -> tuple[SaOProposalDraft, ...]:
    merged: list[SaOProposalDraft] = []
    merged.extend(extract_constants(external_id=external_id, content=content))
    merged.extend(extract_pyproject(external_id=external_id, content=content))
    return tuple(merged)


def build_idempotency_key(
    *,
    extractor: str,
    boundary_id: str,
    what: str,
    locator_value: str,
    content_fingerprint: str,
) -> str:
    raw = f"{extractor}|{boundary_id}|{what}|{locator_value}|{content_fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

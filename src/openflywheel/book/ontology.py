"""Generic base ontology for coverage requirements."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import SystemShape, TruthSection


class CoverageSlotTemplate(BaseModel):
    model_config = ConfigDict(frozen=True)

    section: TruthSection
    slot_key: str
    description: str
    shapes: tuple[SystemShape, ...]


_BASE_ONTOLOGY: tuple[CoverageSlotTemplate, ...] = (
    CoverageSlotTemplate(
        section=TruthSection.U1,
        slot_key="purpose",
        description="Stated system purpose and outcome",
        shapes=(
            SystemShape.MONOLITH,
            SystemShape.MULTI_REPO,
            SystemShape.SERVICE_MESH,
            SystemShape.LIBRARY,
            SystemShape.UNKNOWN,
        ),
    ),
    CoverageSlotTemplate(
        section=TruthSection.U2,
        slot_key="primary_kpi",
        description="Primary evaluation metric or guardrail",
        shapes=(
            SystemShape.MONOLITH,
            SystemShape.MULTI_REPO,
            SystemShape.SERVICE_MESH,
            SystemShape.LIBRARY,
            SystemShape.UNKNOWN,
        ),
    ),
    CoverageSlotTemplate(
        section=TruthSection.U3,
        slot_key="component_identity",
        description="Named component or package identity",
        shapes=(
            SystemShape.MONOLITH,
            SystemShape.MULTI_REPO,
            SystemShape.SERVICE_MESH,
            SystemShape.LIBRARY,
        ),
    ),
    CoverageSlotTemplate(
        section=TruthSection.U3,
        slot_key="runtime_constraints",
        description="Runtime or language constraints",
        shapes=(
            SystemShape.MONOLITH,
            SystemShape.MULTI_REPO,
            SystemShape.SERVICE_MESH,
            SystemShape.LIBRARY,
        ),
    ),
    CoverageSlotTemplate(
        section=TruthSection.U4,
        slot_key="test_discovery",
        description="How tests are discovered and executed",
        shapes=(
            SystemShape.MONOLITH,
            SystemShape.MULTI_REPO,
            SystemShape.SERVICE_MESH,
            SystemShape.LIBRARY,
        ),
    ),
    CoverageSlotTemplate(
        section=TruthSection.U5,
        slot_key="experiments",
        description="What has been tried and outcomes",
        shapes=(
            SystemShape.MONOLITH,
            SystemShape.MULTI_REPO,
            SystemShape.SERVICE_MESH,
            SystemShape.LIBRARY,
            SystemShape.UNKNOWN,
        ),
    ),
    CoverageSlotTemplate(
        section=TruthSection.U6,
        slot_key="performance",
        description="Performance baselines and known gaps",
        shapes=(
            SystemShape.MONOLITH,
            SystemShape.MULTI_REPO,
            SystemShape.SERVICE_MESH,
            SystemShape.LIBRARY,
            SystemShape.UNKNOWN,
        ),
    ),
    CoverageSlotTemplate(
        section=TruthSection.U7,
        slot_key="operations",
        description="Operations ownership and runbooks",
        shapes=(
            SystemShape.MONOLITH,
            SystemShape.MULTI_REPO,
            SystemShape.SERVICE_MESH,
            SystemShape.LIBRARY,
            SystemShape.UNKNOWN,
        ),
    ),
)


def templates_for_shape(shape: SystemShape) -> tuple[CoverageSlotTemplate, ...]:
    return tuple(slot for slot in _BASE_ONTOLOGY if shape in slot.shapes)


def resolve_verified_slot(section: TruthSection, what: str, how: str) -> str | None:
    lowered = f"{what} {how}".lower()
    if section == TruthSection.U3:
        if "package name" in lowered or "configuration constant" in lowered:
            return "component_identity"
        if "requires python" in lowered:
            return "runtime_constraints"
    if section == TruthSection.U4 and "test" in lowered:
        return "test_discovery"
    return None

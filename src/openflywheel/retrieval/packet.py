"""Context packet rendering."""

from __future__ import annotations

from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.evidence import EvidenceAnchorRecord
from openflywheel.contracts.ids import ClaimId, PinId
from openflywheel.contracts.retrieval import ContextPacket, RetrievalGap


def _claim_label(claims: tuple[ClaimRecord, ...], claim_id: ClaimId) -> str:
    for claim in claims:
        if claim.id == claim_id:
            return claim.what
    return "unresolved claim"


def render_markdown(
    *,
    pin_id: PinId | None,
    claims: tuple[ClaimRecord, ...],
    anchors: tuple[EvidenceAnchorRecord, ...],
    tensions: tuple[EdgeRecord, ...],
    gaps: tuple[RetrievalGap, ...],
) -> str:
    lines: list[str] = ["# System Book Context", ""]
    if pin_id is not None:
        lines.extend([f"**Pin:** `{pin_id}`", ""])
    lines.append("## Verified Claims (What + How)")
    if not claims:
        lines.append("- _(none matched)_")
    for claim in claims:
        lines.append(f"- **{claim.what}** — {claim.how}")
    lines.extend(["", "## Evidence Anchors"])
    if not anchors:
        lines.append("- _(none)_")
    for anchor in anchors:
        lines.append(f"- `{anchor.locator.value}` — {anchor.label}")
    lines.extend(["", "## Unresolved Tensions"])
    if not tensions:
        lines.append("- _(none)_")
    for edge in tensions:
        left = _claim_label(claims, edge.from_claim_id)
        right = _claim_label(claims, edge.to_claim_id)
        lines.append(f"- {left} ↔ {right}")
    lines.extend(["", "## Coverage Gaps"])
    if not gaps:
        lines.append("- _(none)_")
    for gap in gaps:
        section = gap.section.value if gap.section is not None else "?"
        lines.append(f"- [{section}] {gap.slot_key}: {gap.description}")
    lines.extend(["", "_Why probes are never included as gold._"])
    return "\n".join(lines)


def build_packet(
    *,
    pin_id: PinId | None,
    claims: tuple[ClaimRecord, ...],
    anchors: tuple[EvidenceAnchorRecord, ...],
    tensions: tuple[EdgeRecord, ...],
    gaps: tuple[RetrievalGap, ...],
) -> ContextPacket:
    markdown = render_markdown(
        pin_id=pin_id,
        claims=claims,
        anchors=anchors,
        tensions=tensions,
        gaps=gaps,
    )
    return ContextPacket(
        pin_id=pin_id,
        claims=claims,
        anchors=anchors,
        tensions=tensions,
        gaps=gaps,
        markdown_body=markdown,
    )

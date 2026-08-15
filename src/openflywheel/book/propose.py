"""Manual proposal helpers."""

from __future__ import annotations

from openflywheel.contracts.book import ProposeManualRequest


def manual_proposal_idempotency_key(request: ProposeManualRequest) -> str:
    anchors = "|".join(sorted(str(anchor_id) for anchor_id in request.anchor_ids))
    return (
        f"manual|{request.boundary_id}|{request.section.value}|"
        f"{request.what}|{request.how}|{anchors}"
    )

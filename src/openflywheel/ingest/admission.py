"""Fail-closed admission policy."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.contracts.enums import AdmissionDecision, RejectReason

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
)

JUNK_EXTENSIONS: frozenset[str] = frozenset({".pyc", ".pyo", ".DS_Store", ".png", ".jpg", ".gif"})


@dataclass(frozen=True)
class AdmissionVerdict:
    decision: AdmissionDecision
    reason: RejectReason | None
    detail: str
    checksum: str


def compute_checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def evaluate_admission(
    envelope: ConnectorEnvelope,
    *,
    excluded_paths: tuple[str, ...],
) -> AdmissionVerdict:
    checksum = compute_checksum(envelope.content_text)
    external_id = envelope.external_id

    for pattern in excluded_paths:
        if external_id.startswith(pattern) or f"/{pattern}" in external_id:
            return AdmissionVerdict(
                decision=AdmissionDecision.REJECT,
                reason=RejectReason.EXCLUDED_PATH,
                detail=f"path excluded by manifest: {external_id}",
                checksum=checksum,
            )

    for ext in JUNK_EXTENSIONS:
        if external_id.endswith(ext):
            return AdmissionVerdict(
                decision=AdmissionDecision.REJECT,
                reason=RejectReason.JUNK,
                detail=f"unsupported junk extension: {external_id}",
                checksum=checksum,
            )

    if len(envelope.content_text.strip()) == 0:
        return AdmissionVerdict(
            decision=AdmissionDecision.REJECT,
            reason=RejectReason.JUNK,
            detail="empty content",
            checksum=checksum,
        )

    for secret_pattern in SECRET_PATTERNS:
        if secret_pattern.search(envelope.content_text):
            return AdmissionVerdict(
                decision=AdmissionDecision.REJECT,
                reason=RejectReason.LIKELY_SECRET,
                detail=f"likely secret in {external_id}",
                checksum=checksum,
            )

    return AdmissionVerdict(
        decision=AdmissionDecision.ACCEPT,
        reason=None,
        detail="accepted",
        checksum=checksum,
    )

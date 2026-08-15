"""ACL filtering for retrieval."""

from __future__ import annotations

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.enums import VisibilityLevel
from openflywheel.contracts.ids import IdentityId


def claim_visible_to_identity(claim: ClaimRecord, identity_id: IdentityId) -> bool:
    return acl_visible_to_identity(claim.acl, identity_id)


def acl_visible_to_identity(acl: AclLabel, identity_id: IdentityId) -> bool:
    """PUBLIC: any known workspace identity. INTERNAL: any known workspace identity.

    RESTRICTED/PRIVATE: only listed allowed_identities. Unknown identity fails closed
    before this function is called. INTERNAL does not bypass RESTRICTED/PRIVATE ACLs
    on individual claims — it only marks workspace-wide visibility at admission time.
    """
    if acl.visibility == VisibilityLevel.PUBLIC:
        return True
    if acl.visibility == VisibilityLevel.INTERNAL:
        return True
    if acl.visibility in (VisibilityLevel.RESTRICTED, VisibilityLevel.PRIVATE):
        return identity_id in acl.allowed_identities
    return False


def filter_claims_by_acl(
    claims: tuple[ClaimRecord, ...],
    identity_id: IdentityId,
) -> tuple[ClaimRecord, ...]:
    return tuple(c for c in claims if claim_visible_to_identity(c, identity_id))

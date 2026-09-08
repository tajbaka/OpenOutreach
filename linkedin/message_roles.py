"""Message wording for operator-reviewed role tags, never audience routing."""
from __future__ import annotations

from linkedin.exceptions import MessageRoleError


ROLE_WORDING = {
    "Founder/CEO": "founders",
    "CFO/Finance": "finance leaders",
    "COO/Operations": "operations leaders",
    "CRO/Revenue": "revenue leaders",
    "Product Executive": "product leaders",
    "Technology/Engineering Executive": "engineering leaders",
    "CIO/Internal IT Executive": "IT leaders",
    "Security/Trust Executive": "security leaders",
    "Product/Engineering N-1": "product and engineering teams",
    "Security/Compliance N-1": "security and compliance teams",
    "Compliance/Risk/Privacy Executive": "compliance leaders",
    "FedRAMP Owner/Operator": "FedRAMP teams",
    "GRC Manager/Lead": "GRC teams",
    "GRC Engineer": "GRC teams",
    "GRC Analyst/Practitioner": "GRC teams",
    "Federal/Public Sector Executive": "public sector leaders",
    "Federal Sales/BD IC": "federal sales teams",
    "Federal Solutions Engineer/Architect": "federal solutions teams",
    "Public Sector Partnerships/Alliances/CS": "public sector partner and customer teams",
    "Field CTO/CISO/Technical Evangelist": "field technical teams",
}


def role_wording(role_tag: str | None) -> str:
    """Resolve a saved canonical tag; only a missing tag falls back to companies.

    Never infer a role from a title, ICP label, company, or profile. Unknown
    nonblank classifications fail instead of appearing verbatim in the copy.
    """
    if role_tag is None or (isinstance(role_tag, str) and not role_tag.strip()):
        return "companies"
    if not isinstance(role_tag, str) or role_tag not in ROLE_WORDING:
        raise MessageRoleError(f"No message wording for Role Tag {role_tag!r}")
    return ROLE_WORDING[role_tag]

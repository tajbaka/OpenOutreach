import json


def linkedin_profile_description(
    public_identifier: str,
    *,
    member_urn: str | None = None,
) -> str:
    return json.dumps(
        {
            "public_identifier": public_identifier,
            "url": f"https://www.linkedin.com/in/{public_identifier}/",
            "urn": member_urn or f"urn:li:fsd_profile:{public_identifier}",
        },
    )

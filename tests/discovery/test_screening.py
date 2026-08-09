import pytest

from linkedin.exceptions import DiscoveryScreeningError
from linkedin.icp_outbound import DiscoveryTarget
from linkedin.discovery.screening import screen_cards
from linkedin.discovery.sources.base import DiscoveryCard


class _Model:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, prompt):
        assert "Enabled ICPs" in prompt
        return self.payload


def _card():
    return DiscoveryCard(
        public_identifier="jane",
        linkedin_url="https://www.linkedin.com/in/jane/",
        name="Jane Doe",
        headline="VP Security",
        company_name="Example Cloud",
    )


def _targets():
    return (
        DiscoveryTarget(
            icp="CSPs",
            profile="Cloud security and compliance leaders",
        ),
    )


def test_structured_screen_accepts_enabled_icp():
    decisions = screen_cards(
        [_card()],
        _targets(),
        structured_model=_Model(
            {
                "decisions": [
                    {
                        "public_identifier": "jane",
                        "should_visit": True,
                        "potential_icp": "CSPs",
                    },
                ],
            },
        ),
    )

    assert decisions["jane"].should_visit
    assert decisions["jane"].potential_icp == "CSPs"


def test_structured_screen_rejects_invented_icp():
    with pytest.raises(DiscoveryScreeningError, match="disabled ICP"):
        screen_cards(
            [_card()],
            _targets(),
            structured_model=_Model(
                {
                    "decisions": [
                        {
                            "public_identifier": "jane",
                            "should_visit": True,
                            "potential_icp": "Invented",
                        },
                    ],
                },
            ),
        )


def test_structured_screen_requires_every_card():
    with pytest.raises(DiscoveryScreeningError, match="omitted"):
        screen_cards(
            [_card()],
            _targets(),
            structured_model=_Model({"decisions": []}),
        )

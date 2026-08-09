import pytest

from linkedin import conf
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


def test_structured_screen_visits_best_score_above_threshold(monkeypatch):
    monkeypatch.setattr(conf, "DISCOVERY_VISIT_SCORE_THRESHOLD", 70)
    decisions = screen_cards(
        [_card()],
        _targets(),
        structured_model=_Model(
            {
                "scores": [
                    {
                        "public_identifier": "jane",
                        "best_icp": "CSPs",
                        "score": 82,
                        "reason": "Security leadership matches the ICP.",
                    },
                ],
            },
        ),
    )

    assert decisions["jane"].should_visit
    assert decisions["jane"].potential_icp == "CSPs"
    assert decisions["jane"].score == 82


def test_structured_screen_skips_below_threshold(monkeypatch):
    monkeypatch.setattr(conf, "DISCOVERY_VISIT_SCORE_THRESHOLD", 70)
    decisions = screen_cards(
        [_card()],
        _targets(),
        structured_model=_Model(
            {
                "scores": [
                    {
                        "public_identifier": "jane",
                        "best_icp": "CSPs",
                        "score": 45,
                        "reason": "Generic cloud company signal only.",
                    },
                ],
            },
        ),
    )

    assert not decisions["jane"].should_visit
    assert decisions["jane"].potential_icp == ""
    assert decisions["jane"].score == 45


def test_structured_screen_accepts_best_scoring_enabled_icp(monkeypatch):
    monkeypatch.setattr(conf, "DISCOVERY_VISIT_SCORE_THRESHOLD", 70)
    decisions = screen_cards(
        [_card()],
        (
            DiscoveryTarget(icp="CSPs", profile="Cloud security leaders"),
            DiscoveryTarget(icp="Advisors", profile="Compliance advisors"),
        ),
        structured_model=_Model(
            {
                "scores": [
                    {
                        "public_identifier": "jane",
                        "best_icp": "Advisors",
                        "score": 91,
                        "reason": "Compliance advisory role.",
                    },
                ],
            },
        ),
    )

    assert decisions["jane"].should_visit
    assert decisions["jane"].potential_icp == "Advisors"
    assert decisions["jane"].score == 91


def test_structured_screen_canonicalizes_wrapped_identifier(monkeypatch):
    monkeypatch.setattr(conf, "DISCOVERY_VISIT_SCORE_THRESHOLD", 70)
    decisions = screen_cards(
        [_card()],
        _targets(),
        structured_model=_Model(
            {
                "scores": [
                    {
                        "public_identifier": "ja\nne",
                        "best_icp": "CSPs",
                        "score": 82,
                        "reason": "Security leadership matches the ICP.",
                    },
                ],
            },
        ),
    )

    assert decisions["jane"].should_visit


def test_structured_screen_rejects_invented_icp():
    with pytest.raises(DiscoveryScreeningError, match="disabled ICP"):
        screen_cards(
            [_card()],
            _targets(),
            structured_model=_Model(
                {
                    "scores": [
                        {
                            "public_identifier": "jane",
                            "best_icp": "Invented",
                            "score": 90,
                            "reason": "Invalid ICP.",
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
            structured_model=_Model({"scores": []}),
        )

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from linkedin.discovery.sources.mynetwork_recommendations import (
    _extract_sections,
    _section_show_all_link,
    collect_mynetwork_recommendations,
    is_supported_section_heading,
)
from linkedin.discovery.sources.recommendation_common import (
    RecommendationSourceResult,
    assert_authenticated,
    cards_from_rows,
    collect_dialog_cards,
    dismiss_dialog,
)
from linkedin.discovery.sources.profile_recommendations import (
    MORE_PROFILES_LINK_SELECTOR,
    collect_profile_recommendations,
)
from linkedin.exceptions import AuthenticationError, DiscoverySurfaceError


FIXTURE = Path("tests/fixtures/discovery/recommendation_sections.json")


class _EvaluatedLocator:
    def __init__(self, value):
        self.value = value

    def evaluate_all(self, script):
        assert "People you may know" in script
        return self.value


class _BodyLocator:
    def __init__(self, text):
        self.text = text

    def inner_text(self, timeout=None):
        return self.text


class _Mouse:
    def move(self, x, y):
        return None

    def wheel(self, x, y):
        return None


class _Keyboard:
    def press(self, key):
        return None


class _Page:
    url = "https://www.linkedin.com/mynetwork/grow/"

    def __init__(self, sections, body=""):
        self.sections = sections
        self.body = body
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()

    def locator(self, selector):
        if selector == "body":
            return _BodyLocator(self.body)
        return _EvaluatedLocator(self.sections)

    def wait_for_timeout(self, value):
        return None


def _fixture():
    return json.loads(FIXTURE.read_text())


def test_supported_headings_are_strict_and_dynamic():
    assert is_supported_section_heading("Suggestions for you")
    assert is_supported_section_heading("People you may know from Example Cloud")
    assert is_supported_section_heading("People you may know in Example City")
    assert not is_supported_section_heading("Invitations (20)")
    assert not is_supported_section_heading("Explore Premium profiles")
    assert not is_supported_section_heading("People who viewed your profile")


def test_snapshot_filters_non_recommendation_sections_and_canonicalizes_cards():
    data = _fixture()
    page = _Page(data["sections"])

    sections = _extract_sections(page)
    headings = [section["heading"] for section in sections]
    cards = [
        card
        for section in sections
        for card in cards_from_rows(
            section["rows"],
            source_kind="mynetwork_recommendation",
            source_section=section["heading"],
            recommendation_depth=0,
        )
    ]

    assert headings == [
        "People you may know from Example Cloud",
        "Suggestions for you",
    ]
    assert {card.public_identifier for card in cards} == {
        "example-security-leader",
        "example-public-sector-founder",
    }
    assert all(card.recommendation_depth == 0 for card in cards)


def test_profile_rows_include_only_profile_anchors_and_are_depth_one():
    cards = cards_from_rows(
        _fixture()["profile_rows"],
        source_kind="profile_recommendation",
        source_section="More profiles for you",
        source_profile_public_identifier="seed-profile",
        recommendation_depth=1,
    )

    assert [card.public_identifier for card in cards] == ["example-related-ciso"]
    assert cards[0].recommendation_depth == 1
    assert cards[0].source_profile_public_identifier == "seed-profile"


def test_collects_inline_sections_and_stops_after_empty_scrolls(monkeypatch):
    data = _fixture()
    page = _Page(data["sections"])
    session = SimpleNamespace(page=page)
    monkeypatch.setattr(
        "linkedin.discovery.sources.mynetwork_recommendations.goto_page",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "linkedin.discovery.sources.mynetwork_recommendations._click_show_all",
        lambda page, heading, label: object(),
    )
    monkeypatch.setattr(
        "linkedin.discovery.sources.mynetwork_recommendations.collect_dialog_cards",
        lambda *args, **kwargs: ([], 0, 1),
    )
    monkeypatch.setattr(
        "linkedin.discovery.sources.mynetwork_recommendations.dismiss_dialog",
        lambda dialog: None,
    )
    monkeypatch.setattr(
        "linkedin.discovery.sources.mynetwork_recommendations._scroll_page",
        lambda page: None,
    )

    result = collect_mynetwork_recommendations(
        session,
        max_cards=20,
        max_sections=5,
        max_scroll_rounds=5,
        max_consecutive_empty_scrolls=2,
    )

    assert isinstance(result, RecommendationSourceResult)
    assert result.sections_scanned == 2
    assert result.consecutive_empty_scrolls == 2
    assert result.stop_reason == "source_exhausted"
    assert len(result.cards) == 2
    assert result.overlays_opened == 1


def test_selector_drift_fails_instead_of_harvesting_page_links(monkeypatch):
    page = _Page([], body="Suggestions for you Example Person")
    session = SimpleNamespace(page=page)
    monkeypatch.setattr(
        "linkedin.discovery.sources.mynetwork_recommendations.goto_page",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "linkedin.discovery.sources.mynetwork_recommendations._scroll_page",
        lambda page: None,
    )

    with pytest.raises(DiscoverySurfaceError, match="no supported section"):
        collect_mynetwork_recommendations(
            session,
            max_cards=20,
            max_sections=5,
            max_scroll_rounds=2,
            max_consecutive_empty_scrolls=1,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/checkpoint/challenge/123",
        "https://www.linkedin.com/challenge/verify",
    ],
)
def test_authentication_loss_is_explicit(url):
    with pytest.raises(AuthenticationError):
        assert_authenticated(SimpleNamespace(url=url))


class _VisibleLink:
    def __init__(self):
        self.clicked = False

    def is_visible(self):
        return True

    def click(self):
        self.clicked = True


class _LinkList:
    def __init__(self, links):
        self.links = links

    def all(self):
        return self.links

    def count(self):
        return len(self.links)


class _ScopedNode:
    def __init__(self, *, parent=None, profile_count=0, links=None):
        self.parent = parent
        self.profile_count = profile_count
        self.links = links or []

    def is_visible(self):
        return True

    def count(self):
        return 1

    def locator(self, selector):
        if selector == "xpath=..":
            return self.parent
        if selector == 'a[href*="/in/"]':
            return _LinkList([object()] * self.profile_count)
        raise AssertionError(selector)

    def get_by_role(self, role, name, exact):
        assert role == "link"
        assert exact
        return _LinkList(self.links)


class _ScopedPage:
    def __init__(self, heading):
        self.heading = heading

    def get_by_role(self, role, name, exact):
        assert role == "heading"
        assert exact
        return _LinkList([self.heading])


def test_show_all_is_resolved_from_its_exact_section_container():
    expected = _VisibleLink()
    unrelated = _VisibleLink()
    outer = _ScopedNode(profile_count=4, links=[unrelated])
    section = _ScopedNode(parent=outer, profile_count=3, links=[expected])
    section_header = _ScopedNode(
        parent=section,
        profile_count=0,
        links=[expected],
    )
    heading = _ScopedNode(parent=section_header)

    resolved = _section_show_all_link(
        _ScopedPage(heading),
        "People you may know from Example Cloud",
        "Show all suggestions for People you may know from Example Cloud",
    )

    assert resolved is expected
    assert not unrelated.clicked


class _Rail:
    def __init__(self, link):
        self.link = link
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return _LinkList([self.link])


def test_profile_source_clicks_only_exact_browsemap_link(monkeypatch):
    page = _Page([])
    page.url = "https://www.linkedin.com/in/seed-profile/"
    session = SimpleNamespace(page=page)
    show_all = _VisibleLink()
    rail = _Rail(show_all)
    inline = cards_from_rows(
        _fixture()["profile_rows"],
        source_kind="profile_recommendation",
        source_section="More profiles for you",
        source_profile_public_identifier="seed-profile",
        recommendation_depth=1,
    )
    overlay = cards_from_rows(
        [
            {
                "href": "/in/overlay-related/",
                "name": "Overlay Related",
                "headline": "Security Leader",
                "company_name": "Example",
                "context": "Overlay Related Security Leader",
            },
        ],
        source_kind="profile_recommendation",
        source_section="More profiles for you",
        source_profile_public_identifier="seed-profile",
        recommendation_depth=1,
    )
    monkeypatch.setattr(
        "linkedin.discovery.sources.profile_recommendations._profile_rail",
        lambda page: rail,
    )
    monkeypatch.setattr(
        "linkedin.discovery.sources.profile_recommendations.extract_cards",
        lambda *args, **kwargs: inline,
    )
    dialog = object()
    monkeypatch.setattr(
        "linkedin.discovery.sources.profile_recommendations."
        "recommendation_overlay_by_heading",
        lambda page, heading: dialog,
    )
    monkeypatch.setattr(
        "linkedin.discovery.sources.profile_recommendations.collect_dialog_cards",
        lambda *args, **kwargs: (overlay, 2, 1),
    )
    dismissed = []
    monkeypatch.setattr(
        "linkedin.discovery.sources.profile_recommendations.dismiss_dialog",
        lambda value: dismissed.append(value),
    )

    result = collect_profile_recommendations(
        session,
        source_profile_public_identifier="seed-profile",
        max_cards=20,
        max_scroll_rounds=4,
        max_consecutive_empty_scrolls=2,
    )

    assert rail.selectors == [MORE_PROFILES_LINK_SELECTOR]
    assert show_all.clicked
    assert dismissed == [dialog]
    assert result.overlays_opened == 1
    assert {card.public_identifier for card in result.cards} == {
        "example-related-ciso",
        "overlay-related",
    }
    assert all(card.recommendation_depth == 1 for card in result.cards)


def test_profile_source_absence_is_clean(monkeypatch):
    page = _Page([])
    page.url = "https://www.linkedin.com/in/seed-profile/"
    monkeypatch.setattr(
        "linkedin.discovery.sources.profile_recommendations._profile_rail",
        lambda page: None,
    )

    result = collect_profile_recommendations(
        SimpleNamespace(page=page),
        source_profile_public_identifier="seed-profile",
        max_cards=20,
        max_scroll_rounds=4,
        max_consecutive_empty_scrolls=2,
    )

    assert result.cards == ()
    assert result.sections_scanned == 0


def test_dialog_scroll_stops_after_consecutive_empty_rounds(monkeypatch):
    card = cards_from_rows(
        [_fixture()["profile_rows"][0]],
        source_kind="profile_recommendation",
        source_section="More profiles for you",
        recommendation_depth=1,
    )[0]
    monkeypatch.setattr(
        "linkedin.discovery.sources.recommendation_common.extract_cards",
        lambda *args, **kwargs: [card],
    )
    scrolls = []
    monkeypatch.setattr(
        "linkedin.discovery.sources.recommendation_common."
        "scroll_recommendation_container",
        lambda dialog: scrolls.append(True),
    )

    cards, rounds, empty = collect_dialog_cards(
        object(),
        source_kind="profile_recommendation",
        source_section="More profiles for you",
        recommendation_depth=1,
        max_cards=10,
        max_scroll_rounds=10,
        max_consecutive_empty_scrolls=2,
    )

    assert cards == [card]
    assert rounds == 2
    assert empty == 2
    assert len(scrolls) == 2


class _DismissLocator:
    def __init__(self):
        self.clicked = False

    def count(self):
        return 1

    def all(self):
        return [self]

    def is_visible(self):
        return True

    def click(self):
        self.clicked = True


class _Dialog:
    def __init__(self):
        self.dismiss = _DismissLocator()
        self.selector = ""
        self.page = SimpleNamespace(wait_for_timeout=lambda value: None)

    def locator(self, selector):
        self.selector = selector
        return self.dismiss


def test_overlay_close_uses_only_exact_dismiss_control():
    dialog = _Dialog()

    dismiss_dialog(dialog)

    assert dialog.selector == 'button[aria-label="Dismiss"]'
    assert dialog.dismiss.clicked

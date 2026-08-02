from types import SimpleNamespace

import pytest

from linkedin.discovery.sources.people_search import (
    PEOPLE_RESULT_CARD_SELECTOR,
    collect_people_search_cards,
)
from linkedin.exceptions import DiscoverySurfaceError


class _TextLocator:
    def __init__(self, text="", count=1):
        self.text = text
        self._count = count
        self.first = self

    def count(self):
        return self._count

    def inner_text(self, timeout=None):
        return self.text


class _LinkLocator(_TextLocator):
    def __init__(self, href):
        super().__init__("")
        self.href = href

    def get_attribute(self, name, timeout=None):
        return self.href if name == "href" else None


class _CardLocator:
    def __init__(self, href, name, headline, company, context):
        self.values = {
            ".entity-result__title-text": name,
            ".entity-result__primary-subtitle": headline,
            "[data-anonymize='company-name']": company,
        }
        self.link = _LinkLocator(href)
        self.context = context

    def locator(self, selector):
        if selector == 'a[href*="/in/"]':
            return self.link
        value = self.values.get(selector)
        return _TextLocator(value or "", count=int(bool(value)))

    def inner_text(self, timeout=None):
        return self.context


class _ContainerList:
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items


class _Page:
    url = "https://www.linkedin.com/search/results/people/"

    def __init__(self, cards, body="results", generic_links=0):
        self.cards = cards
        self.body = body
        self.generic_links = generic_links

    def locator(self, selector):
        if selector == PEOPLE_RESULT_CARD_SELECTOR:
            return _ContainerList(self.cards)
        if selector == "body":
            return _TextLocator(self.body)
        if selector == 'main a[href*="/in/"]':
            return _TextLocator(count=self.generic_links)
        raise AssertionError(selector)


def test_extracts_only_supported_people_result_cards(monkeypatch):
    monkeypatch.setattr(
        "linkedin.discovery.sources.people_search.search_people",
        lambda session, query, page: None,
    )
    page = _Page(
        [
            _CardLocator(
                "/in/Jane-Doe/?miniProfileUrn=1",
                "Jane Doe",
                "VP Security",
                "Example Cloud",
                "Jane Doe VP Security Example Cloud",
            ),
        ],
    )
    session = SimpleNamespace(page=page)

    cards = collect_people_search_cards(
        session,
        query="FedRAMP CISO",
        page_number=1,
    )

    assert len(cards) == 1
    assert cards[0].public_identifier == "jane-doe"
    assert cards[0].company_name == "Example Cloud"


def test_selector_drift_does_not_fall_back_to_all_profile_links(monkeypatch):
    monkeypatch.setattr(
        "linkedin.discovery.sources.people_search.search_people",
        lambda session, query, page: None,
    )
    session = SimpleNamespace(page=_Page([], generic_links=3))

    with pytest.raises(DiscoverySurfaceError, match="no supported result card"):
        collect_people_search_cards(session, query="FedRAMP", page_number=1)


def test_explicit_no_results_is_clean_exhaustion(monkeypatch):
    monkeypatch.setattr(
        "linkedin.discovery.sources.people_search.search_people",
        lambda session, query, page: None,
    )
    session = SimpleNamespace(page=_Page([], body="No results found", generic_links=0))

    assert collect_people_search_cards(
        session,
        query="FedRAMP",
        page_number=2,
    ) == []

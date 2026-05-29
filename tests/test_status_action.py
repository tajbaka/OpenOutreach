from unittest.mock import Mock, patch

from linkedin.actions.status import SELECTORS, get_connection_status
from linkedin.enums import ProfileState


class _Locator:
    def __init__(self, count=0):
        self._count = count

    def count(self):
        return self._count


class _TopCard:
    def __init__(self, *, text="", has_invite=False, has_pending=False):
        self.text = text
        self.has_invite = has_invite
        self.has_pending = has_pending

    def locator(self, selector):
        if selector == SELECTORS["pending_button"]:
            return _Locator(count=1 if self.has_pending else 0)
        if selector == SELECTORS["invite_to_connect"]:
            return _Locator(count=1 if self.has_invite else 0)
        return _Locator(count=0)

    def inner_text(self):
        return self.text


def test_connection_status_invite_anchor_beats_contradictory_first_degree_text():
    session = Mock()
    session.page.url = "https://www.linkedin.com/in/jen-giacomini-7ba2b98/"
    profile = {
        "public_identifier": "jen-giacomini-7ba2b98",
        "url": "https://www.linkedin.com/in/jen-giacomini-7ba2b98/",
    }
    top_card = _TopCard(text="Jen Giacomini\n· 1st\n· 2nd\nConnect", has_invite=True)

    with (
        patch("linkedin.actions.status.search_profile"),
        patch("linkedin.actions.status.find_top_card", return_value=top_card),
    ):
        status = get_connection_status(session, profile)

    assert status == ProfileState.QUALIFIED

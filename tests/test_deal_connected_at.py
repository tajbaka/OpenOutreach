"""Tests for Deal.connected_at — stamped by set_profile_state when a Deal
flips into CONNECTED. Captures the "when did they accept the invite?"
moment, which the followup classifier uses to bump priority on fresh
accepts.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Deal
from linkedin.db.deals import set_profile_state
from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from linkedin.enums import ProfileState


SAMPLE_PROFILE = {
    "first_name": "Cleo",
    "last_name": "Ndubuisi",
    "headline": "Compliance",
    "positions": [{"company_name": "Acme"}],
}


def _make_pending(session, public_id):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, ProfileState.PENDING.value)


def _deal(public_id, session):
    url = f"https://www.linkedin.com/in/{public_id}/"
    return Deal.objects.get(lead__linkedin_url=url, campaign=session.campaign)


@pytest.mark.django_db
class TestConnectedAtStamping:
    @pytest.fixture(autouse=True)
    def _db(self, embeddings_db):
        pass

    def test_field_default_none_for_new_deal(self, fake_session):
        _make_pending(fake_session, "alice")
        assert _deal("alice", fake_session).connected_at is None

    def test_stamped_on_first_pending_to_connected_flip(self, fake_session):
        _make_pending(fake_session, "alice")
        before = timezone.now()
        set_profile_state(fake_session, "alice", ProfileState.CONNECTED.value)
        after = timezone.now()

        deal = _deal("alice", fake_session)
        assert deal.connected_at is not None
        assert before <= deal.connected_at <= after

    def test_idempotent_on_re_save_of_connected_state(self, fake_session):
        """Re-flipping CONNECTED → CONNECTED (or PENDING → CONNECTED after
        a manual revert) must not overwrite the original accept moment.
        Bumping priority on freshly-connected leads only works if the
        timestamp reflects the *first* accept."""
        _make_pending(fake_session, "alice")
        set_profile_state(fake_session, "alice", ProfileState.CONNECTED.value)
        original = _deal("alice", fake_session).connected_at

        # Pretend operator reverted then re-accepted some time later — the
        # workflow rule is to keep the first stamp, since age-since-connection
        # is what matters for priority.
        set_profile_state(fake_session, "alice", ProfileState.PENDING.value)
        set_profile_state(fake_session, "alice", ProfileState.CONNECTED.value)

        assert _deal("alice", fake_session).connected_at == original

    def test_not_stamped_for_other_state_transitions(self, fake_session):
        """Going to FAILED / COMPLETED / QUALIFIED never stamps connected_at —
        the field is specifically about the PENDING → CONNECTED moment."""
        _make_pending(fake_session, "alice")
        set_profile_state(fake_session, "alice", ProfileState.FAILED.value)
        assert _deal("alice", fake_session).connected_at is None

    def test_legacy_connected_deal_can_be_backfilled_directly(self, fake_session):
        """Existing rows pre-dating this field have connected_at=None.
        The migration is non-destructive — backfilling is a separate
        manual step the operator can run later. Locking in that the
        field is freely settable from outside set_profile_state for
        whatever backfill script we eventually write."""
        _make_pending(fake_session, "alice")
        set_profile_state(fake_session, "alice", ProfileState.CONNECTED.value)
        deal = _deal("alice", fake_session)

        # Wipe the field as if it were a legacy row, then backfill.
        Deal.objects.filter(pk=deal.pk).update(connected_at=None)
        backfill_ts = timezone.now() - timedelta(days=10)
        Deal.objects.filter(pk=deal.pk).update(connected_at=backfill_ts)

        assert _deal("alice", fake_session).connected_at == backfill_ts

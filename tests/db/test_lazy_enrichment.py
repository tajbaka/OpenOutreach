# tests/db/test_lazy_enrichment.py
"""Tests for lazy enrichment and embedding helpers."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import numpy as np
import pytest


FAKE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer at Acme",
    "positions": [{"company_name": "Acme Corp"}],
}


class TestEnsureLeadEnriched:
    def test_already_enriched(self, fake_session):
        """Returns True immediately when lead already has a description."""
        from crm.models import Lead
        from linkedin.db.enrichment import ensure_lead_enriched

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
            description=json.dumps(FAKE_PROFILE),
        )

        with patch("linkedin.db.enrichment._fetch_profile") as mock_fetch:
            assert ensure_lead_enriched(fake_session, lead.pk, "alice") is True
            mock_fetch.assert_not_called()

    def test_enriches_url_only_lead(self, fake_session):
        """Fetches profile via Voyager API and populates the lead."""
        from crm.models import Lead
        from linkedin.db.enrichment import ensure_lead_enriched

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
        )
        assert not lead.description

        with patch(
            "linkedin.db.enrichment._fetch_profile",
            return_value=FAKE_PROFILE,
        ):
            assert ensure_lead_enriched(fake_session, lead.pk, "alice") is True

        lead.refresh_from_db()
        assert lead.description
        assert lead.first_name == "Alice"

    def test_overwrites_seeded_name_and_company(self, fake_session):
        """Scraped profile data replaces stale seeded lead fields."""
        from crm.models import Lead
        from linkedin.db.enrichment import ensure_lead_enriched

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/allen-r-mayfield/",
            public_identifier="allen-r-mayfield",
            first_name="Allen",
            last_name="Mayfield",
            company_name="GDI",
        )
        scraped_profile = {
            **FAKE_PROFILE,
            "first_name": 'Allen "Al"',
            "last_name": "Mayfield    ",
            "positions": [{"company_name": "Global Defense, Inc."}],
        }

        with patch(
            "linkedin.db.enrichment._fetch_profile",
            return_value=scraped_profile,
        ):
            assert ensure_lead_enriched(fake_session, lead.pk, "allen-r-mayfield") is True

        lead.refresh_from_db()
        assert lead.first_name == 'Allen "Al"'
        assert lead.last_name == "Mayfield    "
        assert lead.company_name == "Global Defense, Inc."
        assert json.loads(lead.description)["first_name"] == 'Allen "Al"'

    def test_returns_false_on_api_failure(self, fake_session):
        """Returns False when Voyager API returns (None, None)."""
        from crm.models import Lead
        from linkedin.db.enrichment import ensure_lead_enriched

        lead = Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
        )

        with patch(
            "linkedin.db.enrichment._fetch_profile",
            return_value=None,
        ):
            assert ensure_lead_enriched(fake_session, lead.pk, "alice") is False

        lead.refresh_from_db()
        assert not lead.description

    def test_returns_false_for_missing_lead(self, fake_session):
        """Returns False when lead PK doesn't exist."""
        from linkedin.db.enrichment import ensure_lead_enriched

        assert ensure_lead_enriched(fake_session, 99999, "nobody") is False


class TestEnsureProfileEmbedded:
    def test_already_embedded(self, fake_session, embeddings_db):
        """Returns True immediately when embedding exists."""
        from crm.models import Lead
        from linkedin.db.enrichment import ensure_profile_embedded

        emb = np.ones(384, dtype=np.float32)
        Lead.objects.create(
            pk=1, public_identifier="alice",
            linkedin_url="https://www.linkedin.com/in/alice/",
            embedding=emb.tobytes(),
        )

        with patch("linkedin.ml.embeddings.embed_profile") as mock_embed:
            assert ensure_profile_embedded(1, "alice", fake_session) is True
            mock_embed.assert_not_called()

    def test_embeds_enriched_lead(self, fake_session, embeddings_db):
        """Creates embedding from lead description."""
        from crm.models import Lead
        from linkedin.db.enrichment import ensure_profile_embedded

        Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/alice/",
            public_identifier="alice",
            description=json.dumps(FAKE_PROFILE),
            pk=42,
        )

        with patch("linkedin.ml.embeddings.embed_profile", return_value=True) as mock_embed:
            assert ensure_profile_embedded(42, "alice", fake_session) is True
            mock_embed.assert_called_once_with(42, "alice", FAKE_PROFILE)

    def test_enriches_then_embeds_with_session(self, fake_session, embeddings_db):
        """When session is provided, enriches url-only lead then embeds."""
        from crm.models import Lead
        from linkedin.db.enrichment import ensure_profile_embedded

        Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/bob/",
            public_identifier="bob",
            pk=44,
        )

        with (
            patch(
                "linkedin.db.enrichment._fetch_profile",
                return_value=FAKE_PROFILE,
            ),
            patch(
                "linkedin.ml.embeddings.embed_profile",
                return_value=True,
            ) as mock_embed,
        ):
            assert ensure_profile_embedded(44, "bob", session=fake_session) is True
            mock_embed.assert_called_once()

    def test_returns_false_with_session_on_api_failure(self, fake_session, embeddings_db):
        """Returns False when session provided but enrichment fails."""
        from crm.models import Lead
        from linkedin.db.enrichment import ensure_profile_embedded

        Lead.objects.create(
            linkedin_url="https://www.linkedin.com/in/bob/",
            public_identifier="bob",
            pk=45,
        )

        with patch(
            "linkedin.db.enrichment._fetch_profile",
            return_value=None,
        ):
            assert ensure_profile_embedded(45, "bob", session=fake_session) is False

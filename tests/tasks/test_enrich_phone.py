"""Tests for the enrich_phone task handler."""
from unittest.mock import patch

import pytest
from django.utils import timezone

from crm.models import Lead
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.models import Task
from linkedin.tasks.enrich_phone import handle_enrich_phone


def _lead(**over):
    base = dict(
        first_name="Ada", last_name="Lovelace", company_name="AE",
        linkedin_url="https://www.linkedin.com/in/ada/",
    )
    base.update(over)
    return Lead.objects.create(**base)


def _task(lead):
    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload={"lead_id": lead.id, "bettercontact_request_id": ""},
    )


@pytest.mark.django_db
def test_found_writes_phone_and_stamps():
    lead = _lead()
    task = _task(lead)
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+14155550199",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        result = handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone == "+14155550199"
    assert lead.phone_enriched_at is not None
    assert result.status == EnrichmentStatus.FOUND
    mock_notify.assert_called_once()


@pytest.mark.django_db
def test_not_found_stamps_but_leaves_phone_empty():
    lead = _lead()
    task = _task(lead)
    nf = EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider="prospeo")
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=nf), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone == ""
    assert lead.phone_enriched_at is not None
    mock_notify.assert_called_once()


@pytest.mark.django_db
def test_api_failure_does_not_stamp_and_does_not_notify():
    lead = _lead()
    task = _task(lead)
    fail = EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider="prospeo")
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=fail), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        result = handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone == ""
    assert lead.phone_enriched_at is None  # next reply re-attempts
    assert result.status == EnrichmentStatus.API_FAILURE
    mock_notify.assert_not_called()


@pytest.mark.django_db
def test_already_enriched_lead_is_skipped():
    lead = _lead(phone_enriched_at=timezone.now())
    task = _task(lead)
    with patch("linkedin.tasks.enrich_phone.run_waterfall") as mock_wf:
        result = handle_enrich_phone(task)
    assert result is None
    mock_wf.assert_not_called()


@pytest.mark.django_db
def test_disqualified_lead_is_skipped():
    lead = _lead(disqualified=True)
    task = _task(lead)
    with patch("linkedin.tasks.enrich_phone.run_waterfall") as mock_wf:
        result = handle_enrich_phone(task)
    assert result is None
    mock_wf.assert_not_called()


@pytest.mark.django_db
def test_missing_lead_is_skipped():
    task = Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload={"lead_id": 999999, "bettercontact_request_id": ""},
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall") as mock_wf:
        result = handle_enrich_phone(task)
    assert result is None
    mock_wf.assert_not_called()

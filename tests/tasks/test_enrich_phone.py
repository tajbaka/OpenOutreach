"""Tests for the enrich_phone task handler (multi-number contract)."""
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


def _task(lead, provider=None):
    payload = {"lead_id": lead.id, "bettercontact_request_id": ""}
    if provider is not None:
        payload["provider"] = provider
    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload=payload,
    )


@pytest.mark.django_db
def test_found_appends_phone_and_records_provider():
    lead = _lead()
    task = _task(lead, "leadmagic")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+14155550199",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        result = handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone_numbers == ["+14155550199"]
    assert lead.phones[0]["provider"] == "leadmagic"
    assert lead.phone_providers_tried == ["leadmagic"]
    assert result.status == EnrichmentStatus.FOUND
    mock_notify.assert_called_once()


@pytest.mark.django_db
def test_not_found_records_provider_but_no_number():
    lead = _lead()
    task = _task(lead, "prospeo")
    nf = EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider="prospeo")
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=nf), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phones == []
    assert lead.phone_providers_tried == ["prospeo"]
    mock_notify.assert_called_once()


@pytest.mark.django_db
def test_api_failure_records_nothing_and_does_not_notify():
    lead = _lead()
    task = _task(lead, "prospeo")
    fail = EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider="prospeo")
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=fail), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        result = handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phones == []
    assert lead.phone_providers_tried == []  # stays retryable
    assert result.status == EnrichmentStatus.API_FAILURE
    mock_notify.assert_not_called()


@pytest.mark.django_db
def test_found_appends_to_existing_phones():
    lead = _lead(
        phones=[{"number": "+1111", "provider": "leadmagic", "found_at": "x"}],
        phone_providers_tried=["leadmagic"],
    )
    task = _task(lead, "bettercontact")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="bettercontact", phone="+2222",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone_numbers == ["+1111", "+2222"]
    assert lead.phone_providers_tried == ["leadmagic", "bettercontact"]


@pytest.mark.django_db
def test_duplicate_number_not_appended_but_provider_recorded():
    lead = _lead(
        phones=[{"number": "+1555", "provider": "leadmagic", "found_at": "x"}],
        phone_providers_tried=["leadmagic"],
    )
    task = _task(lead, "bettercontact")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="bettercontact", phone="+1555",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone_numbers == ["+1555"]  # no duplicate entry
    assert lead.phone_providers_tried == ["leadmagic", "bettercontact"]


def test_normalize_phone_collapses_formatting():
    from linkedin.tasks.enrich_phone import _normalize_phone

    assert _normalize_phone("+1 904 945 0716") == "+19049450716"
    assert _normalize_phone("+19049450716") == "+19049450716"
    assert _normalize_phone("(904) 945-0716") == "+9049450716"
    assert _normalize_phone("") == ""
    assert _normalize_phone(None) == ""


@pytest.mark.django_db
def test_same_number_different_format_is_deduped():
    lead = _lead(
        phones=[{"number": "+1 904 945 0716", "provider": "bettercontact",
                 "found_at": "x"}],
        phone_providers_tried=["bettercontact"],
    )
    task = _task(lead, "leadmagic")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+19049450716",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    lead.refresh_from_db()
    # Same number, different formatting → one entry, but leadmagic recorded.
    assert len(lead.phones) == 1
    assert lead.phone_providers_tried == ["bettercontact", "leadmagic"]


@pytest.mark.django_db
def test_single_provider_already_tried_is_skipped():
    lead = _lead(phone_providers_tried=["leadmagic"])
    task = _task(lead, "leadmagic")
    with patch("linkedin.tasks.enrich_phone.run_waterfall") as mock_wf:
        result = handle_enrich_phone(task)
    assert result is None
    mock_wf.assert_not_called()


@pytest.mark.django_db
def test_waterfall_skipped_when_all_providers_tried():
    lead = _lead(phone_providers_tried=["bettercontact", "leadmagic", "prospeo"])
    task = _task(lead, "waterfall")
    with patch("linkedin.tasks.enrich_phone.run_waterfall") as mock_wf:
        result = handle_enrich_phone(task)
    assert result is None
    mock_wf.assert_not_called()


@pytest.mark.django_db
def test_waterfall_runs_when_some_providers_untried():
    lead = _lead(phone_providers_tried=["bettercontact"])
    task = _task(lead, "waterfall")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    wf.assert_called_once()


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


@pytest.mark.django_db
def test_waterfall_provider_runs_full_chain():
    lead = _lead()
    task = _task(lead, "waterfall")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="bettercontact", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    assert wf.call_count == 1
    assert "chain" not in wf.call_args.kwargs


@pytest.mark.django_db
def test_absent_provider_runs_full_chain():
    lead = _lead()
    task = _task(lead)  # legacy payload — no "provider" key
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="bettercontact", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    assert "chain" not in wf.call_args.kwargs


@pytest.mark.django_db
def test_single_provider_runs_one_element_chain():
    from linkedin.enrichment.waterfall import PROVIDERS_BY_NAME

    lead = _lead()
    task = _task(lead, "leadmagic")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    assert wf.call_args.kwargs["chain"] == [PROVIDERS_BY_NAME["leadmagic"]]


@pytest.mark.django_db
def test_unknown_provider_falls_back_to_full_chain():
    lead = _lead()
    task = _task(lead, "bogus")
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="bettercontact", phone="+1",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found) as wf, \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched"):
        handle_enrich_phone(task)
    assert "chain" not in wf.call_args.kwargs

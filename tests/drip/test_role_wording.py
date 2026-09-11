"""Drip uses shared role wording without routing or rewriting frozen messages."""

from copy import deepcopy
from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Lead
from drip.manifest import validate_manifest
from drip.models import DripDelivery, DripLane
from drip.services import reconciliation
from drip.services.reconciliation import _render_step, reconcile_drips
from linkedin.exceptions import MessageRoleError
from linkedin.message_roles import ROLE_WORDING
from linkedin.models import Task
from tests.drip.test_reconciliation import _domain


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolate_role_rendering(monkeypatch):
    monkeypatch.setattr(
        "linkedin.tasks.stop_checks.lead_automation_stop_reason",
        lambda lead: "",
    )
    monkeypatch.setattr("linkedin.tasks.follow_up.ENABLE_ACTIVE_HOURS", False)


def _role_payload(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    for theme in payload["audiences"]["CSPs"]["themes"]:
        sender = theme["senders"]["Arian"]
        for step in sender["linkedin"]:
            step["body"] = "Hi {first_name}, I work with {role} at {company_name}."
        for index, step in enumerate(sender["gmail"]):
            step["body"] = "Hi {first_name}, I work with {role} at {company_name}."
            if index == 0:
                step["subject"] = "For {role} at {company_name}"
    return payload


def _both_lanes(valid_drip_payload, *, now, role_tag=""):
    published, enrollment, gmail_lane, linkedin_lane = _domain(
        _role_payload(valid_drip_payload),
        now=now,
    )
    enrollment.lead.role_tag = role_tag
    enrollment.lead.save(update_fields={"role_tag"})
    linkedin_lane.status = DripLane.Status.ACTIVE
    linkedin_lane.current_theme_index = 0
    linkedin_lane.current_theme_key = "visibility_gap"
    linkedin_lane.theme_started_at = now - timedelta(days=10)
    linkedin_lane.save(
        update_fields={
            "status",
            "current_theme_index",
            "current_theme_key",
            "theme_started_at",
            "updated_at",
        },
    )
    return published, enrollment, gmail_lane, linkedin_lane


def _classification(lead_id):
    return Lead.objects.values("icp", "role_tag").get(pk=lead_id)


def test_manifest_accepts_role_in_linkedin_body_and_gmail_subject_and_body(
    valid_drip_payload,
):
    payload = _role_payload(valid_drip_payload)

    validated = validate_manifest(payload)

    first_sender = validated.normalized["audiences"]["CSPs"]["themes"][0][
        "senders"
    ]["Arian"]
    assert first_sender["linkedin"][0]["body"] == (
        "Hi {first_name}, I work with {role} at {company_name}."
    )
    assert first_sender["gmail"][0]["subject"] == "For {role} at {company_name}"
    assert first_sender["gmail"][0]["body"] == (
        "Hi {first_name}, I work with {role} at {company_name}."
    )
    assert validated.content_hash == validate_manifest(deepcopy(payload)).content_hash


@pytest.mark.parametrize(
    ("role_tag", "wording"),
    [*ROLE_WORDING.items(), ("", "companies")],
)
def test_reconciliation_renders_every_shared_role_without_reclassifying_lead(
    valid_drip_payload,
    role_tag,
    wording,
):
    now = timezone.now()
    _published, enrollment, gmail_lane, linkedin_lane = _both_lanes(
        valid_drip_payload,
        now=now,
        role_tag=role_tag,
    )
    before = _classification(enrollment.lead_id)
    lead_count = Lead.objects.count()

    reconcile_drips(apply=False, now=now)
    assert not DripDelivery.objects.exists()
    reconcile_drips(apply=True, now=now)

    expected_body = f"Hi Ada, I work with {wording} at Analytical Engines."
    for lane in (linkedin_lane, gmail_lane):
        delivery = lane.deliveries.get()
        assert delivery.frozen_body == expected_body
        assert "{role}" not in delivery.frozen_body
        assert delivery.current_task.status == Task.Status.PENDING
    assert linkedin_lane.deliveries.get().frozen_subject == ""
    assert gmail_lane.deliveries.get().frozen_subject == (
        f"For {wording} at Analytical Engines"
    )
    assert _classification(enrollment.lead_id) == before
    assert Lead.objects.count() == lead_count
    enrollment.refresh_from_db()
    assert enrollment.frozen_icp == "CSPs"


@pytest.mark.parametrize("role_tag", [None, "", "  \t\n"])
@pytest.mark.parametrize("channel", [DripLane.Channel.LINKEDIN, DripLane.Channel.GMAIL])
def test_missing_or_blank_role_falls_back_in_the_real_renderer(
    valid_drip_payload,
    role_tag,
    channel,
):
    _published, enrollment, gmail_lane, linkedin_lane = _domain(
        valid_drip_payload,
        now=timezone.now(),
    )
    lane = gmail_lane if channel == DripLane.Channel.GMAIL else linkedin_lane
    # Null/whitespace cannot be stored under the canonical-role DB constraint;
    # exercise the shared fallback on an in-memory lead without changing the DB.
    lane.enrollment.lead.role_tag = role_tag
    rendition = [{"body": "I work with {role}.", "subject": "For {role}"}]

    subject, body = _render_step(
        lane=lane,
        rendition=rendition,
        step_index=0,
        thread_subject="",
    )

    assert body == "I work with companies."
    assert subject == ("For companies" if channel == DripLane.Channel.GMAIL else "")
    assert _classification(enrollment.lead_id) == {"icp": "CSPs", "role_tag": ""}
    assert not DripDelivery.objects.exists()


@pytest.mark.parametrize("role_tag", ["CFO", "finance leaders", "Unknown role"])
@pytest.mark.parametrize("location", ["linkedin_body", "gmail_body", "gmail_subject"])
def test_unknown_role_fails_closed_only_when_the_selected_copy_uses_role(
    valid_drip_payload,
    role_tag,
    location,
):
    _published, enrollment, gmail_lane, linkedin_lane = _domain(
        valid_drip_payload,
        now=timezone.now(),
    )
    lane = linkedin_lane if location == "linkedin_body" else gmail_lane
    lane.enrollment.lead.role_tag = role_tag
    step = {"body": "Hello {first_name}.", "subject": "A question"}
    step["subject" if location == "gmail_subject" else "body"] = "For {role}."

    with pytest.raises(MessageRoleError, match="No message wording for Role Tag"):
        _render_step(lane=lane, rendition=[step], step_index=0, thread_subject="")

    assert _classification(enrollment.lead_id) == {"icp": "CSPs", "role_tag": ""}
    assert not DripDelivery.objects.exists()


@pytest.mark.parametrize("channel", [DripLane.Channel.LINKEDIN, DripLane.Channel.GMAIL])
def test_role_free_templates_do_not_resolve_an_unknown_role(
    valid_drip_payload,
    channel,
):
    _published, _enrollment, gmail_lane, linkedin_lane = _domain(
        valid_drip_payload,
        now=timezone.now(),
    )
    lane = gmail_lane if channel == DripLane.Channel.GMAIL else linkedin_lane
    lane.enrollment.lead.role_tag = "Unknown role"

    subject, body = _render_step(
        lane=lane,
        rendition=[{"body": "Hello {first_name}.", "subject": "A question"}],
        step_index=0,
        thread_subject="",
    )

    assert body == "Hello Ada."
    assert subject == ("A question" if channel == DripLane.Channel.GMAIL else "")


def test_unknown_role_aborts_materialization_before_any_delivery_or_task(
    valid_drip_payload,
    monkeypatch,
):
    now = timezone.now()
    _published, enrollment, _gmail, _linkedin = _both_lanes(
        valid_drip_payload,
        now=now,
        role_tag="CFO/Finance",
    )
    original_render_step = reconciliation._render_step

    def _render_invalid_hydrated_role(**kwargs):
        # The DB refuses invalid tags, so inject a corrupt hydrated value at
        # the renderer boundary and still execute the real materialization path.
        kwargs["lane"].enrollment.lead.role_tag = "Unknown role"
        return original_render_step(**kwargs)

    monkeypatch.setattr(reconciliation, "_render_step", _render_invalid_hydrated_role)
    task_count = Task.objects.count()

    with pytest.raises(MessageRoleError, match="No message wording for Role Tag"):
        reconcile_drips(apply=True, now=now)

    assert not DripDelivery.objects.exists()
    assert Task.objects.count() == task_count
    assert _classification(enrollment.lead_id) == {
        "icp": "CSPs",
        "role_tag": "CFO/Finance",
    }


@pytest.mark.parametrize("terminal_status", [Task.Status.COMPLETED, Task.Status.FAILED])
def test_frozen_role_copy_survives_role_edits_and_terminal_task_reconciliation(
    valid_drip_payload,
    terminal_status,
):
    now = timezone.now()
    _published, enrollment, gmail_lane, linkedin_lane = _both_lanes(
        valid_drip_payload,
        now=now,
        role_tag="CFO/Finance",
    )
    reconcile_drips(apply=True, now=now)
    frozen = {
        delivery.pk: (
            delivery.frozen_subject,
            delivery.frozen_body,
            delivery.scheduled_at,
            delivery.provider_account,
        )
        for delivery in DripDelivery.objects.all()
    }
    original_task_ids = set(
        DripDelivery.objects.values_list("current_task_id", flat=True),
    )
    enrollment.lead.role_tag = "Founder/CEO"
    enrollment.lead.save(update_fields={"role_tag"})

    # Neither an ordinary poll nor replacement of a terminal pre-send Task may
    # render again. The existing delivery, not today's role, owns this copy.
    reconcile_drips(apply=True, now=now)
    for task in Task.objects.filter(pk__in=original_task_ids):
        task.status = terminal_status
        task.completed_at = now
        task.save(update_fields={"status", "completed_at"})
    result = reconcile_drips(apply=True, now=now)
    reconcile_drips(apply=True, now=now)

    assert result.counts["rematerialized_task"] == 2
    assert gmail_lane.deliveries.count() == linkedin_lane.deliveries.count() == 1
    for delivery in DripDelivery.objects.all():
        assert (
            delivery.frozen_subject,
            delivery.frozen_body,
            delivery.scheduled_at,
            delivery.provider_account,
        ) == frozen[delivery.pk]
        assert "finance leaders" in delivery.frozen_body
        assert delivery.current_task_id not in original_task_ids
        assert delivery.current_task.status == Task.Status.PENDING
    assert Task.objects.filter(
        task_type__in=[Task.TaskType.DRIP_LINKEDIN, Task.TaskType.DRIP_GMAIL],
    ).count() == 4
    assert _classification(enrollment.lead_id) == {
        "icp": "CSPs",
        "role_tag": "Founder/CEO",
    }

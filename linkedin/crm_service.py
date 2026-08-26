"""Canonical CRM bootstrap and action reconciliation.

The service owns database decisions only.  It does not read or write Google
Sheets; the refresh command imports human edits first, calls these functions,
then hands the resulting records to the publishing adapter.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from linkedin.conf import ACTIVE_TIMEZONE
from linkedin.crm_action_policy import (
    DAILY_MAX_AGE_DAYS,
    MEETING_PREP_DAYS,
    RECOVERY_MAX_AGE_DAYS,
    ActionPlacement,
    OpportunityActionFacts,
    place_action,
)
from linkedin.operators import (
    CANONICAL_OPERATOR_HANDLES,
    resolve_sales_owner_handle,
)


DEFAULT_SALES_OWNERS = (
    ("Arian", "Arian"),
    ("Athena", "Athena"),
    ("Chuka", "Chuka"),
    ("Leili", "Leili"),
)

_SENTINEL_ACCOUNTS = frozenset({
    "",
    "none",
    "n a",
    "na",
    "unknown",
    "self employed",
    "independent",
    "confidential",
})

POLITE_DECLINE_PHRASES = (
    "not interested",
    "not a fit",
    "not the right time",
    "timing is not right",
    "no opportunity",
    "unable to participate",
    "do not contact",
    "don't contact",
    "please remove me",
    "unsubscribe",
)


@dataclass
class BootstrapReport:
    candidates: int = 0
    explicit_candidates: int = 0
    explicit_missing_or_disqualified: int = 0
    accounts_created: int = 0
    opportunities_created: int = 0
    opportunities_existing: int = 0
    contacts_linked: int = 0
    meetings_linked: int = 0
    notes_linked: int = 0
    owners_assigned: int = 0
    unowned: int = 0
    skipped_missing_account: int = 0
    ambiguous_accounts: list[dict] = field(default_factory=list)
    ambiguous_owners: list[dict] = field(default_factory=list)

    def counts(self) -> dict:
        payload = asdict(self)
        payload["ambiguous_accounts"] = len(self.ambiguous_accounts)
        payload["ambiguous_owners"] = len(self.ambiguous_owners)
        return payload


@dataclass(frozen=True)
class ActionEvaluation:
    opportunity_id: str
    account: str
    owner: str
    action_id: str
    target_lead_id: int | None
    action_kind: str
    action_description: str
    placement: ActionPlacement
    context_source: str
    context_note_id: str


@dataclass
class ActionRefreshReport:
    evaluated: int = 0
    actions_created: int = 0
    actions_completed: int = 0
    actions_superseded: int = 0
    actions_targeted: int = 0
    activity_updated: int = 0
    daily_by_owner: dict[str, int] = field(default_factory=dict)
    daily_by_category: dict[str, int] = field(default_factory=dict)
    daily_by_reason: dict[str, int] = field(default_factory=dict)
    surface_counts: dict[str, int] = field(default_factory=dict)
    exclusion_reasons: dict[str, int] = field(default_factory=dict)
    unowned_daily: int = 0
    evaluations: list[ActionEvaluation] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "actions_created": self.actions_created,
            "actions_completed": self.actions_completed,
            "actions_superseded": self.actions_superseded,
            "actions_targeted": self.actions_targeted,
            "activity_updated": self.activity_updated,
            "daily_by_owner": dict(self.daily_by_owner),
            "daily_by_category": dict(self.daily_by_category),
            "daily_by_reason": dict(self.daily_by_reason),
            "surface_counts": dict(self.surface_counts),
            "exclusion_reasons": dict(self.exclusion_reasons),
            "unowned_daily": self.unowned_daily,
        }


def ensure_sales_owners(*, apply: bool) -> dict[str, object]:
    """Ensure the four strict sender identities exist without creating typos."""
    from crm.models import SalesOwner

    existing = {
        owner.handle: owner
        for owner in SalesOwner.objects.filter(handle__in=CANONICAL_OPERATOR_HANDLES)
    }
    missing = [handle for handle, _display in DEFAULT_SALES_OWNERS if handle not in existing]
    if apply:
        for handle, display_name in DEFAULT_SALES_OWNERS:
            owner, _created = SalesOwner.objects.get_or_create(
                handle=handle,
                defaults={"display_name": display_name, "active": True},
            )
            existing[handle] = owner
    return {"existing": existing, "missing": missing}


def bootstrap_opportunities(
    *,
    apply: bool,
    now: datetime | None = None,
    explicit_lead_ids: Iterable[int] = (),
) -> BootstrapReport:
    """Conservatively bootstrap engaged account opportunities.

    A real inbound message, real meeting, or caller-validated explicit People
    signal qualifies a Lead.  Explicit signals are stable numeric Lead IDs;
    this layer never accepts Name/company matching and deliberately derives a
    conservative Discovery/Evaluation stage rather than trusting legacy Deal
    COMPLETED or a visual Sheet position as Closed Won.  Blank or sentinel
    company labels are skipped.  Existing duplicate Account identity keys and
    multiple possible owners are reported, never guessed through.
    """
    from crm.models import Account, Lead, Meeting, Message, Opportunity, OpportunityContact

    current_time = now or timezone.now()
    owner_state = ensure_sales_owners(apply=apply)
    owners = owner_state["existing"]
    report = BootstrapReport()
    explicit_ids = {int(lead_id) for lead_id in explicit_lead_ids}
    eligible_explicit_ids = set(
        Lead.objects.filter(
            id__in=explicit_ids,
            disqualified=False,
        ).values_list("id", flat=True)
    )
    report.explicit_candidates = len(eligible_explicit_ids)
    report.explicit_missing_or_disqualified = len(
        explicit_ids - eligible_explicit_ids
    )

    engaged_leads = list(
        Lead.objects.filter(disqualified=False)
        .filter(
            Q(messages__direction=Message.Direction.INBOUND)
            | Q(meetings__isnull=False)
            | Q(meeting_participations__isnull=False)
            | Q(id__in=eligible_explicit_ids)
        )
        .distinct()
        .order_by("id")
    )
    engaged_keys: set[str] = set()
    grouped: dict[str, list] = defaultdict(list)
    display_name_by_key: dict[str, str] = {}
    from crm.models.sales import normalize_account_name

    for lead in engaged_leads:
        key = normalize_account_name(lead.company_name)
        if key in _SENTINEL_ACCOUNTS:
            report.skipped_missing_account += 1
            continue
        engaged_keys.add(key)
        display_name_by_key.setdefault(key, (lead.company_name or "").strip())

    # Once an account qualifies, link every exact-company CRM contact to the
    # same opportunity.  This gives one sale multiple stakeholders without
    # turning unrelated historical People rows into opportunities.
    for lead in Lead.objects.filter(disqualified=False).order_by("id"):
        key = normalize_account_name(lead.company_name)
        if key in engaged_keys:
            grouped[key].append(lead)
            display_name_by_key.setdefault(key, (lead.company_name or "").strip())

    report.candidates = len(grouped)
    for account_key, account_leads in grouped.items():
        matches = list(Account.objects.filter(normalized_name=account_key).order_by("id"))
        if len(matches) > 1:
            report.ambiguous_accounts.append({
                "normalized_account": account_key,
                "account_ids": [str(account.id) for account in matches],
                "lead_ids": [lead.id for lead in account_leads],
            })
            continue

        account = matches[0] if matches else None
        if account is None and apply:
            account = Account.objects.create(name=display_name_by_key[account_key])
            report.accounts_created += 1
        elif account is None:
            report.accounts_created += 1

        opportunity = None
        if account is not None:
            opportunity = Opportunity.objects.filter(
                account=account,
                motion_key="primary",
            ).first()

        latest_activity = _latest_meaningful_activity(
            [lead.id for lead in account_leads],
            now=current_time,
        )
        owner_handles = _owner_evidence(account_leads)
        owner = owners.get(next(iter(owner_handles))) if len(owner_handles) == 1 else None

        created = opportunity is None
        if created:
            stage, step = _bootstrap_stage(account_leads, now=current_time)
            if apply:
                assert account is not None
                opportunity = Opportunity(
                    account=account,
                    motion_key="primary",
                    name=display_name_by_key[account_key],
                    owner=owner,
                    stage=stage,
                    sales_motion_step=step,
                    last_meaningful_activity_at=latest_activity,
                    source=Opportunity.Source.BOOTSTRAP,
                )
                opportunity._stage_event_source = Opportunity.Source.BOOTSTRAP
                opportunity.save()
            report.opportunities_created += 1
            if owner is not None:
                report.owners_assigned += 1
        else:
            report.opportunities_existing += 1

        # Exact evidence may fill an unowned canonical opportunity.  An
        # explicit owner is human-owned and is never replaced by inferred
        # sender history, even when that history now points elsewhere.
        if (
            opportunity is not None
            and opportunity.owner_id is None
            and owner is not None
        ):
            report.owners_assigned += 1
            if apply:
                opportunity.owner = owner
                opportunity.save(update_fields={"owner", "updated_at"})

        if len(owner_handles) > 1:
            report.ambiguous_owners.append({
                "normalized_account": account_key,
                "candidate_owners": sorted(owner_handles),
                "lead_ids": [lead.id for lead in account_leads],
            })
        if (opportunity is None or opportunity.owner_id is None) and owner is None:
            report.unowned += 1

        if apply and opportunity is not None:
            if (
                latest_activity is not None
                and (
                    opportunity.last_meaningful_activity_at is None
                    or latest_activity > opportunity.last_meaningful_activity_at
                )
            ):
                opportunity.last_meaningful_activity_at = latest_activity
                opportunity.save(update_fields={"last_meaningful_activity_at", "updated_at"})
            for lead in account_leads:
                _link_contact(opportunity, lead, report=report)
            _link_meeting_context(
                opportunity,
                [lead.id for lead in account_leads],
                report=report,
            )
        else:
            report.contacts_linked += len(account_leads)

    return report


def recalculate_actions(
    *,
    apply: bool,
    now: datetime | None = None,
    dont_send_lead_ids: Iterable[int] = (),
    granola_available: bool = True,
) -> ActionRefreshReport:
    """Refresh canonical activity and deterministically place current actions."""
    from crm.models import Opportunity, OpportunityAction

    current_time = now or timezone.now()
    today = _business_date(current_time)
    dont_send_ids = set(dont_send_lead_ids)
    report = ActionRefreshReport()
    surface_counts: dict[str, int] = defaultdict(int)
    daily_by_owner: dict[str, int] = defaultdict(int)
    daily_by_category: dict[str, int] = defaultdict(int)
    daily_by_reason: dict[str, int] = defaultdict(int)
    exclusion_reasons: dict[str, int] = defaultdict(int)

    opportunities = (
        Opportunity.objects.select_related("account", "owner")
        .prefetch_related(
            "contacts__lead",
            "actions__target_lead",
            "meetings__notes",
        )
        .order_by("id")
    )
    for opportunity in opportunities:
        report.evaluated += 1
        snapshot = _opportunity_snapshot(opportunity, now=current_time)
        observed_activity = snapshot["latest_activity"]
        latest_activity = max(
            (
                value
                for value in (
                    opportunity.last_meaningful_activity_at,
                    observed_activity,
                )
                if value is not None
            ),
            default=None,
        )
        snapshot["latest_activity"] = latest_activity
        if (
            latest_activity is not None
            and (
                opportunity.last_meaningful_activity_at is None
                or latest_activity > opportunity.last_meaningful_activity_at
            )
        ):
            report.activity_updated += 1
            opportunity.last_meaningful_activity_at = latest_activity
            if apply:
                opportunity.save(update_fields={"last_meaningful_activity_at", "updated_at"})

        current_action = snapshot["current_action"]
        target_lead = snapshot["current_action_target_lead"]
        proposal_suppression = ""
        if (
            current_action is not None
            and current_action.target_lead_id is None
            and target_lead is not None
        ):
            report.actions_targeted += 1
            if apply:
                current_action.target_lead = target_lead
                current_action.save(update_fields={"target_lead", "updated_at"})

        # Suppress only replaceable system actions.  Human-created or
        # human-revised actions remain durable on Opportunities and are merely
        # excluded from derived work surfaces when no longer actionable.
        current_suppression = _current_action_suppression(
            opportunity,
            current_action=current_action,
            target_lead=target_lead,
            snapshot=snapshot,
            dont_send_ids=dont_send_ids,
        )
        if (
            current_action is not None
            and current_suppression
            and _is_replaceable_system_action(current_action)
        ):
            report.actions_superseded += 1
            if apply:
                _cancel_system_action(current_action)
            current_action = None
            target_lead = None

        if (
            current_action is not None
            and _has_fresh_inbound(snapshot, now=current_time)
            and current_action.kind != OpportunityAction.Kind.NEEDS_RESPONSE
            and _is_replaceable_system_action(current_action)
        ):
            report.actions_superseded += 1
            if apply:
                _cancel_system_action(current_action)
            current_action = None
            target_lead = None

        completion_reason = _automatic_completion_reason(
            current_action,
            snapshot=snapshot,
            now=current_time,
        )
        if completion_reason:
            report.actions_completed += 1
            if apply:
                _complete_system_action(
                    current_action,
                    reason=completion_reason,
                    snapshot=snapshot,
                    now=current_time,
                )
            current_action = None
            target_lead = None
        proposed = None
        if current_action is None and not _globally_suppressed(
            opportunity,
            snapshot=snapshot,
        ):
            proposed = _proposed_action(opportunity, snapshot=snapshot, now=current_time)
            if proposed is not None:
                proposal_suppression = _target_suppression(
                    proposed.get("target_lead"),
                    contact_ids=set(snapshot["contact_ids"]),
                    dont_send_ids=dont_send_ids,
                )
                if proposal_suppression:
                    proposed = None
            if proposed is not None:
                if apply:
                    current_action, created = _create_action(opportunity, proposed)
                    report.actions_created += int(created)
                    if current_action is not None:
                        target_lead = current_action.target_lead
                    else:
                        # A completed/cancelled row with the same idempotency
                        # key is authoritative.  Do not publish a phantom
                        # dry-run action or recreate completed busywork.
                        proposed = None
                else:
                    idempotency_key = _action_idempotency_key(opportunity, proposed)
                    existing = OpportunityAction.objects.filter(
                        opportunity=opportunity,
                        idempotency_key=idempotency_key,
                    ).first()
                    if existing is None:
                        report.actions_created += 1
                        target_lead = proposed.get("target_lead")
                    elif existing.status in {
                        OpportunityAction.Status.OPEN,
                        OpportunityAction.Status.WAITING,
                    }:
                        current_action = existing
                        target_lead = _stable_action_target(
                            existing,
                            contact_links=snapshot["contact_links"],
                            contact_by_id=snapshot["contact_by_id"],
                        )
                    else:
                        proposed = None

        action_status = current_action.status if current_action is not None else (
            "open" if proposed is not None else ""
        )
        action_kind = current_action.kind if current_action is not None else (
            proposed["kind"] if proposed is not None else ""
        )
        due_on = current_action.due_on if current_action is not None else None
        waiting_until = current_action.waiting_until if current_action is not None else None
        description = current_action.description if current_action is not None else (
            proposed["description"] if proposed is not None else ""
        )

        contact_ids = set(snapshot["contact_ids"])
        target_lead_id = target_lead.id if target_lead is not None else None
        explicitly_managed_action = bool(
            current_action is not None
            and (
                current_action.human_revision > 0
                or not current_action.idempotency_key.startswith("system:")
            )
        )
        action_trigger_meeting = (
            current_action.trigger_meeting
            if current_action is not None
            and current_action.kind == OpportunityAction.Kind.MEETING_PREP
            and current_action.trigger_meeting_id is not None
            else proposed.get("trigger_meeting")
            if proposed is not None
            and proposed["kind"] == OpportunityAction.Kind.MEETING_PREP
            else None
        )
        has_routable_action = bool(
            target_lead is not None and (current_action is not None or proposed is not None)
        )
        routing_conflict = bool(
            current_action is not None
            and not _is_replaceable_system_action(current_action)
            and target_lead is not None
            and _has_fresh_inbound(snapshot, now=current_time)
            and snapshot["latest_inbound"].lead_id != target_lead.id
        )
        facts = OpportunityActionFacts(
            stage=opportunity.stage,
            last_meaningful_activity_on=(
                _business_date(latest_activity)
                if latest_activity is not None
                else None
            ),
            action_status=action_status,
            action_kind=action_kind,
            due_on=due_on,
            waiting_until=waiting_until,
            needs_response=snapshot["needs_response"],
            fresh_trigger_on=(
                _business_date(snapshot["latest_inbound"].sent_at)
                if snapshot["latest_inbound"] is not None
                else None
            ),
            upcoming_meeting_on=(
                _business_date(action_trigger_meeting.start_at)
                if action_trigger_meeting is not None
                else _business_date(snapshot["upcoming_meeting"].start_at)
                if proposed is not None and snapshot["upcoming_meeting"] is not None
                else None
            ),
            unresolved_post_meeting=(
                action_kind == "post_meeting_commitment"
            ),
            missing_next_action=(
                (
                    proposed is not None
                    and proposed["reason"] == "missing_next_action"
                )
                or (
                    current_action is not None
                    and current_action.kind == OpportunityAction.Kind.NEXT_STEP
                    and current_action.description == "Define and schedule the next action"
                )
            ),
            explicit_current_action=explicitly_managed_action,
            manual_pin=opportunity.manual_pin and has_routable_action,
            lead_disqualified=(
                bool(target_lead.disqualified)
                if target_lead is not None
                else (
                    snapshot["all_contacts_disqualified"]
                    or proposal_suppression == "target_disqualified"
                )
            ),
            dont_send=(
                target_lead_id in dont_send_ids
                if target_lead_id is not None
                else (
                    bool(contact_ids & dont_send_ids)
                    or proposal_suppression == "target_do_not_contact"
                )
            ),
            failed_non_actionable=snapshot["failed_non_actionable"],
            polite_decline=snapshot["polite_decline"],
            unroutable_action=bool(
                (current_action is not None and target_lead is None)
                or proposal_suppression == "missing_or_unlinked_target"
            ),
            routing_conflict=routing_conflict,
            manual_pin_unresolved=bool(
                opportunity.manual_pin
                and current_action is None
                and proposed is None
                and target_lead is None
            ),
            meeting_prep_has_real_meeting=action_trigger_meeting is not None,
        )
        placement = place_action(facts, today=today)
        surface_counts[placement.surface] += 1
        if placement.surface == "excluded":
            exclusion_reasons[placement.reason] += 1
        owner_handle = opportunity.owner.handle if opportunity.owner_id else ""
        if placement.surface == "daily":
            daily_by_category[placement.category] += 1
            daily_by_reason[placement.reason] += 1
            if owner_handle:
                daily_by_owner[owner_handle] += 1
            else:
                report.unowned_daily += 1

        context = _preferred_meeting_context(
            snapshot,
            opportunity=opportunity,
            granola_available=granola_available,
        )
        report.evaluations.append(ActionEvaluation(
            opportunity_id=str(opportunity.id),
            account=opportunity.account.name,
            owner=owner_handle,
            action_id=str(current_action.id) if current_action is not None else "",
            target_lead_id=target_lead_id,
            action_kind=action_kind,
            action_description=description,
            placement=placement,
            context_source=context["source"],
            context_note_id=context["note_id"],
        ))

    report.surface_counts = dict(surface_counts)
    report.daily_by_owner = dict(daily_by_owner)
    report.daily_by_category = dict(daily_by_category)
    report.daily_by_reason = dict(daily_by_reason)
    report.exclusion_reasons = dict(exclusion_reasons)
    return report


def _link_contact(opportunity, lead, *, report: BootstrapReport) -> None:
    from crm.models import OpportunityContact

    _contact, created = OpportunityContact.objects.get_or_create(
        opportunity=opportunity,
        lead=lead,
        role=OpportunityContact.Role.STAKEHOLDER,
    )
    if created:
        report.contacts_linked += 1


def _link_meeting_context(
    opportunity,
    lead_ids: list[int],
    *,
    report: BootstrapReport,
) -> None:
    from crm.models import Meeting, MeetingNote

    meetings = Meeting.objects.filter(
        Q(lead_id__in=lead_ids) | Q(participants__id__in=lead_ids),
        opportunity__isnull=True,
    ).distinct()
    meeting_ids = list(meetings.values_list("id", flat=True))
    if meeting_ids:
        report.meetings_linked += Meeting.objects.filter(
            id__in=meeting_ids,
            opportunity__isnull=True,
        ).update(opportunity=opportunity)
        report.notes_linked += MeetingNote.objects.filter(
            meeting_id__in=meeting_ids,
            opportunity__isnull=True,
        ).update(opportunity=opportunity)


def _bootstrap_stage(leads: list, *, now: datetime) -> tuple[str, int]:
    from crm.models import Meeting, Opportunity

    lead_ids = [lead.id for lead in leads]
    if Meeting.objects.filter(
        Q(lead_id__in=lead_ids) | Q(participants__id__in=lead_ids),
        start_at__lte=now,
    ).exists():
        return Opportunity.Stage.EVALUATION, 5
    return Opportunity.Stage.DISCOVERY, 2


def _latest_meaningful_activity(lead_ids: list[int], *, now: datetime):
    from crm.models import Meeting, Message

    message = Message.objects.filter(lead_id__in=lead_ids, sent_at__lte=now).order_by(
        "-sent_at",
    ).first()
    meeting = Meeting.objects.filter(
        Q(lead_id__in=lead_ids) | Q(participants__id__in=lead_ids),
        start_at__lte=now,
    ).distinct().order_by("-start_at").first()
    values = [
        value
        for value in (
            message.sent_at if message is not None else None,
            meeting.start_at if meeting is not None else None,
        )
        if value is not None
    ]
    return max(values) if values else None


def _owner_evidence(leads: list) -> set[str]:
    from crm.models import Message

    lead_ids = [lead.id for lead in leads]
    handles: set[str] = set()
    messages = Message.objects.filter(
        lead_id__in=lead_ids,
        direction=Message.Direction.OUTBOUND,
    ).select_related("operator")
    for message in messages:
        candidate = message.operator.handle if message.operator_id else message.sender
        if handle := resolve_sales_owner_handle(candidate):
            handles.add(handle)
    for lead in leads:
        for deal in lead.deal_set.all():
            if handle := resolve_sales_owner_handle(deal.invitation_sender):
                handles.add(handle)
    return handles


def _default_target_lead(contact_links: list):
    """Choose a contact only when the account relationship is unambiguous."""
    for role in ("champion", "decision_maker"):
        candidates = {
            link.lead_id: link.lead
            for link in contact_links
            if link.role == role
        }
        if len(candidates) == 1:
            return next(iter(candidates.values()))
    primary = {
        link.lead_id: link.lead
        for link in contact_links
        if link.is_primary
    }
    if len(primary) == 1:
        return next(iter(primary.values()))
    all_contacts = {link.lead_id: link.lead for link in contact_links}
    return next(iter(all_contacts.values())) if len(all_contacts) == 1 else None


def _stable_action_target(action, *, contact_links: list, contact_by_id: dict):
    if action is None:
        return None
    if action.target_lead_id is not None:
        # Persisted target is authoritative only while it is still a linked
        # Opportunity contact.  This also fails closed around legacy/bulk data
        # that predates model-level membership validation.
        return contact_by_id.get(action.target_lead_id)
    if action.trigger_message_id is not None:
        return contact_by_id.get(action.trigger_message.lead_id)
    if action.trigger_meeting_id is not None:
        return contact_by_id.get(action.trigger_meeting.lead_id)
    return _default_target_lead(contact_links)


def _meeting_target_lead(meeting, *, contact_by_id: dict, default_target):
    direct = contact_by_id.get(meeting.lead_id)
    if direct is not None:
        return direct
    participant_ids = {
        lead.id
        for lead in meeting.participants.all()
        if lead.id in contact_by_id
    }
    if len(participant_ids) == 1:
        return contact_by_id[next(iter(participant_ids))]
    return default_target


def _opportunity_snapshot(opportunity, *, now: datetime) -> dict:
    from crm.models import Deal, Meeting, Message, OpportunityAction
    from linkedin.enums import ProfileState

    contact_links = list(opportunity.contacts.all())
    contact_leads = [link.lead for link in contact_links]
    contact_by_id = {lead.id: lead for lead in contact_leads}
    contact_ids = [lead.id for lead in contact_leads]
    messages = list(
        Message.objects.filter(lead_id__in=contact_ids, sent_at__lte=now)
        .select_related("operator")
        .order_by("sent_at", "id")
    )
    meetings = list(
        Meeting.objects.filter(
            Q(opportunity=opportunity)
            | Q(lead_id__in=contact_ids)
            | Q(participants__id__in=contact_ids)
        )
        .distinct()
        .prefetch_related("notes", "participants")
        .order_by("start_at", "id")
    )
    messages_by_lead: dict[int, list] = defaultdict(list)
    for message in messages:
        messages_by_lead[message.lead_id].append(message)
    latest_inbound_by_lead = {
        lead_id: next(
            (
                message
                for message in reversed(lead_messages)
                if message.direction == Message.Direction.INBOUND
            ),
            None,
        )
        for lead_id, lead_messages in messages_by_lead.items()
    }
    latest_outbound_by_lead = {
        lead_id: next(
            (
                message
                for message in reversed(lead_messages)
                if message.direction == Message.Direction.OUTBOUND
            ),
            None,
        )
        for lead_id, lead_messages in messages_by_lead.items()
    }
    past_meetings = [meeting for meeting in meetings if meeting.start_at <= now]
    future_meetings = [meeting for meeting in meetings if meeting.start_at > now]
    latest_values = [message.sent_at for message in messages]
    latest_values.extend(meeting.start_at for meeting in past_meetings)
    latest_activity = max(latest_values) if latest_values else None
    all_actions = list(opportunity.actions.all())
    current_actions = [
        action
        for action in all_actions
        if action.status in {OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING}
    ]
    current_actions.sort(key=lambda action: (action.updated_at, action.created_at), reverse=True)
    current_action = current_actions[0] if current_actions else None
    completed_trigger_message_ids: set[int] = set()
    completed_by_target: dict[int, list] = defaultdict(list)
    for action in all_actions:
        completed_target = _stable_action_target(
            action,
            contact_links=contact_links,
            contact_by_id=contact_by_id,
        )
        if (
            action.status == OpportunityAction.Status.COMPLETED
            and action.trigger_message_id is not None
            and completed_target is not None
            and action.trigger_message.lead_id == completed_target.id
        ):
            completed_trigger_message_ids.add(action.trigger_message_id)
        if (
            action.status == OpportunityAction.Status.COMPLETED
            and completed_target is not None
            and action.completed_at is not None
            and action.disposition in {
                OpportunityAction.Disposition.SENT,
                OpportunityAction.Disposition.HANDLED,
            }
        ):
            completed_by_target[completed_target.id].append(action)

    def inbound_is_handled(inbound) -> bool:
        return bool(
            inbound.id in completed_trigger_message_ids
            or any(
                action.completed_at >= inbound.sent_at
                for action in completed_by_target.get(inbound.lead_id, ())
            )
        )

    unanswered_inbounds = []
    awaiting_reply_by_lead: dict[int, bool] = {}
    for lead_id in contact_ids:
        inbound = latest_inbound_by_lead.get(lead_id)
        outbound = latest_outbound_by_lead.get(lead_id)
        handled = bool(inbound is not None and inbound_is_handled(inbound))
        if (
            inbound is not None
            and not handled
            and (outbound is None or inbound.sent_at > outbound.sent_at)
        ):
            unanswered_inbounds.append(inbound)
        awaiting_reply_by_lead[lead_id] = bool(handled or (
            outbound is not None
            and (inbound is None or outbound.sent_at >= inbound.sent_at)
        ))

    latest_inbound = None
    if (
        current_action is not None
        and current_action.kind == OpportunityAction.Kind.NEEDS_RESPONSE
        and current_action.trigger_message_id is not None
    ):
        latest_inbound = next(
            (
                inbound
                for inbound in unanswered_inbounds
                if inbound.id == current_action.trigger_message_id
            ),
            None,
        )
    if latest_inbound is None and unanswered_inbounds:
        latest_inbound = max(
            unanswered_inbounds,
            key=lambda message: (message.sent_at, message.id),
        )

    current_target = _stable_action_target(
        current_action,
        contact_links=contact_links,
        contact_by_id=contact_by_id,
    )
    response_outbound = None
    if current_action is not None and current_action.kind == OpportunityAction.Kind.NEEDS_RESPONSE:
        response_lead_id = (
            current_target.id
            if current_target is not None
            else current_action.trigger_message.lead_id
            if current_action.trigger_message_id is not None
            else None
        )
        trigger_matches_target = bool(
            current_action.trigger_message_id is None
            or (
                current_target is not None
                and current_action.trigger_message.lead_id == current_target.id
            )
        )
        candidate = (
            latest_outbound_by_lead.get(response_lead_id)
            if trigger_matches_target
            else None
        )
        trigger_at = (
            current_action.trigger_message.sent_at
            if current_action.trigger_message_id is not None
            else current_action.created_at
        )
        if candidate is not None and candidate.sent_at >= trigger_at:
            response_outbound = candidate

    latest_inbound_body = (
        (latest_inbound.body or "").casefold()
        if latest_inbound is not None
        else ""
    )
    deals = Deal.objects.filter(lead_id__in=contact_ids)
    deal_lead_ids = set(deals.values_list("lead_id", flat=True))
    all_failed = bool(
        contact_ids
        and not messages
        and not meetings
        and deal_lead_ids == set(contact_ids)
        and not deals.exclude(state=ProfileState.FAILED).exists()
    )
    default_target = _default_target_lead(contact_links)
    upcoming_meeting = future_meetings[0] if future_meetings else None
    return {
        "contact_links": contact_links,
        "contact_by_id": contact_by_id,
        "contact_ids": contact_ids,
        "messages": messages,
        "meetings": meetings,
        "latest_inbound": latest_inbound,
        "needs_response": latest_inbound is not None,
        "awaiting_reply_by_lead": awaiting_reply_by_lead,
        "response_outbound": response_outbound,
        "latest_activity": latest_activity,
        "upcoming_meeting": upcoming_meeting,
        "upcoming_meeting_target_lead": (
            _meeting_target_lead(
                upcoming_meeting,
                contact_by_id=contact_by_id,
                default_target=default_target,
            )
            if upcoming_meeting is not None
            else None
        ),
        "latest_past_meeting": past_meetings[-1] if past_meetings else None,
        "current_action": current_action,
        "current_action_target_lead": current_target,
        "default_target_lead": default_target,
        "all_contacts_disqualified": bool(contact_leads) and all(
            lead.disqualified for lead in contact_leads
        ),
        "failed_non_actionable": all_failed,
        "polite_decline": bool(
            latest_inbound is not None
            and any(
                phrase in latest_inbound_body
                for phrase in POLITE_DECLINE_PHRASES
            )
        ),
    }


def _proposed_action(opportunity, *, snapshot: dict, now: datetime) -> dict | None:
    from crm.models import Opportunity, OpportunityAction

    latest_activity = snapshot["latest_activity"]
    inactivity_days = (
        (_business_date(now) - _business_date(latest_activity)).days
        if latest_activity is not None
        else None
    )
    if snapshot["polite_decline"]:
        return {
            "kind": OpportunityAction.Kind.RECOVERY_REVIEW,
            "description": "Review the prospect's decline and choose a disposition",
            "reason": "polite_decline",
            "trigger_message": snapshot["latest_inbound"],
            "target_lead": snapshot["latest_inbound"].lead,
        }
    if _has_fresh_inbound(snapshot, now=now):
        return {
            "kind": OpportunityAction.Kind.NEEDS_RESPONSE,
            "description": "Respond to the latest inbound message",
            "reason": "fresh_inbound",
            "trigger_message": snapshot["latest_inbound"],
            "target_lead": snapshot["latest_inbound"].lead,
        }
    upcoming = snapshot["upcoming_meeting"]
    if upcoming is not None:
        days_to_meeting = (
            _business_date(upcoming.start_at) - _business_date(now)
        ).days
        if 0 <= days_to_meeting <= MEETING_PREP_DAYS:
            return {
                "kind": OpportunityAction.Kind.MEETING_PREP,
                "description": f"Prepare for {upcoming.title or 'the upcoming meeting'}",
                "reason": "upcoming_meeting",
                "trigger_meeting": upcoming,
                "target_lead": snapshot["upcoming_meeting_target_lead"],
            }
    default_target = snapshot["default_target_lead"]
    if opportunity.manual_pin and default_target is not None:
        return {
            "kind": OpportunityAction.Kind.NEXT_STEP,
            "description": "Review and advance this manually pinned opportunity",
            "reason": "manual_pin",
            "target_lead": default_target,
        }
    if (
        inactivity_days is not None
        and inactivity_days <= DAILY_MAX_AGE_DAYS
        and default_target is not None
        and not snapshot["awaiting_reply_by_lead"].get(default_target.id, False)
        and opportunity.stage not in {
            Opportunity.Stage.PROSPECTING,
            Opportunity.Stage.CLOSED_WON,
            Opportunity.Stage.CLOSED_LOST,
        }
    ):
        return {
            "kind": OpportunityAction.Kind.NEXT_STEP,
            "description": "Define and schedule the next action",
            "reason": "missing_next_action",
            "target_lead": default_target,
        }
    if (
        inactivity_days is not None
        and DAILY_MAX_AGE_DAYS < inactivity_days <= RECOVERY_MAX_AGE_DAYS
        and default_target is not None
    ):
        return {
            "kind": OpportunityAction.Kind.RECOVERY_REVIEW,
            "description": "Review whether and how to re-engage this opportunity",
            "reason": "recovery_review",
            "target_lead": default_target,
        }
    return None


def _automatic_completion_reason(current_action, *, snapshot: dict, now: datetime) -> str:
    """Resolve only actions whose real-world completion is deterministic."""
    if current_action is None:
        return ""
    if (
        current_action.kind == current_action.Kind.NEEDS_RESPONSE
        and snapshot["response_outbound"] is not None
    ):
        return "response_sent"
    if (
        current_action.kind == current_action.Kind.MEETING_PREP
        and current_action.trigger_meeting_id
        and (
            current_action.trigger_meeting.end_at
            or current_action.trigger_meeting.start_at
        ) <= now
    ):
        return "meeting_started"
    return ""


def _complete_system_action(current_action, *, reason: str, snapshot: dict, now: datetime) -> None:
    from crm.models import OpportunityAction

    current_action.status = OpportunityAction.Status.COMPLETED
    current_action.completed_at = now
    current_action.handled_at = now
    update_fields = {
        "status",
        "completed_at",
        "handled_at",
        "updated_at",
    }
    if reason == "response_sent":
        current_action.disposition = OpportunityAction.Disposition.SENT
        current_action.sent_at = snapshot["response_outbound"].sent_at
        update_fields.update({"disposition", "sent_at"})
    else:
        current_action.disposition = OpportunityAction.Disposition.HANDLED
        update_fields.add("disposition")
    current_action.save(update_fields=update_fields)


def _action_idempotency_key(opportunity, proposed: dict) -> str:
    trigger_message = proposed.get("trigger_message")
    trigger_meeting = proposed.get("trigger_meeting")
    target_lead = proposed.get("target_lead")
    basis = (
        f"message:{trigger_message.id}"
        if trigger_message is not None
        else f"meeting:{trigger_meeting.id}"
        if trigger_meeting is not None
        else f"activity:{opportunity.last_meaningful_activity_at.isoformat()}"
        if opportunity.last_meaningful_activity_at is not None
        else f"opportunity:{opportunity.id}"
    )
    target_basis = f":target:{target_lead.id}" if target_lead is not None else ""
    return (
        f"system:{proposed['kind']}:{proposed['reason']}:{basis}{target_basis}"
    )[:255]


def _create_action(opportunity, proposed: dict):
    """Create a system action once and return ``(current_action, created)``.

    A previously completed/cancelled row with the same stable evidence remains
    authoritative and returns ``(None, False)``; it is never reopened as
    repetitive busywork.
    """
    from crm.models import Opportunity, OpportunityAction

    trigger_message = proposed.get("trigger_message")
    trigger_meeting = proposed.get("trigger_meeting")
    target_lead = proposed.get("target_lead")
    idempotency_key = _action_idempotency_key(opportunity, proposed)
    with transaction.atomic():
        Opportunity.objects.select_for_update().get(pk=opportunity.pk)
        current = OpportunityAction.objects.select_for_update().filter(
            opportunity=opportunity,
            status__in=[OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING],
        ).first()
        if current is not None:
            return current, False
        existing = OpportunityAction.objects.select_for_update().filter(
            opportunity=opportunity,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            if existing.status in {
                OpportunityAction.Status.OPEN,
                OpportunityAction.Status.WAITING,
            }:
                return existing, False
            return None, False
        action = OpportunityAction.objects.create(
            opportunity=opportunity,
            target_lead=target_lead,
            kind=proposed["kind"],
            description=proposed["description"],
            trigger_message=trigger_message,
            trigger_meeting=trigger_meeting,
            idempotency_key=idempotency_key,
        )
        return action, True


def _is_replaceable_system_action(action) -> bool:
    return bool(
        action is not None
        and action.human_revision == 0
        and action.idempotency_key.startswith("system:")
    )


def _cancel_system_action(action) -> None:
    action.status = action.Status.CANCELLED
    action.save(update_fields={"status", "updated_at"})


def _has_fresh_inbound(snapshot: dict, *, now: datetime) -> bool:
    inbound = snapshot["latest_inbound"]
    if inbound is None:
        return False
    age = (_business_date(now) - _business_date(inbound.sent_at)).days
    return 0 <= age <= DAILY_MAX_AGE_DAYS


def _globally_suppressed(opportunity, *, snapshot: dict) -> bool:
    from crm.models import Opportunity

    return bool(
        opportunity.stage in {
            Opportunity.Stage.CLOSED_WON,
            Opportunity.Stage.CLOSED_LOST,
        }
        or snapshot["failed_non_actionable"]
        or snapshot["all_contacts_disqualified"]
    )


def _target_suppression(
    target_lead,
    *,
    contact_ids: set[int],
    dont_send_ids: set[int],
) -> str:
    if target_lead is None or target_lead.id not in contact_ids:
        return "missing_or_unlinked_target"
    if target_lead.disqualified:
        return "target_disqualified"
    if target_lead.id in dont_send_ids:
        return "target_do_not_contact"
    return ""


def _current_action_suppression(
    opportunity,
    *,
    current_action,
    target_lead,
    snapshot: dict,
    dont_send_ids: set[int],
) -> str:
    if current_action is None:
        return ""
    if _globally_suppressed(opportunity, snapshot=snapshot):
        return "opportunity_non_actionable"
    target_suppression = _target_suppression(
        target_lead,
        contact_ids=set(snapshot["contact_ids"]),
        dont_send_ids=dont_send_ids,
    )
    if target_suppression:
        return target_suppression
    if (
        current_action.kind == current_action.Kind.MEETING_PREP
        and current_action.trigger_meeting_id is None
    ):
        return "meeting_prep_without_real_meeting"
    if (
        current_action.kind == current_action.Kind.NEEDS_RESPONSE
        and current_action.trigger_message_id is not None
        and (
            target_lead is None
            or current_action.trigger_message.lead_id != target_lead.id
        )
    ):
        return "response_trigger_target_conflict"
    return ""


def _preferred_meeting_context(
    snapshot: dict,
    *,
    opportunity,
    granola_available: bool,
) -> dict[str, str]:
    """Return matched Granola first, stored Gemini second, without eligibility."""
    from linkedin.granola_sync import resolve_meeting_context

    context = resolve_meeting_context(
        opportunity=opportunity,
        granola_available=granola_available,
    )
    if context is not None:
        return {"source": context.source, "note_id": context.external_id}
    return {"source": "", "note_id": ""}


def _business_date(value: datetime):
    return timezone.localtime(value, ZoneInfo(ACTIVE_TIMEZONE)).date()

"""Exercise real imported copy; fixture identities never leave the QA process.

Storage/CSV compatibility is tested separately from explicit-enrollment renders
so an import defect cannot hide the remaining message previews. No xfails hide
production defects. A nonzero result is an actionable QA failure.
"""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, transaction
from django.utils import timezone

from crm.models import Deal, Lead, Message
from gmail.auth import GMAIL_OPERATOR_MAPPING
from gmail.client import GmailSendResult
from gmail.handoff import maybe_schedule_gmail_sequence
from gmail.tasks.follow_up import handle_gmail_follow_up
from linkedin.general_icp_json import load_general_message_programs
from linkedin.icp_outbound import resolve_icp
from linkedin.message_delivery import (get_or_create_delivery,
    list_message_steps, render_message, MessageDeliveryError)
from linkedin.message_roles import ROLE_WORDING, role_wording
from linkedin.models import Campaign, CampaignMessageEnrollment, LinkedInProfile, Task, OutboundDelivery
from linkedin.enums import ProfileState
from linkedin.setup.seeds import create_seed_leads_from_csv, parse_csv_leads
from linkedin.tasks.connect import ConnectStrategy, enqueue_follow_up, handle_connect
from linkedin.tasks.follow_up import handle_follow_up
from linkedin.tasks.sweep_connections import process_accepted_deal
from tests.campaign_qa import reporting
from tests.campaign_qa.exceptions import QASafetyError

pytestmark = [pytest.mark.django_db,
              pytest.mark.skipif(not os.environ.get('CAMPAIGN_QA_RUN'), reason='Use the isolated campaign QA runner')]

DRAFTS = load_general_message_programs()
PROGRAM = next((draft for draft in DRAFTS if draft.key == 'fedramp-marketplace-csp'), None)
LABELS = {m.audience_key: m.icp_label for m in PROGRAM.messages} if PROGRAM else {}
AUDIENCES = sorted(LABELS)
OPERATORS = ('Arian', 'Chuka')
BASELINES = [(sender, key) for sender in OPERATORS for key in AUDIENCES]
REFERENCE_KEY = min(AUDIENCES, key=len) if AUDIENCES else ''
ROLE_KEY = next((m.audience_key for m in PROGRAM.messages if '{role}' in m.body), '') if PROGRAM else ''


def identify(request, sender, audience, scenario, expected):
    request.node.user_properties.extend([('sender', sender), ('icp', LABELS.get(audience, audience)),
                                         ('scenario', scenario), ('expected', expected)])


def preview(request, sender, audience, role, channel, step, subject, body, kind):
    reporting.PREVIEWS.append({'case': request.node.nodeid, 'sender': sender,
        'icp': LABELS[audience], 'role_tag': role, 'channel': channel, 'step': step,
        'subject': subject, 'body': body, 'kind': kind})


@pytest.fixture
def harness(monkeypatch, tmp_path):
    assert connection.vendor == 'postgresql'
    assert connection.settings_dict['HOST'] == os.environ['CAMPAIGN_QA_SOCKET']
    assert connection.settings_dict['NAME'] == 'test_campaign_qa'
    clock = SimpleNamespace(now=datetime(2026, 9, 8, 14, tzinfo=dt_timezone.utc))
    monkeypatch.setattr(timezone, 'now', lambda: clock.now)
    for name in ('linkedin.conf.ENABLE_FOLLOW_UP', 'linkedin.tasks.follow_up.ENABLE_FOLLOW_UP',
                 'linkedin.tasks.connect.ENABLE_CONNECT', 'gmail.handoff.ENABLE_GMAIL_SEQUENCE',
                 'gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE'):
        monkeypatch.setattr(name, True)
    monkeypatch.setattr('linkedin.conf.OUR_COMPANY_NAME', 'Boundera')
    monkeypatch.setattr('linkedin.conf.OUR_WEBSITE_URL', 'https://boundera.io')
    # Block only external read boundaries, not routing, stop checks, rendering,
    # persistence, or the normal enqueue/executor functions under test.
    monkeypatch.setattr('linkedin.tasks.sweep_connections.notify_connection_accepted', lambda **kwargs: None)
    monkeypatch.setattr('linkedin.tasks.sweep_connections.get_conversation', lambda *args: None)

    def forbid(*args, **kwargs):
        raise QASafetyError('Bound campaign attempted a browser or legacy message lookup')

    monkeypatch.setattr('linkedin.icp_outbound.load_icp_messages', forbid)
    monkeypatch.setattr('gmail.templates._load', forbid)
    sessions = {}

    def make(sender, audience, *, store_icp=True, role='Founder/CEO'):
        if sender not in sessions:
            user = User.objects.create(username=sender)
            LinkedInProfile.objects.filter(active=True).update(active=False)
            account = LinkedInProfile.objects.create(user=user, linkedin_username=sender,
                linkedin_password='unused-qa-placeholder', active=True)
            path = tmp_path / f'{sender}-campaign.json'
            path.write_text(json.dumps({'name': f'QA {sender}', 'message_program_key': PROGRAM.key}), encoding='utf-8')
            call_command('import_campaign', str(path), stdout=io.StringIO())
            campaign = Campaign.objects.get(name=f'QA {sender}')
            assert campaign.user_id == user.pk
            assert campaign.active_message_version.content_hash == PROGRAM.content_hash
            sessions[sender] = SimpleNamespace(django_user=user, linkedin_profile=account,
                campaign=campaign, handle=sender, ensure_browser=forbid)
        session = sessions[sender]
        number = Lead.objects.count() + 1
        pid = f'qa-{sender.lower()}-{number}'
        lead = Lead.objects.create(first_name='Eddy', last_name='QA', company_name='Example Cloud',
            linkedin_url=f'https://www.linkedin.com/in/{pid}/', public_identifier=pid,
            email=f'{pid}@example.invalid', icp=audience if store_icp else '', role_tag=role)
        deal = Deal.objects.create(lead=lead, campaign=session.campaign, state=ProfileState.QUALIFIED)
        return session, deal

    return SimpleNamespace(make=make, clock=clock, monkeypatch=monkeypatch)


def expected_body(message, sender, role='Founder/CEO'):
    display_name = sender
    if message.channel == 'gmail':
        display_name = GMAIL_OPERATOR_MAPPING[sender].get('display_name') or sender
    return message.body.format(first_name='Eddy', last_name='QA', company_name='Example Cloud',
        my_name=display_name, our_company_name='Boundera', our_website_url='https://boundera.io', role=role_wording(role))


def enrollment_fixture(deal, audience, sender):
    """Seed renderer prerequisites, not a substitute for enrollment QA.

    The separate full-sequence cases always use ensure_message_enrollment via
    production handlers. This fixture lets renderer results remain observable
    when campaign enrollment itself is broken.
    """
    enrollment = CampaignMessageEnrollment(deal=deal,
        message_version=deal.campaign.active_message_version, audience_key=audience, operator=sender)
    enrollment.full_clean()
    enrollment.save()
    return enrollment


@pytest.mark.parametrize('sender,audience', BASELINES)
def test_imported_render_matrix(harness, request, sender, audience):
    identify(request, sender, audience, 'fixture_render_and_freeze', 'With a fixture enrollment, every imported step renders exactly and freezes idempotently; campaign enrollment tested separately')
    session, deal = harness.make(sender, audience, store_icp=False)
    enrollment = enrollment_fixture(deal, audience, sender)
    rows = [m for m in PROGRAM.messages if m.audience_key == audience]
    assert len(list_message_steps(enrollment=enrollment)) == len(rows)
    for selected in list_message_steps(enrollment=enrollment):
        message = next(m for m in rows if m.channel == selected.channel and m.step_key == selected.step_key)
        delivery, created = get_or_create_delivery(enrollment=enrollment, channel=selected.channel,
            step_key=selected.step_key, reference_at=harness.clock.now)
        assert created
        expected = expected_body(message, sender)
        assert delivery.frozen_body == expected
        assert delivery.scheduled_at == harness.clock.now + timedelta(hours=float(message.delay_hours))
        assert '{' not in delivery.frozen_body and '}' not in delivery.frozen_body
        assert delivery.operator == sender and delivery.enrollment.audience_key == audience
        again, created = get_or_create_delivery(enrollment=enrollment, channel=selected.channel, step_key=selected.step_key)
        assert not created and again.pk == delivery.pk
        preview(request, sender, audience, deal.lead.role_tag, selected.channel, selected.step_key,
                delivery.frozen_subject, delivery.frozen_body, 'render_only')


@pytest.mark.parametrize('audience', AUDIENCES)
def test_exact_route_fits_real_database(harness, request, audience):
    identify(request, '', audience, 'lead_route_storage', 'The exact audience key survives PostgreSQL Lead.icp storage')
    request.node.user_properties.append(('actual', f'Audience key length={len(audience)}; Lead.icp max_length={Lead._meta.get_field("icp").max_length}'))
    with transaction.atomic():
        lead = Lead.objects.create(icp=audience)
        lead.refresh_from_db()
        assert resolve_icp(lead) == audience


@pytest.mark.parametrize('audience', AUDIENCES)
def test_csv_preserves_selected_audience(harness, request, audience):
    identify(request, '', audience, 'csv_audience_import', 'CSV ICP selection remains exact through parsing and seed persistence')
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(['Profile URL', 'First Name', 'Last Name', 'Company', 'ICP'])
    writer.writerow(['https://www.linkedin.com/in/qa-csv/', 'Eddy', 'QA', 'Example Cloud', audience])
    parsed = parse_csv_leads(stream.getvalue())
    assert parsed[0]['icp'] == audience, f'CSV importer returned {parsed[0]["icp"]!r} for {audience!r}'
    session, _ = harness.make('Arian', audience, store_icp=False)
    assert create_seed_leads_from_csv(session.campaign, parsed) == 1
    assert Lead.objects.get(public_identifier='qa-csv').icp == audience


def transports(harness, request, sender, audience, deal):
    calls = []

    def record(channel, step, body, subject=''):
        calls.append((channel, step, subject, body))
        preview(request, sender, audience, deal.lead.role_tag, channel, step, subject, body, 'simulated_provider')

    def connect(session, profile, note=''):
        record('linkedin_connect', 'connect', note)
        return ProfileState.PENDING

    def message(session, profile, body):
        delivery = OutboundDelivery.objects.get(enrollment__deal=deal,
            channel='linkedin_followup', status=OutboundDelivery.Status.SENDING)
        record('linkedin_followup', delivery.step_key, body)
        return True

    class GmailRecorder:
        def __init__(self, *, operator):
            assert operator == sender
            mapping = GMAIL_OPERATOR_MAPPING[operator]
            self.account_key = mapping['gmail_account']
            self.send_as = mapping['send_as']
            self.reply_to = mapping['reply_to']

        def send_message(self, **kwargs):
            assert kwargs.get('to') == deal.lead.email
            callback = kwargs.get('on_submit_attempt')
            if callback:
                callback()
            delivery = OutboundDelivery.objects.get(enrollment__deal=deal,
                channel='gmail', status=OutboundDelivery.Status.SENDING)
            record('gmail', delivery.step_key, kwargs['body'], kwargs['subject'])
            return GmailSendResult(message_id=f'qa-message-{len(calls)}',
                thread_id=kwargs.get('thread_id') or f'qa-thread-{deal.pk}',
                rfc_message_id=kwargs['rfc_message_id'])

    harness.monkeypatch.setattr('linkedin.actions.connect.send_connection_request', connect)
    # Keep the real send_raw_message wrapper and its normal message persistence.
    harness.monkeypatch.setattr('linkedin.actions.message._send_msg_pop_up', message)
    harness.monkeypatch.setattr('gmail.tasks.follow_up.GmailClient', GmailRecorder)
    return calls


def run_connect(harness, session, deal, status=ProfileState.QUALIFIED):
    profile = {'first_name': deal.lead.first_name, 'last_name': deal.lead.last_name,
        'public_identifier': deal.lead.public_identifier, 'url': deal.lead.linkedin_url,
        'headline': 'QA contact', 'positions': [{'company_name': deal.lead.company_name}]}
    candidate = {'public_identifier': deal.lead.public_identifier, 'url': deal.lead.linkedin_url,
                 'profile': profile, 'lead_id': deal.lead_id}
    strategy = ConnectStrategy(find_candidate=lambda _: candidate, pre_connect=None,
        delay=0, action_fraction=1.0, qualifier=SimpleNamespace(explain=lambda *args: 'QA'))
    harness.monkeypatch.setattr('linkedin.tasks.connect.strategy_for', lambda *args: strategy)
    harness.monkeypatch.setattr('linkedin.actions.status.get_connection_status', lambda *args: status)
    task = Task.objects.create(task_type=Task.TaskType.CONNECT, status=Task.Status.RUNNING,
        scheduled_at=harness.clock.now, started_at=harness.clock.now, payload={'campaign_id': session.campaign.pk})
    handle_connect(task, session, {})
    task.status = Task.Status.COMPLETED
    task.save(update_fields=['status'])


def seed_observed_thread(deal, sender):
    # Simulate the connection note returned by the post-accept inbox read. The
    # real invitation action stores sent_note, not a crm.Message by itself.
    deal.refresh_from_db()
    Message.objects.create(lead=deal.lead, source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND, sender=sender,
        external_id=f'qa-observed-note-{deal.pk}', body=deal.sent_note or 'Existing internal QA thread',
        sent_at=timezone.now())


def run_due(harness, session, deal, limit=8):
    executed = 0
    while executed < limit:
        task = Task.objects.filter(status=Task.Status.PENDING,
            task_type__in=[Task.TaskType.FOLLOW_UP, Task.TaskType.GMAIL_FOLLOW_UP]).order_by('scheduled_at', 'pk').first()
        if task is None:
            return
        harness.clock.now = max(harness.clock.now, task.scheduled_at)
        task.status, task.started_at = Task.Status.RUNNING, harness.clock.now
        task.save(update_fields=['status', 'started_at'])
        if task.task_type == Task.TaskType.FOLLOW_UP:
            handle_follow_up(task, session, {})
        else:
            handle_gmail_follow_up(task)
        task.refresh_from_db()
        if task.status == Task.Status.RUNNING:
            task.status = Task.Status.COMPLETED
            task.save(update_fields=['status'])
        executed += 1
    assert not Task.objects.filter(status=Task.Status.PENDING,
        task_type__in=[Task.TaskType.FOLLOW_UP, Task.TaskType.GMAIL_FOLLOW_UP]).exists(), 'Sequence did not terminate within the QA task bound'


@pytest.mark.parametrize('sender,audience', BASELINES)
def test_imported_campaign_sequence(harness, request, sender, audience):
    messages = [m for m in PROGRAM.messages if m.audience_key == audience]
    followups = [m for m in messages if m.channel == 'linkedin_followup']
    held = bool(followups and not any(m.step_index == 0 for m in followups))
    identify(request, sender, audience,
        'connection_with_incomplete_followup_held' if held else 'new_connection_to_full_sequence',
        'Connection only; incomplete LinkedIn follow-ups must stay held, not skip ahead' if held
        else 'Exact JSON copy reaches the recorder once per configured step, in order')
    session, deal = harness.make(sender, audience)
    calls = transports(harness, request, sender, audience, deal)
    run_connect(harness, session, deal)
    deal.refresh_from_db()
    assert deal.state == ProfileState.PENDING
    assert deal.invitation_sender == sender
    seed_observed_thread(deal, sender)
    process_accepted_deal(session, deal)
    run_due(harness, session, deal)
    if held:
        assert not OutboundDelivery.objects.filter(enrollment__deal=deal, channel='linkedin_followup').exists()
        messages = [m for m in messages if m.channel != 'linkedin_followup']
    assert len(calls) == len(messages), f'Expected {len(messages)} messages; recorder saw {[(c[0], c[1]) for c in calls]}'
    for channel in ('linkedin_connect', 'linkedin_followup', 'gmail'):
        selected = sorted((m for m in messages if m.channel == channel), key=lambda m: m.step_index)
        actual = [c for c in calls if c[0] == channel]
        assert [c[1] for c in actual] == [m.step_key for m in selected]
        assert [c[3] for c in actual] == [expected_body(m, sender) for m in selected]
    assert OutboundDelivery.objects.filter(enrollment__deal=deal).exclude(status='sent').count() == 0


@pytest.mark.parametrize('sender', OPERATORS)
@pytest.mark.parametrize('role', [*ROLE_WORDING, ''])
def test_real_role_wording(harness, request, sender, role):
    identify(request, sender, ROLE_KEY, 'role_wording', 'Saved role changes wording only, never the explicitly selected audience')
    session, deal = harness.make(sender, ROLE_KEY, store_icp=False, role=role)
    enrollment = enrollment_fixture(deal, ROLE_KEY, sender)
    steps = [m for m in list_message_steps(enrollment=enrollment) if '{role}' in m.body]
    assert steps, 'Imported JSON no longer contains a role placeholder to test'
    for step in steps:
        rendered = render_message(enrollment=enrollment, message=step)
        assert f'I work with {role_wording(role)}' in rendered.body
        assert enrollment.audience_key == ROLE_KEY


@pytest.mark.parametrize('sender', OPERATORS)
@pytest.mark.parametrize('scenario', ['pending', 'already_connected', 'reply', 'duplicate', 'missing_email', 'wrong_sender', 'missing_icp'])
def test_imported_branches(harness, request, sender, scenario):
    identify(request, sender, REFERENCE_KEY, scenario, 'Correct stop, routing, or handoff; no unintended provider calls')
    session, deal = harness.make(sender, REFERENCE_KEY)
    calls = transports(harness, request, sender, REFERENCE_KEY, deal)
    if scenario == 'pending':
        run_connect(harness, session, deal, ProfileState.PENDING)
        assert not calls
        assert not OutboundDelivery.objects.filter(enrollment__deal=deal).exists()
        return
    if scenario == 'missing_icp':
        deal.lead.icp = ''
        deal.lead.save(update_fields=['icp'])
        with pytest.raises(MessageDeliveryError):
            run_connect(harness, session, deal)
        assert not calls
        return
    seed_observed_thread(deal, sender)
    run_connect(harness, session, deal, ProfileState.CONNECTED)
    assert not calls
    if scenario == 'already_connected':
        run_due(harness, session, deal)
        assert not any(call[0] == 'linkedin_connect' for call in calls)
        assert len(calls) == 4
        return
    if scenario == 'reply':
        Message.objects.create(lead=deal.lead, source=Message.Source.GMAIL,
            direction=Message.Direction.INBOUND, sender=deal.lead.email,
            external_id='qa-reply', body='Thanks, let us talk', sent_at=timezone.now())
        run_due(harness, session, deal)
        assert not calls
        return
    if scenario == 'wrong_sender':
        task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP, status=Task.Status.PENDING)
        task.payload['operator'] = 'Chuka' if sender == 'Arian' else 'Arian'
        task.status = Task.Status.RUNNING
        task.started_at = timezone.now()
        task.save()
        with pytest.raises(MessageDeliveryError):
            handle_follow_up(task, session, {})
        assert not calls
        return
    if scenario == 'missing_email':
        # Separate lead: don't reset a queued delivery's recipient identity.
        session, other = harness.make(sender, REFERENCE_KEY)
        other.lead.email = ''
        other.lead.save(update_fields=['email'])
        other.state = ProfileState.CONNECTED
        other.connected_at = timezone.now()
        other.save()
        task = maybe_schedule_gmail_sequence(deal=other, operator=sender)
        assert task is not None and task.task_type == Task.TaskType.ENRICH_EMAIL
        assert task.payload['delivery_id']
        assert not calls
        return
    if scenario == 'duplicate':
        deal.refresh_from_db()
        count = Task.objects.count()
        task = enqueue_follow_up(session.campaign.pk, deal.lead.public_identifier,
            operator=sender, icp=REFERENCE_KEY, delay_seconds=0)
        assert Task.objects.count() == count
        run_due(harness, session, deal)
        first_count = len(calls)
        # Reprocessing a completed frozen delivery must never record a second send.
        handle_follow_up(task, session, {})
        assert len(calls) == first_count


def test_safety_guards():
    import socket
    import subprocess
    import psycopg
    from tests.campaign_qa.runner import ROOT
    with pytest.raises(QASafetyError):
        socket.create_connection(('example.com', 443))
    with pytest.raises(QASafetyError):
        subprocess.run(['open', 'https://linkedin.com'])
    with pytest.raises(QASafetyError):
        (ROOT / '.env').read_text()
    with pytest.raises(QASafetyError):
        psycopg.connect('dbname=production host=example.com')


def test_campaign_models_have_checked_in_migrations():
    call_command('makemigrations', 'crm', 'linkedin', check_changes=True,
                 dry_run=True, interactive=False, stdout=io.StringIO())

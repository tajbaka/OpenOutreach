from types import SimpleNamespace

import pytest

from crm.models.lead import LEAD_ROLE_TAG_VALUES
from gmail import templates as gmail_templates
from linkedin import icp_outbound
from linkedin.exceptions import MessageRoleError
from linkedin.message_roles import ROLE_WORDING, role_wording


def test_role_wording_covers_exactly_the_existing_canonical_tags():
    assert set(ROLE_WORDING) == set(LEAD_ROLE_TAG_VALUES)
    assert all(text and '{' not in text and '}' not in text for text in ROLE_WORDING.values())


@pytest.mark.parametrize(('tag', 'expected'), [
    ('Founder/CEO', 'founders'),
    ('CFO/Finance', 'finance leaders'),
    ('COO/Operations', 'operations leaders'),
    ('CRO/Revenue', 'revenue leaders'),
    ('Product Executive', 'product leaders'),
    ('Federal/Public Sector Executive', 'public sector leaders'),
    ('', 'companies'),
    ('  ', 'companies'),
    (None, 'companies'),
])
def test_role_wording_uses_explicit_alias_or_blank_fallback(tag, expected):
    assert role_wording(tag) == expected


@pytest.mark.parametrize('tag', ['CFO', 'finance leaders', '{first_name}', 1])
def test_unknown_nonblank_role_is_not_inferred_or_echoed(tag):
    with pytest.raises(MessageRoleError):
        role_wording(tag)


@pytest.mark.parametrize(('tag', 'expected'), [
    ('CFO/Finance', 'finance leaders'), ('', 'companies'),
])
def test_legacy_linkedin_role_wording_does_not_select_icp(monkeypatch, tag, expected):
    def channel_steps(*, sender, icp, channel):
        assert (sender, icp, channel) == ('Arian', 'CSPs', 'linkedin_connect_followup')
        return [icp_outbound.TemplateStep(delay_hours=0, variants=['I work with {role}.'])]

    monkeypatch.setattr(icp_outbound, 'channel_steps', channel_steps)
    monkeypatch.setattr(icp_outbound, '_media_names_for_icp', lambda **kwargs: ())
    lead = SimpleNamespace(id=7, first_name='Ada', role_tag=tag, icp='CSPs')
    result = icp_outbound.fill_for_lead(
        sender='Arian', role='CSP', channel='linkedin_connect_followup', lead=lead,
    )
    assert result.body == f'I work with {expected}.'
    assert lead.role_tag == tag and lead.icp == 'CSPs'


@pytest.mark.parametrize(('tag', 'expected'), [
    ('CFO/Finance', 'finance leaders'), ('', 'companies'),
])
def test_legacy_gmail_role_wording_in_subject_and_body(monkeypatch, tag, expected):
    monkeypatch.setattr(gmail_templates, '_load', lambda: {'Arian': {'CSPs': [{
        'delay_hours': 0, 'subject_variants': ['For {role}'],
        'body_variants': ['Hi {first_name}, I work with {role}.'],
    }]}})
    lead = SimpleNamespace(id=7, first_name='Ada', role_tag=tag)
    result = gmail_templates.render_for_lead(sender='Arian', role='CSP', lead=lead, step_index=0)
    assert result.subject == f'For {expected}'
    assert result.body == f'Hi Ada, I work with {expected}.'


def test_legacy_connection_note_uses_saved_role_without_changing_icp(monkeypatch):
    from crm.models import Lead
    from linkedin.tasks.connect import build_connection_note

    lead = Lead.objects.create(first_name='Ada', icp='CSPs', role_tag='CFO/Finance')
    monkeypatch.setattr(icp_outbound, 'load_icp_messages', lambda sender: {
        'CSPs': {'linkedin_connect_note': ['Hi {first_name}, I work with {role}.']},
    })
    assert build_connection_note(lead.pk, 'Arian') == 'Hi Ada, I work with finance leaders.'
    lead.refresh_from_db()
    assert lead.icp == 'CSPs' and lead.role_tag == 'CFO/Finance'

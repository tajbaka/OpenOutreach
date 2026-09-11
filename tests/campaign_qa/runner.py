"""Credential-free child process; deny network, browser, and private-data reads."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from tests.campaign_qa.exceptions import QASafetyError

ROOT = Path(__file__).resolve().parents[2]


def audit(event, args):
    if event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'socket.connect', 'socket.getaddrinfo'}:
        raise QASafetyError(f'Blocked in campaign QA: {event}')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name.startswith('.env') or any(path.is_relative_to(ROOT / name) for name in ('data', 'secrets')):
            raise QASafetyError('QA cannot read environment files, tokens, cookies, or private data')


def main():
    if not os.environ.get('CAMPAIGN_QA_RUN') or os.environ.get('DATABASE_URL'):
        raise QASafetyError('Use scripts/qa_campaigns.py, not this child directly')
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    # Install before Django or pytest loads application code. libpq is a native
    # client, so separately constrain its connection parameters to our socket.
    sys.addaudithook(audit)
    import psycopg
    connect = psycopg.connect

    def local_connect(conninfo='', **kwargs):
        if conninfo or kwargs.get('host') != os.environ['CAMPAIGN_QA_SOCKET']:
            raise QASafetyError('Only the runner-owned PostgreSQL socket is permitted')
        if kwargs.get('dbname') not in {'postgres', 'campaign_qa', 'test_campaign_qa'} or kwargs.get('user') != 'campaign_qa':
            raise QASafetyError('Unexpected QA database or user')
        return connect(**kwargs)

    psycopg.connect = local_connect
    import pytest
    common = [
        '-p', 'pytest_django.plugin', '-p', 'pytest_mock', '-p', 'tests.campaign_qa.reporting',
        '-q', '--tb=short', '-p', 'no:cacheprovider', '--strict-markers',
    ]
    if os.environ.get('CAMPAIGN_QA_SUITE') == 'accepted-connections':
        return pytest.main(common + [
            'tests/test_accepted_connections.py', 'tests/test_accepted_connections_sheet.py',
            'tests/management/test_refresh_crm_v2.py', 'tests/management/test_sync_sheets.py',
            'tests/management/test_notify_sync_sheets_health.py', 'tests/management/test_generate_followups.py',
            'tests/test_crm_v2_evidence.py', 'tests/test_crm_lock.py',
        ])
    return pytest.main(common + [
        'tests/campaign_qa/test_imported_campaigns.py',
        'tests/test_general_icp_json.py', 'tests/test_general_icp_messages.py',
        'tests/test_message_roles.py', 'tests/test_message_delivery.py',
        'tests/test_message_delivery_locks.py',
        'tests/test_message_program_models.py',
        'tests/test_seed_import.py', 'tests/test_seeds.py',
        'tests/tasks/test_message_program_runtime.py',
        'tests/tasks/test_follow_up_schedule.py', 'tests/test_heal.py',
        'tests/gmail/test_versioned_delivery.py', 'tests/gmail/test_invitation_start.py',
        'tests/gmail/test_invitation_recovery_cursor.py',
        'tests/gmail/test_handoff.py', 'tests/gmail/test_worker.py',
        'tests/gmail/test_current_timing.py', 'tests/gmail/test_enrichment_boundaries.py',
        'tests/test_campaign_email_mode.py',
        'tests/management/test_campaign_email_mode.py',
        'tests/migrations/test_campaign_gmail_start_mode.py',
        'tests/management/test_request_sender_restart.py',
        'tests/management/test_request_sender_stop.py',
        'tests/migrations/test_sender_restart_requested.py',
        'tests/migrations/test_sender_stop_requested.py',
        'tests/management/test_import_campaign_message_program.py',
        'tests/management/test_publish_general_icp_messages.py',
        'tests/tasks/test_stop_policy.py',
        'tests/test_browser_nav.py', 'tests/test_status_action.py',
        'tests/test_connect_action.py',
        'tests/tasks/test_follow_up_media.py', 'tests/tasks/test_drip_message_action.py',
        'tests/tasks/test_tasks.py', 'tests/test_message_media.py',
        'tests/drip', 'tests/discovery',
        'tests/test_single_instance.py', 'tests/test_daemon_supervisor_gmail.py',
        'tests/test_daemon_supervisor_feed.py', 'tests/test_daemon_supervisor_restart.py',
        'tests/test_daemon_supervisor_stop.py', 'tests/test_supervisor_safety.py',
        'tests/test_slack_supervisor_controls.py',
        'tests/test_supervisor_control.py',
        'tests/realtime/test_listener.py', 'tests/realtime/test_supervisor.py',
        'tests/realtime/test_listener_command.py',
        'tests/test_daemon_resilience.py',
        'tests/gmail/test_delivery.py', 'tests/gmail/test_client.py',
        'tests/gmail/test_templates.py',
        'tests/gmail/tasks', 'tests/test_icp_outbound.py',
        'tests/enrichment/test_worker.py',
        'tests/test_general_icp_messages_sheet.py', 'tests/test_general_icp_review_status.py',
        'tests/test_lead_role_tag.py', 'tests/test_sales_nav_saved_search_exports.py',
        'tests/management/test_review_general_icp_messages.py',
        'tests/management/test_sync_sheets.py', 'tests/test_sheets.py',
        'tests/test_accepted_connections.py', 'tests/test_accepted_connections_sheet.py',
        'tests/management/test_refresh_crm_v2.py', 'tests/test_crm_lock.py',
        'tests/management/test_sync_gmail_context_failures.py',
    ])


if __name__ == '__main__':
    raise SystemExit(main())

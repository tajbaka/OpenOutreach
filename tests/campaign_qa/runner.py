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
    return pytest.main([
        '-p', 'pytest_django.plugin', '-p', 'tests.campaign_qa.reporting',
        '-q', '--tb=short', '-p', 'no:cacheprovider', '--strict-markers',
        'tests/campaign_qa/test_imported_campaigns.py',
        'tests/test_general_icp_json.py', 'tests/test_general_icp_messages.py',
        'tests/test_message_roles.py', 'tests/test_message_delivery.py',
        'tests/test_message_program_models.py',
        'tests/test_seed_import.py', 'tests/test_seeds.py',
        'tests/tasks/test_message_program_runtime.py',
        'tests/gmail/test_versioned_delivery.py',
        'tests/management/test_import_campaign_message_program.py',
        'tests/management/test_publish_general_icp_messages.py',
        'tests/tasks/test_stop_policy.py',
        'tests/test_browser_nav.py', 'tests/test_status_action.py',
        'tests/test_connect_action.py',
    ])


if __name__ == '__main__':
    raise SystemExit(main())

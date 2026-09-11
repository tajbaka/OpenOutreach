"""Run campaign QA on a newly created, socket-only PostgreSQL cluster.

No DATABASE_URL argument, existing database, real credentials, or live-send
mode is supported. Invoke with .venv/bin/python from the repository root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
INPUTS = (ROOT / 'linkedin/icp_messages.json', ROOT / 'gmail/icp_emails.json')


def source_fingerprint() -> tuple[str, int]:
    """Identify the uncommitted source under test without reading credentials."""
    paths = set(ROOT.glob('*.py'))
    for directory in ('linkedin', 'gmail', 'drip', 'crm', 'chat', 'tests', 'scripts', 'requirements'):
        paths.update(
            path for path in (ROOT / directory).rglob('*')
            if path.is_file() and path.suffix in {'.py', '.json', '.j2', '.txt'}
            and '__pycache__' not in path.parts
        )
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode('utf-8') + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest(), len(paths)


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, help='New directory for QA reports; never overwritten')
    parser.add_argument('--suite', choices=('campaigns', 'accepted-connections'), default='campaigns',
                        help='Full campaign QA or the focused accepted-connections/CRM sync regression suite')
    args = parser.parse_args()
    expected_python = ROOT / '.venv/bin/python'
    if Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
        parser.error(f'Use {expected_python}')
    binaries = {name: shutil.which(name) for name in ('initdb', 'pg_ctl', 'postgres')}
    if not all(binaries.values()):
        parser.error('Local PostgreSQL binaries initdb, pg_ctl, and postgres are required. No existing DB is used.')
    version = subprocess.check_output([binaries['postgres'], '--version'], text=True).strip()
    if not version.startswith('postgres (PostgreSQL) 17.'):
        parser.error(f'PostgreSQL 17 is required for this rehearsal; found {version}')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = (args.output_dir or ROOT / 'artifacts/qa/campaigns' / run_id).resolve()
    output.mkdir(parents=True, exist_ok=False)
    fingerprints = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in INPUTS}
    source_sha256, source_file_count = source_fingerprint()
    clean_env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TZ', 'TMPDIR') if key in os.environ}
    # A short private Unix-socket path avoids both TCP exposure and macOS's
    # socket path limit. This cluster contains synthetic data only.
    cluster = Path(tempfile.mkdtemp(prefix='campaign-qa-', dir='/tmp')).resolve()
    socket_dir = cluster / 'sock'
    socket_dir.mkdir(mode=0o700)
    (cluster / 'QA_ONLY').write_text(run_id, encoding='utf-8')
    data = cluster / 'pgdata'
    result = 2
    started = False
    stopped = False
    try:
        with (output / 'postgres-setup.log').open('w', encoding='utf-8') as log:
            subprocess.run([binaries['initdb'], '-D', str(data), '-U', 'campaign_qa',
                            '--auth-local=trust', '--auth-host=reject', '--no-locale', '--encoding=UTF8'],
                           env=clean_env, stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run([binaries['pg_ctl'], '-D', str(data), '-l', str(output / 'postgres.log'),
                            '-o', f"-h '' -k {socket_dir} -c unix_socket_permissions=0700", '-w', 'start'],
                           env=clean_env, stdout=log, stderr=subprocess.STDOUT, check=True)
        started = True
        env = {**clean_env, 'DATABASE_URL': '', 'PYTHON_DOTENV_DISABLED': '1',
               'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1',
               'DJANGO_SETTINGS_MODULE': 'tests.campaign_qa.settings',
               'CAMPAIGN_QA_SOCKET': str(socket_dir), 'CAMPAIGN_QA_RUN': run_id,
               'CAMPAIGN_QA_SUITE': args.suite,
               'CAMPAIGN_QA_OUTPUT': str(output)}
        print(f'Campaign QA: {version}; private socket; all external delivery blocked.\nReports: {output}', flush=True)
        with (output / 'test-output.log').open('w', encoding='utf-8') as log:
            result = subprocess.run([str(expected_python), '-m', 'tests.campaign_qa.runner'],
                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=300, check=False).returncode
        report = output / 'report.json'
        if report.exists():
            summary = json.loads(report.read_text())
            print(json.dumps({key: summary[key] for key in ('collected', 'counts', 'preview_count', 'collection_errors')}), flush=True)
    finally:
        # Only stop/remove the exact cluster created by this invocation. Retain
        # it on stop failure so cleanup can never delete a running database.
        if data.exists():
            status = subprocess.run([binaries['pg_ctl'], '-D', str(data), 'status'],
                                    env=clean_env, capture_output=True, check=False)
            if status.returncode == 0:
                subprocess.run([binaries['pg_ctl'], '-D', str(data), '-m', 'fast', '-w', 'stop'],
                               env=clean_env, check=True, capture_output=True)
            elif status.returncode != 3:
                raise RuntimeError(f'Cannot verify private QA cluster stopped: {cluster}')
        stopped = True
        assert cluster.parent == Path('/tmp').resolve() and (cluster / 'QA_ONLY').read_text() == run_id
        shutil.rmtree(cluster)
        unchanged = fingerprints == {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in INPUTS}
        source_unchanged = (source_sha256, source_file_count) == source_fingerprint()
        receipt = {'run_id': run_id, 'postgres_version': version, 'exit_code': result,
                   'suite': args.suite,
                   'input_sha256': fingerprints, 'input_files_unchanged': unchanged,
                   'source_tree_sha256': source_sha256, 'source_file_count': source_file_count,
                   'source_files_unchanged': source_unchanged,
                   'private_cluster_started': started, 'private_cluster_stopped_and_removed': stopped,
                   'live_sending': False, 'production_database_access': False}
        (output / 'run.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        assert unchanged, 'QA must not alter the message JSON files'
        assert source_unchanged, 'Source changed during QA; rerun against the final working tree'
    print(f'QA reports: {output}\nExit status: {result}', flush=True)
    return result


if __name__ == '__main__':
    raise SystemExit(run())

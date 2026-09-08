"""Small pytest report writer for rendered copy and honest case outcomes."""
import csv
import json
import os
from collections import Counter
from pathlib import Path

CASES = {}
PREVIEWS = []
COLLECTION_ERRORS = []


def pytest_runtest_logreport(report):
    if report.when == 'call' or report.failed:
        details = dict(report.user_properties)
        CASES[report.nodeid] = {
            'case': report.nodeid, 'sender': details.get('sender', ''),
            'icp': details.get('icp', ''), 'scenario': details.get('scenario', report.nodeid.split('::')[-1]),
            'outcome': report.outcome, 'expected': details.get('expected', 'All assertions pass'),
            'actual': str(report.longrepr) if report.failed else details.get('actual', 'All assertions passed'),
        }


def pytest_collectreport(report):
    if report.failed:
        COLLECTION_ERRORS.append(str(report.longrepr))


def write_csv(path, rows, headers):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def pytest_sessionfinish(session, exitstatus):
    output = Path(os.environ['CAMPAIGN_QA_OUTPUT'])
    cases = list(CASES.values())
    counts = dict(Counter(row['outcome'] for row in cases))
    holds = sorted({row['icp'] for row in cases
                    if row['scenario'] == 'connection_with_incomplete_followup_held'})
    write_csv(output / 'case_results.csv', cases,
              ['case', 'sender', 'icp', 'scenario', 'outcome', 'expected', 'actual'])
    write_csv(output / 'message_previews.csv', PREVIEWS,
              ['case', 'sender', 'icp', 'role_tag', 'channel', 'step', 'subject', 'body', 'kind'])
    summary = {'exit_status': int(exitstatus), 'counts': counts, 'collected': session.testscollected,
               'preview_count': len(PREVIEWS), 'collection_errors': COLLECTION_ERRORS,
               'incomplete_linkedin_followup_cohorts': holds,
               'cases': cases, 'scope': 'Isolated PostgreSQL; imported JSON; provider calls recorded, never delivered'}
    (output / 'report.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    failures = [row for row in cases if row['outcome'] == 'failed']
    classified = Counter()
    for row in failures:
        if 'FOR UPDATE cannot be applied to the nullable side' in row['actual']:
            classified['PostgreSQL rejects nullable-join row locking'] += 1
        elif 'value too long for type character varying(64)' in row['actual']:
            classified['Audience key exceeds Lead.icp database length'] += 1
        elif row['scenario'] == 'csv_audience_import':
            classified['CSV importer does not preserve the new audience key'] += 1
        else:
            classified['Other or downstream failure; inspect case_results.csv'] += 1
    lines = ['# Campaign QA', '', 'No live accounts, browser sessions, CRM records, or messages were used.', '',
             f"Collected: {session.testscollected}. Results: {counts}. Rendered previews: {len(PREVIEWS)}.", '',
             'See case_results.csv for every expected/actual result and message_previews.csv for the exact copy.', '',
             'This does not verify live authentication, browser selectors, provider delivery, or inbox placement.', '',
             'Rendering tests use fixture enrollments so copy can be inspected even when enrollment fails. Full-sequence tests do not bypass campaign enrollment. A failed setup means later steps are NOT verified.', '',
             '## Copy still requiring completion', '',
             *([f'- {label}: first LinkedIn follow-up is blank; later follow-ups are held, not skipped ahead to.' for label in holds] or ['None.']), '',
             'A passed hold check proves no incomplete sequence was queued; it is not a completed message sequence.', '',
             '## Failure groups', '']
    lines.extend(f'- {name}: {count} failed checks (not distinct bugs).' for name, count in classified.items())
    lines.extend(['', '## Imported-copy scenarios', '', '| Scenario | Passed | Failed |', '| --- | ---: | ---: |'])
    scenarios = sorted({row['scenario'] for row in cases if row['case'].startswith('tests/campaign_qa/')})
    for scenario in scenarios:
        selected = [row for row in cases if row['scenario'] == scenario]
        passed = sum(row['outcome'] == 'passed' for row in selected)
        failed = sum(row['outcome'] == 'failed' for row in selected)
        lines.append(f'| {scenario} | {passed} | {failed} |')
    lines.extend(['', '## First failing cases', ''])
    lines.extend(f"- {row['scenario']} ({row['sender']}): {row['icp']}" for row in failures[:5])
    if len(failures) > 5:
        lines.append(f'- {len(failures)-5} additional failing checks are listed in case_results.csv.')
    lines.extend(COLLECTION_ERRORS)
    if not failures and not COLLECTION_ERRORS:
        lines.append('None.')
    (output / 'README.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

# Windows smoke-test prompt

This is a one-time simulation before a future rollout, not the recurring
visitor-review automation. The new files must first be available in a separate
review checkout on Windows. Do not pull them into the running worker's checkout.

Copy this prompt:

> Run the new campaign-priority smoke test on this Windows computer using a
> separate review checkout containing the candidate changes. Leave the running
> OpenOutreach checkout, its environment, supervisor and campaigns untouched.
>
> Locate the review copy of OpenOutreach and its separate rb2b-integration copy.
> Resolve a Python interpreter with the project's test dependencies. Reusing the
> existing venv interpreter is fine; do not change that venv or load its .env.
> If dependencies are missing, report that instead of falling back to plain
> pytest, manage.py, a real database, or browser-based sends.
>
> From the review OpenOutreach directory, run the equivalent of:
>
> ```powershell
> & "C:\actual\path\to\python.exe" -I -B .\scripts\smoke_campaign_priority.py --integration-dir "C:\actual\review\rb2b-integration"
> ```
>
> Replace paths with verified local paths. This command uses an in-memory
> synthetic database and fake LinkedIn/Slack boundaries. It removes inherited
> credentials, blocks .env/private runtime data reads, rejects PostgreSQL,
> network and subprocess calls inside the tests, and does not start the worker.
>
> Inspect the printed summary and its core/integration JSON receipts and logs.
> Require both suites to exit 0, report passed tests, and record all four guard
> checks. Confirm the smoke scenario records one simulated invitation, one
> simulated follow-up, two simulated callback attempts and zero real sends.
> Check default/equal campaign order, the due follow-up boundary, sender/limit
> guards, duplicate enrollment, frozen copy, exclusions, reply stopping and
> old-visitor no-resend holds through the included tests. Report failures and
> skipped tests accurately. Never remove guards or retry through live systems.
>
> Return the exact Windows Python version, counts, skipped cases and receipt
> paths. A successful simulation does not prove real LinkedIn selectors or
> PostgreSQL concurrency, and does not complete the old-release handover. Stop
> after reporting. Do not migrate, activate the new workflow, restart, modify
> limits, send invitations/messages, deploy, commit or push.

The runner's JSON distinguishes simulation success from PostgreSQL validation
and rollout authorization, both of which remain false. A later authorized
cutover still needs the historical receipt/budget/reply-notification handover.

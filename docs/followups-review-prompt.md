# Run the Followups review

Open this repository on the computer that has the existing OpenOutreach database,
Google Sheets and Arian Gmail connections. Give Codex this request:

> Read `docs/followups-workflow.md` and run one complete Followups review now.
> Use this checkout's virtualenv and existing authentication. Read stored LinkedIn
> conversations and live Arian Gmail, including Sent. Join only verified contact
> identities and assess each request against the dated context on both channels.
> Review the last 14 days for new requests and every current open item regardless
> of age; extend discovery over any known missed interval. Update only the existing
> Followups tab in the configured FedRampGPT workbook, preserving human Notes,
> stable identities and formatting. Include readable Relevant ICP labels grounded
> in saved assignments or explicitly marked suggestions. Remove fulfilled items
> only after saving their exact completion evidence. Recheck live sources and rows
> before editing, verify the changed ranges by API readback, and confirm LinkedIn
> Pending is unchanged. Save a new private run receipt under artifacts/followups.
> Report additions, resolutions, meaningful changes and any incomplete coverage.
> Do not send messages, create Gmail drafts, mutate CRM records, run other
> publishers, start workers, or create/change a schedule.

To inspect without publishing, add: **Preview only; do not write to Google Sheets.**

This prompt can also be used in a receiving-computer Codex scheduled task after
the operator separately enables its daily 09:00 America/Toronto schedule and
disables the old host's timer. Pulling the repository does not copy a local timer,
credentials or private review receipts.

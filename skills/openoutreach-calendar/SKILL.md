---
name: openoutreach-calendar
description: Read, find, or schedule Google Calendar meetings for Arian or Eddy from OpenOutreach on this Mac. Use the repo's existing Google OAuth when the user asks to check their calendar or book a meeting from OpenOutreach.
---

# Google Calendar through OpenOutreach

Run the commands from the OpenOutreach repository root with `.venv/bin/python`. Use `--account arian_boundera` for Arian, including "my calendar" in Arian's chat, and `--account eddy_boundera` for Eddy or Chuka. The commands use that account's local Calendar token and its primary calendar. Use the repo's OAuth by default for this workflow. If the token or scope fails, report the specific account and error. Run `calendar_oauth` again when the user asks to reconnect.

## Read meetings

Use `manage.py calendar_read` with `--account`, `--start`, and `--end`. Use `--query` for a title, guest email, or other search text. Bound the search to the requested time range. A `has_more` result means the list is partial. Narrow the window or increase `--limit` as needed. Interpret relative dates in the user's timezone and summarize only the relevant events.

## Schedule a meeting

Resolve the calendar owner, title, date, start, end, timezone, and any guest addresses from the request. Ask for missing details that would change the meeting or who is invited. Both timestamps take an ISO 8601 offset, such as `2026-11-12T14:00:00-05:00`. Use a read for the proposed window when availability matters.

Run `manage.py calendar_schedule` with `--account`, `--title`, `--start`, and `--end`. Add one `--attendee` per confirmed guest email, and include `--description` or `--location` when supplied. The default is a preview. For an authorized booking, add `--apply` once the details match the request. Google Calendar invites the listed guests when the event is created.

Report the organizer, time in the user's timezone, invited guests, and returned Calendar link. If the result is uncertain, read the exact window and check the title, time, and guests before another attempt. Reuse the same command details for a retry so it targets the same event ID.

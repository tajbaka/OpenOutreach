# Realtime event wire format — captured 2026-05-16

Captured live via `scripts/capture_realtime_events.py` against a real
LinkedIn session. These notes ground the SSE buffer + parser.

## Transport (differs from the original design spec)

The design spec assumed CDP `Network.eventSourceMessageReceived`. **That
event never fires** — LinkedIn's web client does not use the native
`EventSource` API. The realtime feed is a long-lived streaming `fetch()`:

- Request: `GET https://www.linkedin.com/realtime/connect?rc=1`
- Response: `type=Fetch`, `mimeType=text/event-stream`

It is captured by: watch `Network.requestWillBeSent` for a URL containing
`/realtime/connect` → on `Network.responseReceived` call
`Network.streamResourceContent` for that `requestId` → subsequent
`Network.dataReceived` events then carry the body bytes in a base64 `data`
field (plus the `streamResourceContent` result's `bufferedData`).

This is still pure observation — no injected script, no second connection.

## SSE framing

The decoded stream is Server-Sent-Events text. Each event is a line
`data: <compact-json>` terminated by a blank line (`\n\n`). The JSON is
single-line (no embedded newlines). One `Network.dataReceived` chunk may
contain multiple events; an event may also be split across two chunks —
so a buffer that holds the trailing partial is required.

## Event envelopes (the outer JSON key)

- `com.linkedin.realtimefrontend.DecoratedEvent` — all topic events.
- `com.linkedin.realtimefrontend.Heartbeat` — `{}`, keep-alive.
- `com.linkedin.realtimefrontend.ClientConnection` — initial handshake.

## DecoratedEvent

`event["com.linkedin.realtimefrontend.DecoratedEvent"]` has:
- `topic` — e.g. `urn:li-realtime:messagesTopic:urn:li-realtime:myself`.
  The topic substring identifies the event kind:
  `messagesTopic`, `typingIndicatorsTopic`, `messageSeenReceiptsTopic`,
  `conversationsTopic`, `tabBadgeUpdateTopic`, `replySuggestionTopicV2`.
- `payload`, `id`, `leftServerAt`, `publisherTrackingId`.

## Inbound/outbound message event (`messagesTopic`)

The message object is at:
`DecoratedEvent.payload.data.doDecorateMessageMessengerRealtimeDecoration.result`

Fields on that `result`:
- `body.text` — message text (`body` is an AttributedText dict).
- `backendUrn` — message URN `urn:li:messagingMessage:2-...` (idempotency key).
- `deliveredAt` — epoch milliseconds.
- `conversation.entityUrn` — `urn:li:msg_conversation:(urn:li:fsd_profile:...,2-...)`
  — this is the form that matches `Message.thread_external_id` written by
  `persist_thread`. Prefer it for lead resolution.
- `backendConversationUrn` — `urn:li:messagingThread:2-...` (alternate form).
- `actor.hostIdentityUrn` — sender's `urn:li:fsd_profile:...`.
- `actor.participantType.member.firstName.text` / `lastName.text` — sender name.

Inbound vs outbound is determined downstream by `persist_thread` (sender
name vs lead name); the parser extracts every `messagesTopic` event.

## Fixtures in this directory

- `inbound_message.json` — messagesTopic, sender "Arian Taj", "hey im interested".
- `outbound_echo.json`   — messagesTopic, sender "Chuka Agu" (the daemon account).
- `typing_indicator.json` — typingIndicatorsTopic → parser returns None.
- `read_receipt.json`     — messageSeenReceiptsTopic → parser returns None.
- `presence.json`         — Heartbeat → parser returns None.
- `raw_stream_chunk.txt`  — a raw multi-event SSE chunk for the SSE-buffer tests.

# TBHprint ↔ TechBenchHub protocol

The server side lives in the TechBenchHub repo (`docs/PRINT_AGENT_DESIGN.md`,
`routes/api.php` "Print agent API", `routes/channels.php`). This is the
contract the agent implements. Versioned by the `/v1` path.

All requests: `Authorization: Bearer <token>` (except pair),
`X-TBHprint-Version`, `X-TBHprint-Platform`. JSON in, JSON out.

Every authenticated request (since 0.1.10) also carries
`X-TBHprint-Transport`, read at the moment the request is sent:

- `reverb` — the websocket is connected AND subscribed to the agent's
  channel; jobs arrive the instant they are queued.
- `poller` — anything else (socket down, reconnecting, never configured,
  `transport.mode: poll`); jobs arrive on the next sweep, up to
  `poll_interval_s` late. The server shows the agent amber, "Online,
  polling only", and warns the shop after ten minutes of `poller`.

## Pair

`POST /api/print/v1/pair` `{code, name, platform?, version?}`

- 201 `{agent_uuid, name, token, tenant, channel, reverb: {key, host, port, scheme}}`
- 422 `{error: "unknown_code", message}` — unknown, used or expired code
- Throttled 10/min per IP. Codes are 8 characters, single use, 10 minutes.

## Update

`GET /api/print/v1/update?platform=windows|linux&version=<current>`

- 200 `{version, url, sha256, notes}` — a newer build exists (`url` is
  same-host, checked with the document-download allowlist; `sha256`
  verified before install)
- 200 `{version: null}` — already current, or no update feed configured

Checked after the first successful `status`, every 6 hours, and on
`tbhprint update` / the tray's "Check for updates". Never GitHub — this
repo is private and the agent only ever holds its own bearer token.

## Job JSON (identical on the websocket and the REST list)

```json
{
  "uuid": "6b0e…",
  "document_type": "ticket_label",
  "title": "Ticket label #1042",
  "copies": 2,
  "source": {"type": "ticket", "id": 17, "label": "#1042"},
  "document_url": "https://shop.techbenchhub.co.uk/api/print/v1/jobs/6b0e…/document",
  "status": "queued",
  "origin": "manual",
  "created_at": "2026-09-01T10:15:00+01:00"
}
```

Document types: `invoice`, `receipt`, `estimate`, `credit_note`,
`purchase_order`, `booking_sheet`, `collection_form`, `ticket_label`.

## Realtime

Pusher protocol to `wss://{reverb.host}:{reverb.port}/app/{reverb.key}`.
After `pusher:connection_established` the agent POSTs
`/api/print/v1/broadcasting/auth` `{socket_id, channel_name}` and sends
`pusher:subscribe` `{channel, auth}`. Event name: `print.job`, data = Job
JSON (Pusher double-encodes data as a string).

## Catch-up / fallback

`GET /api/print/v1/jobs[?since=<iso>]` →
`{jobs: [Job…], server_time, reverb: {key, host, port, scheme}, channel}` —
every job still `queued` or `delivered` for THIS agent, not expired,
oldest first, max 200. The agent calls it on start, on every websocket
(re)connect, every `poll_interval_s` while the websocket is down and every
`heartbeat_interval_s` while it is up.

`reverb` and `channel` are the same block pairing returns, repeated so an
agent follows the server when its websocket host or key moves (production
moved from the droplet's Reverb to Laravel Cloud's on 2026-11-01; agents
paired before 0.1.10 kept dialling the dead host and printed on the
fallback poll). The agent compares them with its config on every sweep and
does nothing when they match; when the host, key, port, scheme or channel
differ it saves them and reconnects the websocket on the new ones. A reply
without the block (or with an empty `host` or `key`) changes nothing.

The websocket handshake sends `Origin: <scheme>://<host[:port]>` of the
paired server URL. Reverb checks the Origin header against its allowed
origins; a self-hosted one is usually `*`, but Laravel Cloud's hosted Reverb
allows only the environment's own domains and answers a missing or foreign
origin with `pusher:error` 4009 "Origin not allowed" before
`connection_established` (found 2026-11-03, the day after the move). The
agent presents the origin a browser on the shop's own page would, and a
`pusher:error` in the handshake is logged with its code and message.

## Acks

`POST /api/print/v1/jobs/{uuid}/ack` `{state: received|printed|failed, error?}`
→ `{job: Job, applied: bool}`.

- `received` moves queued → delivered (idempotent; `applied:false` on repeat)
- `printed` / `failed` are terminal; a later ack never overwrites them
- another agent's uuid → 404

## Document

`GET /api/print/v1/jobs/{uuid}/document` → `application/pdf`, rendered on
demand from the source record. 410 `{error: "render_failed", message}` when
the record is gone; 500 for a render error (both are logged server-side).

The agent only fetches from the paired host, over HTTPS (HTTP only if the
server itself was paired over HTTP, i.e. local development), ≤ 25 MB,
must start with `%PDF`.

## Agent obligations

- Dedupe by `uuid` (SQLite history); ack `received` once per job.
- Route by `document_type`; no route → ack `failed` with the reason so the
  owner sees "no printer routed for ticket_label on Front desk" in Settings.
- Print through the OS driver; ack `printed` after the OS accepted the job
  (and flip to `failed` if CUPS later reports the job aborted).

# TBRprint — the TechBenchHub print agent

A small background program for a shop PC. TechBenchHub queues a print job
(ticket label at intake, booking sheet, invoice, collection form…); TBRprint
receives it over an authenticated websocket, fetches the PDF, prints it to the
printer you routed that document type to, and reports back — so Settings →
Printing in TechBenchHub shows *printed* or *failed (reason)* for every job.

Windows, Linux (CUPS), macOS (CUPS). Standard OS printer drivers only — no
raw ESC/POS, no cash drawers, no printer-specific code. If it prints from a
browser it prints from TBRprint.

## Install

Python 3.11+.

```
pip install .
# Windows, optional but recommended for reliable silent printing:
#   put SumatraPDF.exe on PATH or in C:\Program Files\SumatraPDF\
```

## Pair with your shop

In TechBenchHub: **Settings → Printing → Add agent** shows an 8-character
code (valid 10 minutes, single use). On the shop PC:

```
tbrprint pair https://yourshop.techbenchhub.co.uk ABCD2345 --name "Front desk"
```

That stores the server URL, this agent's own bearer token and the websocket
details in the config file (`%ProgramData%\TBRprint\config.json` on Windows,
`/etc/tbrprint/config.json` on Linux). Revoking the agent in Settings kills
that token immediately.

## Route documents to printers

```
tbrprint printers                                  # what the OS knows
tbrprint route ticket_label   --printer "Brother QL-800" --copies 2
tbrprint route booking_sheet  --printer "HP LaserJet"
tbrprint route invoice        --printer "HP LaserJet"
tbrprint route collection_form --printer "HP LaserJet"
tbrprint test-print "HP LaserJet"
```

Document types: `ticket_label`, `booking_sheet`, `collection_form`,
`invoice`, `receipt`, `estimate`, `credit_note`, `purchase_order`. A type
with no route fails the job with "no printer routed" so the reason is
visible in TechBenchHub rather than silently dropped.

## Run

```
tbrprint run                 # foreground, logs to stdout
tbrprint run --dry-run       # everything except the actual print
tbrprint service install     # Windows: scheduled task at logon; Linux: prints the systemd steps
tbrprint status
tbrprint history --limit 20
tbrprint reprint <uuid>
tbrprint pause / resume
```

## How delivery works (docs/PROTOCOL.md)

1. `pair` → `POST /api/print/v1/pair` → agent uuid + token + Reverb details.
2. Realtime: websocket to Reverb (Pusher protocol), private channel
   `private-tenant.<tenant>.print-agent.<uuid>` authorised with the token via
   `POST /api/print/v1/broadcasting/auth`. Event `print.job` carries the job.
3. Catch-up: on every (re)connect, and every `poll_interval_s` while the
   websocket is down, `GET /api/print/v1/jobs` lists every job still open
   for this agent. Jobs are deduplicated by their server-issued uuid.
4. Each job: `ack received` → fetch `document_url` (must be on the paired
   host, HTTPS, PDF, ≤ 25 MB) → print → `ack printed` or `ack failed`
   with the reason.

## Development

```
pip install -e .[dev]
pytest
```

MIT. Architecture carried over from the author's SyncroPrint for Linux
daemon — see NOTICE.

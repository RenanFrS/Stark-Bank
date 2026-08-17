# Stark Bank Invoice to Transfer

Integration built for the Stark Bank Back End Developer Trial.

The service issues invoices to random people on a fixed schedule and, whenever
the sandbox credits one of them, forwards the received amount net of fees to the
destination account specified in the challenge.

## What it does

1. Every 3 hours, for 8 batches covering a full 24 hour window, it issues
   between 8 and 12 invoices with randomly generated payers.
2. It exposes a webhook endpoint that verifies the ECDSA signature of every
   incoming Stark Bank event.
3. On an invoice credit event, it creates a Transfer for `amount - fee` to
   bank code `20018183`, branch `0001`, account `6341320293482496`.
4. Every 15 minutes it runs two sweeps: one over the events Stark Bank still
   considers undelivered, and one over its own rows that still owe a transfer.

## Design decisions

**Seeing an event is not the same as finishing it.** `processed_events` is keyed
by the Stark Bank event id, and the claim is an INSERT guarded by the primary
key, so two concurrent deliveries race in the database and exactly one wins. But
losing that race does not mean the work is done: a row can exist and still owe a
transfer. So a collision is resolved by reading the row's status —
`TRANSFERRED` and `SKIPPED` are final, `FAILED` is retried at once, and
`RECEIVED` is retried only after a grace window, since a background task may
still hold it.

**Two sweeps, because one queue is not enough.** `event.query(is_delivered=False)`
finds deliveries that never arrived. It cannot find the more dangerous case: the
webhook answered 200, so Stark Bank counts the event as delivered and drops it
from that queue, and only afterwards did the transfer fail. `sweep_local_ledger`
walks our own unfinished rows, which is the only view that still contains them.

**A retry looks before it sends.** `external_id = invoice-{invoice_id}` makes
Stark Bank reject a second Transfer for the same invoice — but the rejection
arrives as `InputErrors`, indistinguishable from a malformed request, and
`transfer.query` has no `external_ids` filter to check with. The invoice id is
therefore mirrored into a tag, which *is* queryable, so a retry can tell "already
paid" from "failed".

**Failure has a floor.** After `TRANSFER_MAX_ATTEMPTS`, an event becomes
`ABANDONED` rather than retrying forever. It stops consuming attempts, keeps
reporting itself on every pass, and surfaces in `/status` for a human.

**The webhook answers before it works.** Creating a Transfer inline would hold
the connection open long enough for Stark Bank to treat the delivery as failed.
The handler verifies the signature, records the claim, returns 200, and processes
in a background task.

**Error codes are chosen for what they cause.** A forged signature is 401. A
genuinely malformed body is 400. Anything unexpected is 500, because
`event.parse` fetches Stark Bank's public key over HTTP and a 4xx there would
tell the sender not to bother retrying a valid event.

**Retries are selective.** `InputErrors` means the request itself is wrong, so
retrying reproduces the rejection. Transient server and network faults get
exponential backoff.

**Money is always an integer.** Amounts are handled in cents throughout. A float
never touches a monetary value.

**The batch counter lives in the database.** A restart halfway through the 24
hour window resumes where it stopped instead of replaying the schedule.

## Architecture

```
                    ┌──────────────────┐
   every 3h  ──────▶│  invoice_issuer  │──── invoice.create ────▶ Stark Bank
                    └──────────────────┘
                                                                      │
                                                             sandbox pays
                                                                      │
                                                                      ▼
   POST /webhooks/starkbank  ◀──────── signed event ──────────  Stark Bank
              │
              ├── event.parse  (ECDSA signature check, else 401)
              ├── 200 returned immediately
              │
              ▼
      ┌─────────────────┐      claim      ┌──────────────────┐
      │ event_processor │◀───────────────▶│ processed_events │
      └─────────────────┘                 └──────────────────┘
              │
              │ log.type == "credited"
              ▼
      ┌──────────────────┐
      │ transfer_service │──── transfer.create ────▶ destination account
      └──────────────────┘      (external_id guard)

   every 15m ──┬─▶ run()                ──▶ event.query(is_delivered=False) ─┐
               │                                                            │
               └─▶ sweep_local_ledger() ──▶ rows still owing a transfer ────┤
                                                                            ▼
                                                                        processor
```

The second sweep exists because a webhook that answered 200 removes the event
from the undelivered queue, even if the transfer afterwards failed.

## Project layout

```
app/
  api/
    webhook.py            signature verification, fast 200, background dispatch
    health.py             /health probe and /status run summary
  db/
    models.py             processed_events, issued_invoices, issuer_batches
    database.py           engine, session scope, WAL pragmas
  repositories/
    event_repository.py   the claim, and which rows still owe work
    invoice_repository.py batch bookkeeping
  services/
    invoice_issuer.py     batch generation and emission
    transfer_service.py   fee math, duplicate lookup, selective retry
    event_processor.py    the one place the rules live
    reconciliation.py     the remote and local sweeps
  utils/
    cpf.py                modulus 11 generation and validation
    people.py             random payer generation
  config.py               environment-driven settings
  scheduler.py            APScheduler wiring
  main.py                 FastAPI app and lifespan
scripts/
  generate_keys.py        ECDSA key pair
  manage_webhook.py       register, list, delete subscriptions
  smoke_test.py           credential check
tests/                    65 tests
```

## Setup

Requires Python 3.11 or newer.

```bash
git clone <your-repo-url>
cd starkbank_challenge

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt
```

### 1. Generate the key pair

```bash
python scripts/generate_keys.py
```

This writes `keys/private-key.pem` and `keys/public-key.pem`. The directory is
gitignored.

### 2. Create the Project in the sandbox

1. Log into https://web.sandbox.starkbank.com
2. Menu, then Integrations, then New Project
3. Upload `keys/public-key.pem`
4. Copy the resulting Project ID

### 3. Configure the environment

```bash
cp .env.example .env
```

Fill in `STARKBANK_PROJECT_ID`. Leave `STARKBANK_PRIVATE_KEY_PATH` pointing at
the PEM file for local work.

### 4. Verify the credentials

```bash
python scripts/smoke_test.py
```

It prints the balance and issues one invoice. If that works, everything is
wired correctly.

### 5. Run the tests

```bash
pytest
```

## Running locally with a real webhook

The Stark Bank API needs a public HTTPS URL, so tunnel your local port:

```bash
# terminal 1
uvicorn app.main:app --reload --port 8080

# terminal 2
ngrok http 8080

# terminal 3, using the https url ngrok printed
python scripts/manage_webhook.py register https://xxxx-xx.ngrok-free.app
```

Confirm what is registered at any time:

```bash
python scripts/manage_webhook.py list
```

## Deployment

### Fly.io

Chosen because the free tiers that scale to zero introduce a cold start, and a
cold start during a webhook delivery is a dropped event. The configuration keeps
one machine warm and mounts a volume so the SQLite file survives restarts.

```bash
fly auth login
fly apps create your-app-name          # then update the app name in fly.toml
fly volumes create starkbank_data --size 1 --region gru

fly secrets set \
  STARKBANK_PROJECT_ID="your_project_id" \
  STARKBANK_PRIVATE_KEY="$(cat keys/private-key.pem)"

fly deploy
```

Then point the webhook at the deployed URL:

```bash
python scripts/manage_webhook.py register https://your-app-name.fly.dev
```

### Any other container platform

The `Dockerfile` is self contained and platform agnostic. Requirements:

1. Persist `/data` so the ledger and batch counter survive restarts.
2. Run exactly one instance. The scheduler must not run in parallel copies.
3. Set `STARKBANK_PROJECT_ID` and `STARKBANK_PRIVATE_KEY` as secrets.

If you deploy somewhere that sleeps on inactivity, disable that behaviour or a
webhook arriving during the cold start will time out.

## Monitoring the 24 hour run

```bash
curl https://your-app-name.fly.dev/status
```

```json
{
  "environment": "sandbox",
  "issuer": {
    "completed_batches": 3,
    "total_batches": 8,
    "invoices_issued": 29
  },
  "events": {
    "transferred": 21,
    "skipped": 4,
    "failed": 1
  },
  "transferred_amount_cents": 1043500
}
```

Logs are JSON on stdout, one object per line, so `fly logs | jq` works directly.

## Security

1. No secret is ever hardcoded. Everything comes from the environment.
2. `keys/`, `*.pem`, `.env` and the database are gitignored.
3. Unsigned or forged webhook payloads are rejected with 401 before any
   processing happens.
4. The container runs as a non root user.

## Testing

```bash
pytest                                    # 65 tests
pytest --cov=app --cov-report=term-missing
```

The suite covers the parts where a bug costs money: fee arithmetic including the
zero and negative net cases, the idempotency guarantee under repeated delivery,
recovery of an event whose transfer failed, the retry cap, duplicate detection
before sending, signature rejection, HTTP status semantics, batch size bounds,
CPF check digit correctness, and both reconciliation sweeps.

Every test runs against an isolated SQLite file, and an autouse fixture replaces
the SDK's HTTP layer with one that raises. A test cannot reach the network even
by accident, which matters: an earlier version of this suite passed partly
because an unmocked call was landing in an exception handler.

## Notes on the API and SDK

See `NOTES.md`.

# Notes on the SDK and the API

Observations collected while building this integration. The first section holds
what was verified against the published SDK. The second is a checklist to fill
in during the 24 hour run, since some behaviour can only be confirmed against
live sandbox traffic.

## Verified against the SDK

**The README advertises Python 2.7 support.** `sdk-python` lists Python 2.7 and
3.4+ under supported versions. Python 2 reached end of life in January 2020 and
3.4 in March 2019, so this is very likely stale documentation rather than a
tested guarantee. Worth confirming whether the CI matrix still covers them.

**`event.parse` raises `InvalidSignatureError` but the signature header name is
only documented by example.** The header is `Digital-Signature`, which appears
in code samples but not as a named contract. An integrator reading only the
prose could reasonably miss it, and getting it wrong produces a signature
failure rather than a clear "missing header" error.

**Error handling documentation omits `StarkError`.** The README enumerates
`InputErrors`, `InternalServerError`, `UnknownError` and `InvalidSignatureError`.
The module also exports `Error` and `StarkError`, and their intended role in the
hierarchy is not described. That matters when writing `except` clauses, since it
is unclear whether catching `StarkError` is the recommended catch-all.

**Invoice `fee` is not documented as a field to expect on a credited log.** The
`Invoice` constructor accepts `fee`, and this challenge requires subtracting it,
but the README does not state when the field is populated or whether it can be
`None` before the invoice is paid. The implementation defensively coerces a
missing value to zero.

**`external_id` is enforced as unique, but there is no way to query by it.**
A repeated `external_id` is correctly refused. The problem is what happens next:
`transfer.query` accepts `limit, after, before, transaction_ids, status, tax_id,
sort, tags, ids` and offers no `external_ids` filter, so after an ambiguous
failure an integrator cannot ask "did my earlier attempt land?" using the very
key meant to make the operation idempotent.

That gap cost real time here. A retry that reuses the `external_id` is rejected,
which is right, but the rejection arrives as `InputErrors` carrying the bare
string `'Duplicated transfer'` — with `code` and `message` both `None`, unlike
every other error the SDK raises. Since `InputErrors` is also what a malformed
request produces, code that correctly refuses to retry `InputErrors` will record
an already-successful payment as a permanent failure.

Two things would have made this obvious rather than a multi-hour hunt: an
`external_ids` filter on `transfer.query`, and a distinct error code for a
duplicate key. The workaround adopted here is to scope the `external_id` per
attempt and keep a stable `invoice-{id}` tag, which *is* queryable, so a retry
can still tell "already paid" from "never sent".

Worth noting the failure is asynchronous: `transfer.create` returns an id and
status `created`, and only later does the transfer move to `failed`. Treating
creation as success makes an integration report money it never moved.

**The ECDSA implementation is solid, with one caveat.** `starkbank-ecdsa` derives
the nonce via RFC 6979, which removes the nonce-reuse class of key recovery; it
also range-checks `r` and `s`, verifies curve membership, rejects the point at
infinity, and normalises to low `s`. The caveat is that it is pure Python, so
big-integer arithmetic is not constant time and timing side channels are
theoretically reachable. For a client-side SDK signing with a local key that
seems an acceptable trade, but it is worth stating explicitly rather than
leaving a reader to assume constant-time behaviour.

## To confirm during the live run

- [ ] Which `log.type` values an Invoice actually emits, and whether `credited`
      is the only one that means funds landed.
- [ ] Whether `fee` is populated on the credited log or only after settlement.
- [ ] Observed webhook redelivery behaviour: how many attempts, at what interval,
      and what response codes trigger a retry.
- [ ] Whether `event.attempt.query()` surfaces anything useful when the endpoint
      is deliberately taken offline for a few minutes.
- [ ] Whether an invoice can be credited more than once (partial payments), which
      would change the fee arithmetic.

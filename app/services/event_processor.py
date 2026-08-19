"""Decides what to do with a parsed Stark Bank event.

The webhook and the reconciler both funnel through here, so the rules live in
one place.
"""

import logging
from enum import StrEnum

from app.db.database import session_scope
from app.repositories import event_repository
from app.repositories.event_repository import ClaimResult
from app.services.transfer_service import (
    TransferFailed,
    TransferSkipped,
    send_invoice_proceeds,
)

logger = logging.getLogger(__name__)

INVOICE_SUBSCRIPTION = "invoice"
TRANSFER_SUBSCRIPTION = "transfer"
CREDITED_LOG_TYPE = "credited"
TRANSFER_SETTLED_LOG_TYPES = {"success", "failed"}


class ProcessResult(StrEnum):
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    SKIPPED = "skipped"
    SENT = "sent"            # transfer accepted, outcome still pending
    SETTLED = "settled"      # a transfer event resolved an earlier send
    TRANSFERRED = "transferred"
    FAILED = "failed"
    ABANDONED = "abandoned"


#: Results that need no further attempt. A FAILED event is deliberately absent:
#: it still owes a transfer and must stay eligible for the next pass.
TERMINAL_RESULTS = frozenset(
    {
        ProcessResult.TRANSFERRED,
        ProcessResult.SENT,
        ProcessResult.SETTLED,
        ProcessResult.SKIPPED,
        ProcessResult.IGNORED,
        ProcessResult.DUPLICATE,
        ProcessResult.ABANDONED,
    }
)


def process_event(
    event, source: str = "webhook", received_grace_minutes: int | None = None
) -> ProcessResult:
    """Handle one event, end to end. Safe to call more than once per event."""
    event_id = str(event.id)
    subscription = getattr(event, "subscription", None)
    log = getattr(event, "log", None)
    log_type = getattr(log, "type", None) if log else None
    invoice = getattr(log, "invoice", None) if log else None
    invoice_id = str(invoice.id) if invoice is not None else None

    if subscription == TRANSFER_SUBSCRIPTION:
        return _settle_transfer(event_id, log, log_type)

    if subscription != INVOICE_SUBSCRIPTION:
        logger.info(
            "ignoring event from another subscription",
            extra={"event_id": event_id, "subscription": subscription},
        )
        return ProcessResult.IGNORED

    with session_scope() as session:
        claim = event_repository.claim(
            session,
            event_id=event_id,
            subscription=subscription,
            log_type=log_type,
            invoice_id=invoice_id,
            source=source,
            received_grace_minutes=received_grace_minutes,
        )

    if claim is ClaimResult.DONE:
        return ProcessResult.DUPLICATE

    if claim is ClaimResult.EXHAUSTED:
        with session_scope() as session:
            event_repository.mark_abandoned(
                session, event_id, "retry cap reached, needs manual review"
            )
        return ProcessResult.ABANDONED

    # Invoices emit several log types across their life cycle. Only the credit
    # event means money actually landed in the account.
    if log_type != CREDITED_LOG_TYPE:
        _record_skip(event_id, f"log type is '{log_type}', not '{CREDITED_LOG_TYPE}'")
        return ProcessResult.SKIPPED

    if invoice is None:
        _record_skip(event_id, "credited log carried no invoice")
        return ProcessResult.SKIPPED

    with session_scope() as session:
        attempt = event_repository.increment_attempts(session, event_id)

    try:
        outcome = send_invoice_proceeds(invoice, attempt=attempt)
    except TransferSkipped as exc:
        logger.info(
            "nothing to transfer",
            extra={"event_id": event_id, "invoice_id": invoice_id, "reason": str(exc)},
        )
        _record_skip(event_id, str(exc))
        return ProcessResult.SKIPPED
    except TransferFailed as exc:
        logger.error(
            "transfer failed",
            extra={
                "event_id": event_id,
                "invoice_id": invoice_id,
                "reason": str(exc),
                "attempt": attempt,
            },
        )
        with session_scope() as session:
            event_repository.mark_failed(session, event_id, str(exc))
        return ProcessResult.FAILED

    # Accepted, not settled. The outcome arrives later, via a transfer event or
    # the polling sweep.
    with session_scope() as session:
        event_repository.mark_sent(
            session,
            event_id=event_id,
            transfer_id=outcome.transfer_id,
            gross_amount=outcome.gross_amount,
            fee_amount=outcome.fee_amount,
            net_amount=outcome.net_amount,
        )
    return ProcessResult.SENT


def _record_skip(event_id: str, reason: str) -> None:
    with session_scope() as session:
        event_repository.mark_skipped(session, event_id, reason)


def _settle_transfer(event_id: str, log, log_type: str | None) -> ProcessResult:
    """Resolve an earlier send using the Transfer's own lifecycle event."""
    if log_type not in TRANSFER_SETTLED_LOG_TYPES:
        return ProcessResult.IGNORED

    transfer = getattr(log, "transfer", None)
    if transfer is None:
        return ProcessResult.IGNORED

    transfer_id = str(transfer.id)
    errors = getattr(log, "errors", None) or []
    reason = "; ".join(str(error) for error in errors) or f"transfer {log_type}"

    with session_scope() as session:
        updated = event_repository.settle_transfer(
            session,
            transfer_id=transfer_id,
            succeeded=(log_type == "success"),
            reason=reason,
        )

    if not updated:
        logger.info(
            "transfer event for an unknown transfer",
            extra={"event_id": event_id, "transfer_id": transfer_id},
        )
        return ProcessResult.IGNORED

    logger.info(
        "transfer settled",
        extra={
            "event_id": event_id,
            "transfer_id": transfer_id,
            "outcome": log_type,
            "reason": reason if log_type == "failed" else None,
        },
    )
    return ProcessResult.SETTLED

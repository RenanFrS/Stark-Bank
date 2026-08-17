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
CREDITED_LOG_TYPE = "credited"


class ProcessResult(StrEnum):
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    SKIPPED = "skipped"
    TRANSFERRED = "transferred"
    FAILED = "failed"
    ABANDONED = "abandoned"


#: Results that need no further attempt. A FAILED event is deliberately absent:
#: it still owes a transfer and must stay eligible for the next pass.
TERMINAL_RESULTS = frozenset(
    {
        ProcessResult.TRANSFERRED,
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
        outcome = send_invoice_proceeds(invoice)
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

    with session_scope() as session:
        event_repository.mark_transferred(
            session,
            event_id=event_id,
            transfer_id=outcome.transfer_id,
            gross_amount=outcome.gross_amount,
            fee_amount=outcome.fee_amount,
            net_amount=outcome.net_amount,
        )
    return ProcessResult.TRANSFERRED


def _record_skip(event_id: str, reason: str) -> None:
    with session_scope() as session:
        event_repository.mark_skipped(session, event_id, reason)

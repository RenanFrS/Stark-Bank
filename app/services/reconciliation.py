"""Recovers events the webhook never finished.

`run` covers deliveries that never arrived; `sweep_local_ledger` covers the ones
Stark Bank already counts as delivered. See the README on why both are needed.
"""

import logging

import starkbank

from app.config import get_settings
from app.db.database import session_scope
from app.repositories import event_repository
from app.services.event_processor import TERMINAL_RESULTS, ProcessResult, process_event

logger = logging.getLogger(__name__)


def run(limit: int = 100) -> dict[str, int]:
    """Process the events Stark Bank has not had acknowledged yet."""
    summary: dict[str, int] = {}
    scanned = 0

    for event in starkbank.event.query(limit=limit, is_delivered=False):
        scanned += 1
        try:
            result = process_event(event, source="reconciliation")
        except Exception:
            logger.exception(
                "reconciliation failed for event",
                extra={"event_id": str(event.id)},
            )
            summary["error"] = summary.get("error", 0) + 1
            continue

        summary[result.value] = summary.get(result.value, 0) + 1

        # A FAILED result is not terminal: it still owes a transfer, so it stays
        # undelivered and the next sweep picks it up again.
        if result in TERMINAL_RESULTS:
            _acknowledge(event)

    if scanned:
        logger.info(
            "reconciliation sweep finished",
            extra={"scanned": scanned, "summary": summary},
        )
    return summary


def sweep_local_ledger(
    stale_after_minutes: int | None = None, limit: int = 100
) -> dict[str, int]:
    """Retry our own rows that never reached a terminal state.

    Without this, an event whose transfer failed after the webhook answered 200
    is unreachable: Stark Bank already counts it as delivered.
    """
    if stale_after_minutes is None:
        stale_after_minutes = get_settings().reconciliation_stale_after_minutes

    with session_scope() as session:
        event_ids = event_repository.unfinished(
            session, stale_after_minutes=stale_after_minutes, limit=limit
        )

    summary: dict[str, int] = {}
    for event_id in event_ids:
        event = _fetch(event_id)
        if event is None:
            summary["unfetchable"] = summary.get("unfetchable", 0) + 1
            continue

        try:
            result = process_event(
                event,
                source="local-sweep",
                received_grace_minutes=stale_after_minutes,
            )
        except Exception:
            logger.exception(
                "local sweep failed for event", extra={"event_id": event_id}
            )
            summary["error"] = summary.get("error", 0) + 1
            continue

        summary[result.value] = summary.get(result.value, 0) + 1

        if result is ProcessResult.TRANSFERRED:
            _acknowledge(event)

    if event_ids:
        logger.info(
            "local ledger sweep finished",
            extra={"candidates": len(event_ids), "summary": summary},
        )
    return summary


def _fetch(event_id: str):
    try:
        return starkbank.event.get(event_id)
    except Exception:
        logger.warning(
            "could not fetch event for the local sweep",
            extra={"event_id": event_id},
        )
        return None


def _acknowledge(event) -> None:
    try:
        starkbank.event.update(str(event.id), is_delivered=True)
    except Exception:
        # Not fatal: the local ledger already guarantees we will not double pay.
        logger.warning(
            "could not mark event as delivered",
            extra={"event_id": str(event.id)},
        )


def sweep_pending_transfers(
    stale_after_minutes: int | None = None, limit: int = 100
) -> dict[str, int]:
    """Pull the outcome of transfers whose settlement event never arrived.

    The transfer webhook is the fast path; this is the guarantee. A send left
    unresolved would otherwise be counted as neither paid nor retryable.
    """
    if stale_after_minutes is None:
        stale_after_minutes = get_settings().reconciliation_stale_after_minutes

    with session_scope() as session:
        pending = event_repository.pending_transfers(
            session, stale_after_minutes=stale_after_minutes, limit=limit
        )

    summary: dict[str, int] = {}
    for event_id, transfer_id in pending:
        try:
            transfer = starkbank.transfer.get(transfer_id)
        except Exception:
            logger.warning(
                "could not fetch transfer for settlement",
                extra={"event_id": event_id, "transfer_id": transfer_id},
            )
            summary["unfetchable"] = summary.get("unfetchable", 0) + 1
            continue

        status = getattr(transfer, "status", None)
        if status == "success":
            with session_scope() as session:
                event_repository.settle_transfer(
                    session, transfer_id=transfer_id, succeeded=True, reason=""
                )
            summary["settled"] = summary.get("settled", 0) + 1
        elif status in ("failed", "canceled"):
            with session_scope() as session:
                event_repository.settle_transfer(
                    session,
                    transfer_id=transfer_id,
                    succeeded=False,
                    reason=f"transfer {status}",
                )
            summary["reopened"] = summary.get("reopened", 0) + 1
        else:
            summary["still_pending"] = summary.get("still_pending", 0) + 1

    if pending:
        logger.info(
            "transfer settlement sweep finished",
            extra={"candidates": len(pending), "summary": summary},
        )
    return summary

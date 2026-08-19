"""Idempotency ledger.

`claim` guards the insert with the primary key so concurrent deliveries race in
the database, then resolves a collision by reading the existing row's status —
losing the race does not mean the work finished.
"""

import logging
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import PENDING_STATUSES, TERMINAL_STATUSES, EventStatus, ProcessedEvent

logger = logging.getLogger(__name__)


class ClaimResult(StrEnum):
    CLAIMED = "claimed"      # first sighting
    RESUMABLE = "resumable"  # seen before, still owes a transfer
    EXHAUSTED = "exhausted"  # out of attempts, needs manual intervention
    DONE = "done"            # already finished


def _as_utc(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes even for timezone-aware columns."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def claim(
    session: Session,
    event_id: str,
    subscription: str | None = None,
    log_type: str | None = None,
    invoice_id: str | None = None,
    source: str = "webhook",
    received_grace_minutes: int | None = None,
) -> ClaimResult:
    """Try to take ownership of an event id.

    A `RECEIVED` row is only resumable once `received_grace_minutes` has passed,
    because a background task may still be working on it. A `FAILED` row is
    resumable immediately: the outcome is already known.
    """
    record = ProcessedEvent(
        event_id=event_id,
        subscription=subscription,
        log_type=log_type,
        invoice_id=invoice_id,
        status=EventStatus.RECEIVED,
        source=source,
    )
    session.add(record)
    try:
        session.commit()
        return ClaimResult.CLAIMED
    except IntegrityError:
        session.rollback()

    return _resolve_collision(session, event_id, source, received_grace_minutes)


def _resolve_collision(
    session: Session,
    event_id: str,
    source: str,
    received_grace_minutes: int | None,
) -> ClaimResult:
    existing = session.get(ProcessedEvent, event_id)
    if existing is None:
        # Deleted between the failed insert and this read. Treat as unfinished.
        return ClaimResult.RESUMABLE

    settings = get_settings()
    if received_grace_minutes is None:
        received_grace_minutes = settings.reconciliation_stale_after_minutes

    if existing.status == EventStatus.ABANDONED:
        # Reported as exhausted rather than as a duplicate, so an event needing
        # manual review keeps saying so on every pass instead of going quiet.
        return ClaimResult.EXHAUSTED

    if existing.status in PENDING_STATUSES:
        # A transfer is already in flight for this event. Re-sending would risk
        # paying twice; the settlement sweep resolves it.
        return ClaimResult.DONE

    if existing.status in TERMINAL_STATUSES:
        logger.info(
            "event already settled, skipping",
            extra={
                "event_id": event_id,
                "status": existing.status,
                "source": source,
            },
        )
        return ClaimResult.DONE

    if existing.attempts >= settings.transfer_max_attempts:
        logger.error(
            "event exhausted its retries",
            extra={
                "event_id": event_id,
                "attempts": existing.attempts,
                "max_attempts": settings.transfer_max_attempts,
            },
        )
        return ClaimResult.EXHAUSTED

    if existing.status == EventStatus.RECEIVED:
        age = datetime.now(timezone.utc) - _as_utc(existing.updated_at)
        if age < timedelta(minutes=received_grace_minutes):
            logger.info(
                "event still in flight, leaving it alone",
                extra={"event_id": event_id, "age_seconds": int(age.total_seconds())},
            )
            return ClaimResult.DONE

    logger.info(
        "resuming an unfinished event",
        extra={
            "event_id": event_id,
            "status": existing.status,
            "attempts": existing.attempts,
            "source": source,
        },
    )
    return ClaimResult.RESUMABLE


def unfinished(
    session: Session, stale_after_minutes: int, limit: int = 100
) -> list[str]:
    """Event ids that still owe work.

    `FAILED` rows qualify at once. `RECEIVED` rows only after the grace window,
    so a delivery still being processed in the background is left alone.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    rows = session.execute(
        select(ProcessedEvent.event_id, ProcessedEvent.status, ProcessedEvent.updated_at)
        .where(ProcessedEvent.status.in_([EventStatus.FAILED, EventStatus.RECEIVED]))
        .where(ProcessedEvent.attempts < get_settings().transfer_max_attempts)
        .order_by(ProcessedEvent.updated_at)
        .limit(limit)
    ).all()

    return [
        event_id
        for event_id, status, updated_at in rows
        if status == EventStatus.FAILED or _as_utc(updated_at) <= cutoff
    ]


def get(session: Session, event_id: str) -> Optional[ProcessedEvent]:
    return session.get(ProcessedEvent, event_id)


def mark_skipped(session: Session, event_id: str, reason: str) -> None:
    _update(session, event_id, status=EventStatus.SKIPPED, detail=reason)


def mark_failed(session: Session, event_id: str, reason: str) -> None:
    _update(session, event_id, status=EventStatus.FAILED, detail=reason)


def mark_abandoned(session: Session, event_id: str, reason: str) -> None:
    _update(session, event_id, status=EventStatus.ABANDONED, detail=reason)


def mark_sent(
    session: Session,
    event_id: str,
    transfer_id: str,
    gross_amount: int,
    fee_amount: int,
    net_amount: int,
) -> None:
    """Record that the API accepted the Transfer. Not that it completed."""
    _update(
        session,
        event_id,
        status=EventStatus.SENT,
        transfer_id=transfer_id,
        gross_amount=gross_amount,
        fee_amount=fee_amount,
        net_amount=net_amount,
        detail=None,
    )


def settle_transfer(
    session: Session, transfer_id: str, succeeded: bool, reason: str
) -> bool:
    """Resolve the row waiting on this transfer. Returns False if none is."""
    record = session.execute(
        select(ProcessedEvent).where(ProcessedEvent.transfer_id == transfer_id)
    ).scalar_one_or_none()
    if record is None:
        return False
    if record.status == EventStatus.TRANSFERRED:
        return True

    record.status = EventStatus.TRANSFERRED if succeeded else EventStatus.FAILED
    record.detail = None if succeeded else reason
    session.commit()
    return True


def pending_transfers(
    session: Session, stale_after_minutes: int, limit: int = 100
) -> list[tuple[str, str]]:
    """(event_id, transfer_id) for sends still awaiting an outcome."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    rows = session.execute(
        select(ProcessedEvent.event_id, ProcessedEvent.transfer_id, ProcessedEvent.updated_at)
        .where(ProcessedEvent.status == EventStatus.SENT)
        .where(ProcessedEvent.transfer_id.is_not(None))
        .order_by(ProcessedEvent.updated_at)
        .limit(limit)
    ).all()
    return [
        (event_id, transfer_id)
        for event_id, transfer_id, updated_at in rows
        if _as_utc(updated_at) <= cutoff
    ]


def mark_transferred(
    session: Session,
    event_id: str,
    transfer_id: str,
    gross_amount: int,
    fee_amount: int,
    net_amount: int,
) -> None:
    _update(
        session,
        event_id,
        status=EventStatus.TRANSFERRED,
        transfer_id=transfer_id,
        gross_amount=gross_amount,
        fee_amount=fee_amount,
        net_amount=net_amount,
        detail=None,
    )


def increment_attempts(session: Session, event_id: str) -> int:
    record = session.get(ProcessedEvent, event_id)
    if record is None:
        return 0
    record.attempts += 1
    session.commit()
    return record.attempts


def _update(session: Session, event_id: str, **fields) -> None:
    record = session.get(ProcessedEvent, event_id)
    if record is None:
        logger.warning("cannot update unknown event", extra={"event_id": event_id})
        return
    for key, value in fields.items():
        setattr(record, key, value)
    session.commit()

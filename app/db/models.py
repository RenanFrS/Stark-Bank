"""Tables: the event ledger, the invoices issued, and the batch counter."""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class EventStatus(StrEnum):
    RECEIVED = "received"
    SKIPPED = "skipped"
    TRANSFERRED = "transferred"
    FAILED = "failed"
    # Past the retry cap. Needs a human, so it is never retried automatically.
    ABANDONED = "abandoned"


#: Statuses that need no further work. Anything else is eligible for a retry.
TERMINAL_STATUSES = frozenset(
    {EventStatus.TRANSFERRED, EventStatus.SKIPPED, EventStatus.ABANDONED}
)


class ProcessedEvent(Base):
    """One row per Stark Bank event id, which doubles as the idempotency key."""

    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscription: Mapped[str | None] = mapped_column(String(64))
    log_type: Mapped[str | None] = mapped_column(String(64))
    invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=EventStatus.RECEIVED, nullable=False
    )
    transfer_id: Mapped[str | None] = mapped_column(String(64))
    gross_amount: Mapped[int | None] = mapped_column(BigInteger)
    fee_amount: Mapped[int | None] = mapped_column(BigInteger)
    net_amount: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="webhook", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class IssuedInvoice(Base):
    __tablename__ = "issued_invoices"

    invoice_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    payer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class IssuerBatch(Base):
    __tablename__ = "issuer_batches"
    __table_args__ = (UniqueConstraint("batch_number", name="uq_issuer_batch_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    invoice_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

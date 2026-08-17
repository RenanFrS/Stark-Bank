"""Audit trail for what the issuer emitted."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import IssuedInvoice, IssuerBatch


def record_batch(
    session: Session, batch_number: int, invoices: list[IssuedInvoice]
) -> None:
    session.add(IssuerBatch(batch_number=batch_number, invoice_count=len(invoices)))
    session.add_all(invoices)
    session.commit()


def completed_batches(session: Session) -> int:
    result = session.execute(select(func.count()).select_from(IssuerBatch)).scalar_one()
    return int(result)


def next_batch_number(session: Session) -> int:
    current = session.execute(select(func.max(IssuerBatch.batch_number))).scalar_one()
    return (current or 0) + 1


def total_invoices(session: Session) -> int:
    result = session.execute(
        select(func.count()).select_from(IssuedInvoice)
    ).scalar_one()
    return int(result)

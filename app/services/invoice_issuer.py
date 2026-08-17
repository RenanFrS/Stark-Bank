"""Issues a batch of invoices to random people.

The batch counter is persisted so a restart mid window resumes instead of
replaying the schedule.
"""

import logging
import random
from dataclasses import dataclass

import starkbank
from starkbank.error import InputErrors

from app.config import get_settings
from app.db.database import session_scope
from app.db.models import IssuedInvoice
from app.repositories import invoice_repository
from app.utils.people import random_payer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchResult:
    batch_number: int
    requested: int
    created: int
    skipped_reason: str | None = None


def build_invoices(count: int, rng: random.Random | None = None) -> list:
    """Create the Invoice payloads for one batch, without calling the API."""
    settings = get_settings()
    rng = rng or random

    invoices = []
    for _ in range(count):
        payer = random_payer(rng)
        invoices.append(
            starkbank.Invoice(
                amount=rng.randint(settings.issuer_min_amount, settings.issuer_max_amount),
                name=payer.name,
                tax_id=payer.tax_id,
                expiration=settings.issuer_expiration_seconds,
                tags=["challenge", "auto-issued"],
            )
        )
    return invoices


def pick_batch_size(rng: random.Random | None = None) -> int:
    settings = get_settings()
    rng = rng or random
    return rng.randint(settings.issuer_min_invoices, settings.issuer_max_invoices)


def issue_batch(rng: random.Random | None = None) -> BatchResult:
    """Emit one batch, unless the configured total has already been reached."""
    settings = get_settings()

    with session_scope() as session:
        already_done = invoice_repository.completed_batches(session)
        batch_number = invoice_repository.next_batch_number(session)

    if already_done >= settings.issuer_total_batches:
        logger.info(
            "issuer finished, no batch emitted",
            extra={
                "completed_batches": already_done,
                "total_batches": settings.issuer_total_batches,
            },
        )
        return BatchResult(
            batch_number=batch_number,
            requested=0,
            created=0,
            skipped_reason="issuer already completed all batches",
        )

    count = pick_batch_size(rng)
    payloads = build_invoices(count, rng)

    try:
        created = starkbank.invoice.create(payloads)
    except InputErrors as exc:
        messages = "; ".join(f"{error.code}: {error.message}" for error in exc.errors)
        logger.error(
            "invoice batch rejected",
            extra={"batch_number": batch_number, "errors": messages},
        )
        raise

    records = [
        IssuedInvoice(
            invoice_id=str(invoice.id),
            batch_number=batch_number,
            payer_name=invoice.name,
            amount=int(invoice.amount),
        )
        for invoice in created
    ]

    with session_scope() as session:
        invoice_repository.record_batch(session, batch_number, records)

    logger.info(
        "invoice batch issued",
        extra={
            "batch_number": batch_number,
            "invoice_count": len(created),
            "total_amount": sum(record.amount for record in records),
        },
    )
    return BatchResult(batch_number=batch_number, requested=count, created=len(created))

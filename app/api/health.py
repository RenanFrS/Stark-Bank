"""`/health` for the platform probe, `/status` for a human."""

from fastapi import APIRouter
from sqlalchemy import func, select

from app.config import get_settings
from app.db.database import session_scope
from app.db.models import EventStatus, ProcessedEvent
from app.repositories import invoice_repository

router = APIRouter(tags=["ops"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/status")
def run_status() -> dict:
    settings = get_settings()

    with session_scope() as session:
        batches = invoice_repository.completed_batches(session)
        invoices = invoice_repository.total_invoices(session)

        by_status = dict(
            session.execute(
                select(ProcessedEvent.status, func.count())
                .group_by(ProcessedEvent.status)
            ).all()
        )
        transferred_total = session.execute(
            select(func.coalesce(func.sum(ProcessedEvent.net_amount), 0)).where(
                ProcessedEvent.status == EventStatus.TRANSFERRED
            )
        ).scalar_one()

    return {
        "environment": settings.starkbank_environment,
        "issuer": {
            "completed_batches": batches,
            "total_batches": settings.issuer_total_batches,
            "invoices_issued": invoices,
        },
        "events": by_status,
        "transferred_amount_cents": int(transferred_total),
    }

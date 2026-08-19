"""Background jobs: the invoice issuer and the reconciliation sweeps."""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.services import reconciliation
from app.services.invoice_issuer import issue_batch

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _safe_issue_batch() -> None:
    try:
        issue_batch()
    except Exception:
        logger.exception("invoice batch job raised")


def _safe_reconcile() -> None:
    # The two sweeps cover different gaps, so a failure in one must not stop the
    # other from running.
    try:
        reconciliation.run()
    except Exception:
        logger.exception("reconciliation job raised")
    try:
        reconciliation.sweep_local_ledger()
    except Exception:
        logger.exception("local ledger sweep raised")
    try:
        reconciliation.sweep_pending_transfers()
    except Exception:
        logger.exception("transfer settlement sweep raised")


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    scheduler = BackgroundScheduler(timezone="UTC")

    if settings.issuer_enabled:
        scheduler.add_job(
            _safe_issue_batch,
            trigger=IntervalTrigger(hours=settings.issuer_interval_hours),
            id="issue_invoices",
            # Fire the first batch a few seconds after boot instead of waiting
            # a full interval, so the 24 hour window starts immediately.
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
        logger.info(
            "issuer job scheduled",
            extra={
                "interval_hours": settings.issuer_interval_hours,
                "total_batches": settings.issuer_total_batches,
            },
        )

    if settings.reconciliation_enabled:
        scheduler.add_job(
            _safe_reconcile,
            trigger=IntervalTrigger(minutes=settings.reconciliation_interval_minutes),
            id="reconcile",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        logger.info(
            "reconciliation job scheduled",
            extra={"interval_minutes": settings.reconciliation_interval_minutes},
        )

    scheduler.start()
    _scheduler = scheduler
    return scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

"""Application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import scheduler
from app.api import health, webhook
from app.config import get_settings
from app.db.database import init_db
from app.logging_config import configure_logging
from app.starkbank_client import configure_sdk

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    configure_sdk()
    scheduler.start()
    logger.info("application started")
    try:
        yield
    finally:
        scheduler.shutdown()
        logger.info("application stopped")


def create_app() -> FastAPI:
    application = FastAPI(
        title="Stark Bank Invoice to Transfer",
        description=(
            "Issues invoices on a schedule and forwards each credited amount, "
            "net of fees, to the destination account."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    application.include_router(health.router)
    application.include_router(webhook.router)
    return application


app = create_app()

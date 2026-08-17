"""Loads the Project credential and installs it as the SDK default user."""

import logging
from functools import lru_cache

import starkbank

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_project() -> starkbank.Project:
    settings = get_settings()
    if not settings.starkbank_project_id:
        raise RuntimeError("STARKBANK_PROJECT_ID is not configured.")

    project = starkbank.Project(
        environment=settings.starkbank_environment,
        id=settings.starkbank_project_id,
        private_key=settings.resolve_private_key(),
    )
    logger.info(
        "starkbank project loaded",
        extra={
            "environment": settings.starkbank_environment,
            "project_id": settings.starkbank_project_id,
        },
    )
    return project


def configure_sdk() -> None:
    """Install the project as the SDK default user. Call once at startup."""
    starkbank.user = get_project()
    starkbank.language = "en-US"

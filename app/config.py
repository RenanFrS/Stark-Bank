"""Settings from the environment.

The private key comes either inline or from a file path, so the same code runs
on a laptop and in a container.
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- Stark Bank
    starkbank_environment: str = Field(default="sandbox")
    starkbank_project_id: str = Field(default="")
    starkbank_private_key: Optional[str] = Field(default=None)
    starkbank_private_key_path: Optional[str] = Field(default=None)

    # ---------------------------------------------------------------- Storage
    database_url: str = Field(default="sqlite:///./data/app.db")

    # ---------------------------------------------------------------- Issuer job
    issuer_enabled: bool = Field(default=True)
    issuer_interval_hours: float = Field(default=3.0)
    issuer_total_batches: int = Field(default=8)
    issuer_min_invoices: int = Field(default=8)
    issuer_max_invoices: int = Field(default=12)
    issuer_min_amount: int = Field(default=1_000)      # R$ 10,00 in cents
    issuer_max_amount: int = Field(default=100_000)    # R$ 1.000,00 in cents
    issuer_expiration_seconds: int = Field(default=86_400)

    # ---------------------------------------------------------------- Reconciler
    reconciliation_enabled: bool = Field(default=True)
    reconciliation_interval_minutes: int = Field(default=15)
    # How long a row may sit in RECEIVED before the sweep assumes the background
    # task that owned it died. Shorter than the interval would race the webhook.
    reconciliation_stale_after_minutes: int = Field(default=15)

    # ---------------------------------------------------------------- Destination
    # Values supplied by the challenge statement.
    transfer_bank_code: str = Field(default="20018183")
    transfer_branch_code: str = Field(default="0001")
    transfer_account_number: str = Field(default="6341320293482496")
    transfer_account_type: str = Field(default="payment")
    transfer_name: str = Field(default="Stark Bank S.A.")
    transfer_tax_id: str = Field(default="20.018.183/0001-80")

    # ---------------------------------------------------------------- Resilience
    transfer_max_attempts: int = Field(default=4)
    transfer_backoff_seconds: float = Field(default=2.0)

    # ---------------------------------------------------------------- Misc
    log_level: str = Field(default="INFO")
    webhook_public_url: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def _validate_ranges(self) -> "Settings":
        if self.issuer_min_invoices > self.issuer_max_invoices:
            raise ValueError("issuer_min_invoices cannot exceed issuer_max_invoices")
        if self.issuer_min_amount > self.issuer_max_amount:
            raise ValueError("issuer_min_amount cannot exceed issuer_max_amount")
        return self

    def resolve_private_key(self) -> str:
        """Return the PEM content of the ECDSA private key."""
        if self.starkbank_private_key:
            # Environment variables cannot hold real newlines on most platforms,
            # so we accept the escaped form as well.
            return self.starkbank_private_key.replace("\\n", "\n")
        if self.starkbank_private_key_path:
            return Path(self.starkbank_private_key_path).read_text(encoding="utf-8")
        raise RuntimeError(
            "No private key configured. Set STARKBANK_PRIVATE_KEY or "
            "STARKBANK_PRIVATE_KEY_PATH."
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

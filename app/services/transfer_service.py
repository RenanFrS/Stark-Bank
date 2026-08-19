"""Sends the proceeds of a credited invoice onward.

The invoice id is mirrored into a tag because `transfer.query` cannot filter on
`external_id`, which is the only way a retry can tell "already paid" from
"failed". See NOTES.md.
"""

import logging
import time
from dataclasses import dataclass

import starkbank
from starkbank.error import InputErrors, InternalServerError, UnknownError

from app.config import get_settings

logger = logging.getLogger(__name__)

TRANSFER_EXTERNAL_ID_PREFIX = "invoice"
BASE_TAGS = ["challenge", "invoice-proceeds"]


class TransferSkipped(Exception):
    """Raised when a transfer is intentionally not created."""


class TransferFailed(Exception):
    """Raised when a transfer could not be created after every attempt."""


@dataclass(frozen=True)
class TransferOutcome:
    transfer_id: str
    gross_amount: int
    fee_amount: int
    net_amount: int
    external_id: str


def build_invoice_tag(invoice_id: str) -> str:
    """Stable across attempts: this is what identifies the payment."""
    return f"{TRANSFER_EXTERNAL_ID_PREFIX}-{invoice_id}"


def build_external_id(invoice_id: str, attempt: int = 1) -> str:
    """Unique per attempt.

    The API refuses a repeated external_id, so reusing it after a failure makes
    every retry fail as a duplicate. Duplicate protection therefore rests on the
    local ledger and on the tag lookup below, which both survive a retry.
    """
    tag = build_invoice_tag(invoice_id)
    return tag if attempt <= 1 else f"{tag}-r{attempt}"


def compute_net_amount(gross_amount: int, fee_amount: int) -> int:
    """Amount to forward, in cents. Money is never a float."""
    if gross_amount < 0:
        raise ValueError("gross_amount cannot be negative")
    if fee_amount < 0:
        raise ValueError("fee_amount cannot be negative")
    return gross_amount - fee_amount


#: A Transfer in this state moved no money, so it must not block a new attempt.
DEAD_TRANSFER_STATUSES = frozenset({"failed", "canceled"})


def find_existing_transfer(invoice_id: str):
    """Return a live Transfer already sent for this invoice, or None.

    A failed one is ignored on purpose: treating it as proof of payment would
    report success while the money never left.

    A lookup failure returns None, because refusing to pay when we could not
    check would strand the money, and `external_id` still guards a real
    duplicate.
    """
    tag = build_invoice_tag(invoice_id)
    try:
        for transfer in starkbank.transfer.query(tags=[tag]):
            if getattr(transfer, "status", None) in DEAD_TRANSFER_STATUSES:
                continue
            return transfer
    except Exception:
        logger.warning(
            "could not check for an existing transfer, proceeding",
            extra={"invoice_id": invoice_id},
        )
    return None


def send_invoice_proceeds(invoice, attempt: int = 1) -> TransferOutcome:
    """Create the Transfer for a credited invoice.

    Raises TransferSkipped when nothing should be sent, TransferFailed when the
    API rejected or could not process the request.
    """
    settings = get_settings()

    invoice_id = str(invoice.id)
    gross_amount = int(getattr(invoice, "amount", 0) or 0)
    fee_amount = int(getattr(invoice, "fee", 0) or 0)
    net_amount = compute_net_amount(gross_amount, fee_amount)
    external_id = build_external_id(invoice_id, attempt)
    invoice_tag = build_invoice_tag(invoice_id)

    if net_amount <= 0:
        raise TransferSkipped(
            f"net amount is {net_amount} cents "
            f"(gross={gross_amount}, fee={fee_amount})"
        )

    already_sent = find_existing_transfer(invoice_id)
    if already_sent is not None:
        logger.info(
            "transfer already exists for this invoice, reusing it",
            extra={
                "transfer_id": str(already_sent.id),
                "invoice_id": invoice_id,
                "external_id": external_id,
            },
        )
        return TransferOutcome(
            transfer_id=str(already_sent.id),
            gross_amount=gross_amount,
            fee_amount=fee_amount,
            net_amount=net_amount,
            external_id=external_id,
        )

    transfer = starkbank.Transfer(
        amount=net_amount,
        bank_code=settings.transfer_bank_code,
        branch_code=settings.transfer_branch_code,
        account_number=settings.transfer_account_number,
        account_type=settings.transfer_account_type,
        tax_id=settings.transfer_tax_id,
        name=settings.transfer_name,
        external_id=external_id,
        description=f"Invoice {invoice_id} proceeds",
        tags=BASE_TAGS + [invoice_tag],
    )

    created = _create_with_retry(transfer, invoice_id, external_id)

    logger.info(
        "transfer created",
        extra={
            "transfer_id": created.id,
            "invoice_id": invoice_id,
            "gross_amount": gross_amount,
            "fee_amount": fee_amount,
            "net_amount": net_amount,
            "external_id": external_id,
        },
    )
    return TransferOutcome(
        transfer_id=str(created.id),
        gross_amount=gross_amount,
        fee_amount=fee_amount,
        net_amount=net_amount,
        external_id=external_id,
    )


def _create_with_retry(transfer, invoice_id: str, external_id: str):
    """Retry only what is worth retrying.

    InputErrors means the request itself is wrong, so retrying reproduces the
    same rejection. The one exception is a rejection caused by our own earlier
    attempt having succeeded, which the tag lookup can confirm.
    """
    settings = get_settings()
    last_error: Exception | None = None

    for attempt in range(1, settings.transfer_max_attempts + 1):
        try:
            return starkbank.transfer.create([transfer])[0]
        except InputErrors as exc:
            messages = "; ".join(
                f"{error.code}: {error.message}" for error in exc.errors
            )
            raced = find_existing_transfer(invoice_id)
            if raced is not None:
                logger.info(
                    "rejection was our own duplicate, transfer already exists",
                    extra={"external_id": external_id, "transfer_id": str(raced.id)},
                )
                return raced
            logger.error(
                "transfer rejected by the API, not retrying",
                extra={"external_id": external_id, "errors": messages},
            )
            raise TransferFailed(messages) from exc
        except (InternalServerError, UnknownError) as exc:
            last_error = exc
            delay = settings.transfer_backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "transient failure creating transfer, retrying",
                extra={
                    "external_id": external_id,
                    "attempt": attempt,
                    "max_attempts": settings.transfer_max_attempts,
                    "retry_in_seconds": delay,
                },
            )
            if attempt < settings.transfer_max_attempts:
                time.sleep(delay)

    raise TransferFailed(
        f"exhausted {settings.transfer_max_attempts} attempts: {last_error}"
    )

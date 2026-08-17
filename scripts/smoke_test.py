"""Verify credentials before running anything else.

Fetches the balance and issues one invoice, then prints its id. If this works,
your Project ID and private key are correct.

    python scripts/smoke_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import starkbank  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.starkbank_client import configure_sdk  # noqa: E402
from app.utils.people import random_payer  # noqa: E402


def main() -> None:
    configure_logging(get_settings().log_level)
    configure_sdk()

    balance = starkbank.balance.get()
    print(f"Balance: {balance.amount} cents ({balance.currency})")

    payer = random_payer()
    invoice = starkbank.invoice.create([
        starkbank.Invoice(
            amount=1_000,
            name=payer.name,
            tax_id=payer.tax_id,
            tags=["smoke-test"],
        )
    ])[0]

    print(f"Invoice created: {invoice.id}")
    print(f"  payer:  {invoice.name} ({invoice.tax_id})")
    print(f"  amount: {invoice.amount} cents")
    print(f"  status: {invoice.status}")
    print()
    print("Sandbox pays most invoices automatically. Wait a few minutes and")
    print("check the invoice logs to confirm a 'credited' event was produced.")


if __name__ == "__main__":
    main()

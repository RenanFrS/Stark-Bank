"""Register, list or delete the webhook subscription.

    python scripts/manage_webhook.py list
    python scripts/manage_webhook.py register https://your-app.fly.dev
    python scripts/manage_webhook.py delete <webhook_id>

The URL passed to `register` is the base URL: the script appends the endpoint
path so it always matches what the application actually serves.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import starkbank  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.starkbank_client import configure_sdk  # noqa: E402

WEBHOOK_PATH = "/webhooks/starkbank"
SUBSCRIPTIONS = ["invoice"]


def register(base_url: str) -> None:
    url = base_url.rstrip("/") + WEBHOOK_PATH
    if not url.startswith("https://"):
        print(f"Refusing to register a non HTTPS url: {url}")
        sys.exit(1)

    webhook = starkbank.webhook.create(url=url, subscriptions=SUBSCRIPTIONS)
    print(f"Registered webhook {webhook.id}")
    print(f"  url:           {webhook.url}")
    print(f"  subscriptions: {webhook.subscriptions}")


def list_webhooks() -> None:
    found = False
    for webhook in starkbank.webhook.query():
        found = True
        print(f"{webhook.id}  {webhook.url}  {webhook.subscriptions}")
    if not found:
        print("No webhook subscriptions registered.")


def delete(webhook_id: str) -> None:
    starkbank.webhook.delete(webhook_id)
    print(f"Deleted webhook {webhook_id}")


def main() -> None:
    configure_logging(get_settings().log_level)
    configure_sdk()

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    if command == "list":
        list_webhooks()
    elif command == "register":
        if len(sys.argv) < 3:
            print("Usage: python scripts/manage_webhook.py register <base_url>")
            sys.exit(1)
        register(sys.argv[2])
    elif command == "delete":
        if len(sys.argv) < 3:
            print("Usage: python scripts/manage_webhook.py delete <webhook_id>")
            sys.exit(1)
        delete(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

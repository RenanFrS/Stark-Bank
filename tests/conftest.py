import os
from dataclasses import dataclass

import pytest

os.environ.setdefault("STARKBANK_PROJECT_ID", "test-project")
os.environ.setdefault("STARKBANK_PRIVATE_KEY", "test-key")
os.environ.setdefault("ISSUER_ENABLED", "false")
os.environ.setdefault("RECONCILIATION_ENABLED", "false")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Give every test its own SQLite file and a fresh schema."""
    from app.config import get_settings
    from app.db import database

    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    get_settings.cache_clear()
    database.reset_state()
    database.init_db()

    yield

    database.reset_state()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if a test reaches the real API.

    Without this, a service that gained a new SDK call would keep its tests
    green by falling into an exception handler, and the suite would quietly
    start depending on the network.
    """
    import starkcore.utils.request

    def refuse(*args, **kwargs):
        raise AssertionError(
            "a test tried to call the Stark Bank API; mock the SDK call instead"
        )

    monkeypatch.setattr(starkcore.utils.request, "fetch", refuse)


@pytest.fixture(autouse=True)
def no_existing_transfer(monkeypatch):
    """Default the duplicate lookup to "nothing found".

    Tests that care about the lookup override this; the rest get the ordinary
    case without having to know the lookup exists.
    """
    import starkbank

    monkeypatch.setattr(starkbank.transfer, "query", lambda **_kwargs: iter([]))


# ----------------------------------------------------------------- fake objects
# Minimal stand ins for the SDK resources. They only carry the attributes the
# application actually reads, which keeps the tests readable.


@dataclass
class FakeInvoice:
    id: str
    amount: int
    fee: int = 0
    name: str = "Ana Ribeiro"
    tax_id: str = "012.345.678-90"


@dataclass
class FakeLog:
    type: str
    invoice: FakeInvoice | None = None
    transfer: "FakeTransfer | None" = None
    errors: list | None = None


@dataclass
class FakeEvent:
    id: str
    subscription: str
    log: FakeLog | None


@dataclass
class FakeTransfer:
    id: str
    status: str = "created"


@pytest.fixture
def credited_event():
    def _build(event_id="event-1", invoice_id="invoice-1", amount=10_000, fee=200):
        return FakeEvent(
            id=event_id,
            subscription="invoice",
            log=FakeLog(
                type="credited",
                invoice=FakeInvoice(id=invoice_id, amount=amount, fee=fee),
            ),
        )

    return _build

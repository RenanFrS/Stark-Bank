import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import health
from app.db.database import session_scope
from app.db.models import EventStatus, IssuedInvoice, ProcessedEvent
from app.repositories import invoice_repository


@pytest.fixture
def client():
    application = FastAPI()
    application.include_router(health.router)
    return TestClient(application)


def test_health_reports_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_reflects_persisted_activity(client):
    with session_scope() as session:
        invoice_repository.record_batch(
            session,
            batch_number=1,
            invoices=[
                IssuedInvoice(
                    invoice_id="inv-1",
                    batch_number=1,
                    payer_name="Ana Ribeiro",
                    amount=10_000,
                )
            ],
        )
        session.add(
            ProcessedEvent(
                event_id="event-1",
                subscription="invoice",
                log_type="credited",
                invoice_id="inv-1",
                status=EventStatus.TRANSFERRED,
                transfer_id="transfer-1",
                gross_amount=10_000,
                fee_amount=200,
                net_amount=9_800,
            )
        )
        session.commit()

    body = client.get("/status").json()

    assert body["issuer"]["completed_batches"] == 1
    assert body["issuer"]["invoices_issued"] == 1
    assert body["events"]["transferred"] == 1
    assert body["transferred_amount_cents"] == 9_800

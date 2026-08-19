"""A created Transfer is not a completed one.

`transfer.create` returning an id only means the request was accepted. The
transfer then goes created -> processing -> success/failed asynchronously, and
the whole 24 hour run reported success while every transfer was failing.
"""

import pytest

from app.db.database import session_scope
from app.db.models import EventStatus
from app.repositories import event_repository
from app.services import event_processor, reconciliation, transfer_service
from app.services.event_processor import ProcessResult, process_event
from app.services.transfer_service import TransferOutcome
from tests.conftest import FakeEvent, FakeLog, FakeTransfer


def _outcome(transfer_id="transfer-1"):
    return TransferOutcome(
        transfer_id=transfer_id,
        gross_amount=10_000,
        fee_amount=0,
        net_amount=10_000,
        external_id="invoice-invoice-1",
    )


def _transfer_event(event_id, transfer_id, log_type):
    return FakeEvent(
        id=event_id,
        subscription="transfer",
        log=FakeLog(type=log_type, invoice=None),
    ), transfer_id


def test_a_created_transfer_is_not_yet_settled(monkeypatch, credited_event):
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _i, **_k: _outcome()
    )

    assert process_event(credited_event(event_id="ev-1")) is ProcessResult.SENT

    with session_scope() as session:
        record = event_repository.get(session, "ev-1")
    assert record.status == EventStatus.SENT
    assert record.transfer_id == "transfer-1"


def test_status_only_counts_settled_money(monkeypatch, credited_event):
    """The bug that made the run look successful: SENT counted as transferred."""
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _i, **_k: _outcome()
    )
    process_event(credited_event(event_id="ev-2"))

    from app.api import health
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(health.router)
    body = TestClient(app).get("/status").json()

    assert body["transferred_amount_cents"] == 0
    assert body["events"].get("sent") == 1
    assert body["events"].get("transferred") is None


def test_success_event_settles_the_transfer(monkeypatch, credited_event):
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _i, **_k: _outcome("t-9")
    )
    process_event(credited_event(event_id="ev-3"))

    event, _ = _transfer_event("ev-tr-ok", "t-9", "success")
    event.log.transfer = FakeTransfer(id="t-9")

    assert process_event(event) is ProcessResult.SETTLED

    with session_scope() as session:
        assert event_repository.get(session, "ev-3").status == EventStatus.TRANSFERRED


def test_failed_event_reopens_the_transfer(monkeypatch, credited_event):
    """This is what the run needed: a failed transfer must become retryable."""
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _i, **_k: _outcome("t-bad")
    )
    process_event(credited_event(event_id="ev-4"))

    event, _ = _transfer_event("ev-tr-bad", "t-bad", "failed")
    event.log.transfer = FakeTransfer(id="t-bad")
    event.log.errors = ["Duplicated transfer"]

    assert process_event(event) is ProcessResult.SETTLED

    with session_scope() as session:
        record = event_repository.get(session, "ev-4")
    assert record.status == EventStatus.FAILED
    assert "Duplicated transfer" in (record.detail or "")


def test_a_reopened_transfer_is_retried_by_the_sweep(monkeypatch, credited_event):
    calls = []

    def send(_invoice, **_kwargs):
        calls.append(1)
        return _outcome(f"t-{len(calls)}")

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", send)

    event = credited_event(event_id="ev-5")
    process_event(event)

    with session_scope() as session:
        event_repository.mark_failed(session, "ev-5", "Duplicated transfer")

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: []
    )
    monkeypatch.setattr(reconciliation.starkbank.event, "get", lambda eid: event)

    reconciliation.sweep_local_ledger(stale_after_minutes=0)

    assert len(calls) == 2, "the sweep must attempt the transfer again"


def test_pending_transfers_are_polled_when_no_event_arrives(
    monkeypatch, credited_event
):
    """The webhook can be missed, so the outcome is also pulled."""
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _i, **_k: _outcome("t-poll")
    )
    process_event(credited_event(event_id="ev-6"))

    class Settled:
        id = "t-poll"
        status = "success"

    monkeypatch.setattr(
        reconciliation.starkbank.transfer, "get", lambda tid: Settled()
    )

    summary = reconciliation.sweep_pending_transfers(stale_after_minutes=0)

    assert summary.get("settled") == 1
    with session_scope() as session:
        assert event_repository.get(session, "ev-6").status == EventStatus.TRANSFERRED


def test_polling_reopens_a_failed_transfer(monkeypatch, credited_event):
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _i, **_k: _outcome("t-poll-bad")
    )
    process_event(credited_event(event_id="ev-7"))

    class Failed:
        id = "t-poll-bad"
        status = "failed"

    monkeypatch.setattr(
        reconciliation.starkbank.transfer, "get", lambda tid: Failed()
    )

    summary = reconciliation.sweep_pending_transfers(stale_after_minutes=0)

    assert summary.get("reopened") == 1
    with session_scope() as session:
        assert event_repository.get(session, "ev-7").status == EventStatus.FAILED


def test_a_failed_transfer_is_not_reused_as_if_it_had_worked(monkeypatch):
    """`find_existing_transfer` must ignore transfers that failed.

    Otherwise a retry finds the corpse of the previous attempt and reports it as
    a success, which is exactly how the money would stay put forever.
    """

    class Failed:
        id = "t-dead"
        status = "failed"

    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "query", lambda tags: iter([Failed()])
    )

    assert transfer_service.find_existing_transfer("inv-x") is None


def test_a_live_transfer_is_still_reused(monkeypatch):
    class Processing:
        id = "t-live"
        status = "processing"

    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "query", lambda tags: iter([Processing()])
    )

    found = transfer_service.find_existing_transfer("inv-y")
    assert found is not None and found.id == "t-live"

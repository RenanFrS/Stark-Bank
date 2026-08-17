from app.db.database import session_scope
from app.db.models import EventStatus
from app.repositories import event_repository
from app.services import event_processor
from app.services.event_processor import ProcessResult, process_event
from app.services.transfer_service import TransferFailed, TransferOutcome
from tests.conftest import FakeEvent, FakeLog


def _outcome(net=9_800):
    return TransferOutcome(
        transfer_id="transfer-1",
        gross_amount=10_000,
        fee_amount=200,
        net_amount=net,
        external_id="invoice-invoice-1",
    )


def test_credited_event_creates_a_transfer(monkeypatch, credited_event):
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _invoice: _outcome()
    )

    assert process_event(credited_event()) is ProcessResult.TRANSFERRED

    with session_scope() as session:
        record = event_repository.get(session, "event-1")
        assert record.status == EventStatus.TRANSFERRED
        assert record.transfer_id == "transfer-1"
        assert record.net_amount == 9_800


def test_duplicate_delivery_does_not_transfer_twice(monkeypatch, credited_event):
    calls = 0

    def fake_send(_invoice):
        nonlocal calls
        calls += 1
        return _outcome()

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", fake_send)

    first = process_event(credited_event())
    second = process_event(credited_event())

    assert first is ProcessResult.TRANSFERRED
    assert second is ProcessResult.DUPLICATE
    assert calls == 1


def test_non_credited_log_types_are_skipped(monkeypatch, credited_event):
    called = False

    def fake_send(_invoice):
        nonlocal called
        called = True
        return _outcome()

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", fake_send)

    event = credited_event()
    event.log.type = "created"

    assert process_event(event) is ProcessResult.SKIPPED
    assert called is False

    with session_scope() as session:
        assert event_repository.get(session, "event-1").status == EventStatus.SKIPPED


def test_other_subscriptions_are_ignored_without_persisting(monkeypatch):
    event = FakeEvent(id="event-other", subscription="transfer", log=None)

    assert process_event(event) is ProcessResult.IGNORED

    with session_scope() as session:
        assert event_repository.get(session, "event-other") is None


def test_credited_log_without_invoice_is_skipped():
    event = FakeEvent(
        id="event-empty",
        subscription="invoice",
        log=FakeLog(type="credited", invoice=None),
    )

    assert process_event(event) is ProcessResult.SKIPPED


def test_failed_transfer_is_recorded_as_failed(monkeypatch, credited_event):
    def fake_send(_invoice):
        raise TransferFailed("api rejected the request")

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", fake_send)

    assert process_event(credited_event()) is ProcessResult.FAILED

    with session_scope() as session:
        record = event_repository.get(session, "event-1")
        assert record.status == EventStatus.FAILED
        assert "api rejected" in record.detail


def test_claim_is_recorded_with_its_source(monkeypatch, credited_event):
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _invoice: _outcome()
    )

    process_event(credited_event(), source="reconciliation")

    with session_scope() as session:
        assert event_repository.get(session, "event-1").source == "reconciliation"

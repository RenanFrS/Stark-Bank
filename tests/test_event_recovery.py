"""Recovery of events whose transfer did not go through on the first try.

These exercise the real `process_event`, not a stand in. Mocking it out is what
let the original suite pass while a failed transfer was silently dropped.
"""

import pytest

from app.db.database import session_scope
from app.db.models import EventStatus
from app.repositories import event_repository
from app.services import event_processor, reconciliation
from app.services.event_processor import ProcessResult, process_event
from app.services.transfer_service import TransferFailed, TransferOutcome


def _outcome(net=9_800):
    return TransferOutcome(
        transfer_id="transfer-1",
        gross_amount=10_000,
        fee_amount=200,
        net_amount=net,
        external_id="invoice-invoice-1",
    )


def _always_fails(_invoice, **_kwargs):
    raise TransferFailed("sandbox returned 500")


def test_failed_transfer_is_attempted_again(monkeypatch, credited_event):
    attempts = []

    def flaky(_invoice, **_kwargs):
        attempts.append(1)
        raise TransferFailed("sandbox returned 500")

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", flaky)

    assert process_event(credited_event(event_id="ev-retry")) is ProcessResult.FAILED
    assert process_event(credited_event(event_id="ev-retry")) is ProcessResult.FAILED
    assert len(attempts) == 2


def test_failed_event_eventually_succeeds(monkeypatch, credited_event):
    calls = []

    def fails_once(invoice, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TransferFailed("sandbox returned 500")
        return _outcome()

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", fails_once)

    assert process_event(credited_event(event_id="ev-heal")) is ProcessResult.FAILED
    assert process_event(credited_event(event_id="ev-heal")) is ProcessResult.SENT

    with session_scope() as session:
        record = event_repository.get(session, "ev-heal")
    assert record.status == EventStatus.SENT
    assert record.transfer_id == "transfer-1"


def test_transferred_event_is_never_reprocessed(monkeypatch, credited_event):
    calls = []

    def once(_invoice, **_kwargs):
        calls.append(1)
        return _outcome()

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", once)

    assert process_event(credited_event(event_id="ev-done")) is ProcessResult.SENT
    assert process_event(credited_event(event_id="ev-done")) is ProcessResult.DUPLICATE
    assert len(calls) == 1


def test_skipped_event_is_never_reprocessed(monkeypatch, credited_event):
    calls = []
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda i, **_k: calls.append(1)
    )

    event = credited_event(event_id="ev-skip")
    event.log.type = "created"

    assert process_event(event) is ProcessResult.SKIPPED
    assert process_event(event) is ProcessResult.DUPLICATE
    assert calls == []


def test_attempts_cap_abandons_the_event(monkeypatch, credited_event):
    monkeypatch.setenv("TRANSFER_MAX_ATTEMPTS", "2")

    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(event_processor, "send_invoice_proceeds", _always_fails)

    results = [
        process_event(credited_event(event_id="ev-cap")) for _ in range(4)
    ]

    assert results[0] is ProcessResult.FAILED
    assert results[1] is ProcessResult.FAILED
    # Past the cap the event stops consuming attempts and needs a human.
    assert results[2] is ProcessResult.ABANDONED
    assert results[3] is ProcessResult.ABANDONED

    with session_scope() as session:
        record = event_repository.get(session, "ev-cap")
    assert record.status == EventStatus.ABANDONED
    assert record.attempts == 2

    get_settings.cache_clear()


class TestReconciliationAcknowledgement:
    """The sweep must not tell Stark Bank it handled something it did not."""

    @pytest.fixture
    def sweep(self, monkeypatch, credited_event):
        acknowledged = []

        def _run(event, transfer):
            monkeypatch.setattr(event_processor, "send_invoice_proceeds", transfer)
            monkeypatch.setattr(
                reconciliation.starkbank.event,
                "query",
                lambda limit, is_delivered: [event],
            )
            monkeypatch.setattr(
                reconciliation.starkbank.event,
                "update",
                lambda event_id, is_delivered: acknowledged.append(event_id),
            )
            reconciliation.run()
            return acknowledged

        return _run

    def test_failed_event_stays_queued_across_sweeps(self, sweep, credited_event):
        event = credited_event(event_id="ev-queued")

        assert sweep(event, _always_fails) == []
        assert sweep(event, _always_fails) == []

    def test_event_is_acknowledged_once_it_transfers(self, sweep, credited_event):
        event = credited_event(event_id="ev-ack")

        assert sweep(event, _always_fails) == []
        assert sweep(event, lambda _i, **_k: _outcome()) == ["ev-ack"]


def test_local_sweep_recovers_an_event_the_remote_queue_lost(
    monkeypatch, credited_event
):
    """Path A: the webhook returned 200, so the event is delivered remotely,
    but the transfer failed. Only a local sweep can find it."""
    event = credited_event(event_id="ev-orphan")

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", _always_fails)
    assert process_event(event) is ProcessResult.FAILED

    # The remote queue is empty: Stark Bank considers this delivered.
    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: []
    )
    monkeypatch.setattr(
        reconciliation.starkbank.event, "get", lambda event_id: event
    )

    calls = []

    def succeeds(invoice, **_kwargs):
        calls.append(1)
        return _outcome()

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", succeeds)

    summary = reconciliation.sweep_local_ledger(stale_after_minutes=0)

    assert calls == [1]
    assert summary.get("sent") == 1

    with session_scope() as session:
        assert event_repository.get(session, "ev-orphan").status == EventStatus.SENT


def test_local_sweep_recovers_a_row_stuck_in_received(monkeypatch, credited_event):
    """Path C: the process died between the claim and the transfer."""
    event = credited_event(event_id="ev-stuck")

    with session_scope() as session:
        event_repository.claim(
            session,
            event_id="ev-stuck",
            subscription="invoice",
            log_type="credited",
            invoice_id="invoice-1",
        )

    with session_scope() as session:
        assert event_repository.get(session, "ev-stuck").status == EventStatus.RECEIVED

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: []
    )
    monkeypatch.setattr(reconciliation.starkbank.event, "get", lambda event_id: event)
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _i, **_k: _outcome()
    )

    reconciliation.sweep_local_ledger(stale_after_minutes=0)

    with session_scope() as session:
        assert event_repository.get(session, "ev-stuck").status == EventStatus.SENT


def test_local_sweep_leaves_fresh_rows_alone(monkeypatch, credited_event):
    """A row created seconds ago may still be in flight in a background task."""
    event = credited_event(event_id="ev-fresh")

    with session_scope() as session:
        event_repository.claim(
            session, event_id="ev-fresh", subscription="invoice", log_type="credited"
        )

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: []
    )

    def should_not_be_called(event_id):
        raise AssertionError("a fresh RECEIVED row must not be swept")

    monkeypatch.setattr(
        reconciliation.starkbank.event, "get", should_not_be_called
    )

    assert reconciliation.sweep_local_ledger(stale_after_minutes=15) == {}


def test_local_sweep_ignores_terminal_rows(monkeypatch, credited_event):
    monkeypatch.setattr(
        event_processor, "send_invoice_proceeds", lambda _i, **_k: _outcome()
    )
    assert process_event(credited_event(event_id="ev-final")) is ProcessResult.SENT

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: []
    )

    def should_not_be_called(event_id):
        raise AssertionError("a TRANSFERRED row must never be swept")

    monkeypatch.setattr(reconciliation.starkbank.event, "get", should_not_be_called)

    assert reconciliation.sweep_local_ledger(stale_after_minutes=0) == {}


def test_local_sweep_survives_an_unfetchable_event(monkeypatch, credited_event):
    monkeypatch.setattr(event_processor, "send_invoice_proceeds", _always_fails)
    assert process_event(credited_event(event_id="ev-gone")) is ProcessResult.FAILED

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: []
    )

    def cannot_fetch(event_id):
        raise RuntimeError("event not found")

    monkeypatch.setattr(reconciliation.starkbank.event, "get", cannot_fetch)

    summary = reconciliation.sweep_local_ledger(stale_after_minutes=0)

    assert summary == {"unfetchable": 1}


def test_local_sweep_survives_an_exploding_processor(monkeypatch, credited_event):
    event = credited_event(event_id="ev-boom")

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", _always_fails)
    assert process_event(event) is ProcessResult.FAILED

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: []
    )
    monkeypatch.setattr(reconciliation.starkbank.event, "get", lambda event_id: event)

    def exploding(*args, **kwargs):
        raise RuntimeError("database locked")

    monkeypatch.setattr(reconciliation, "process_event", exploding)

    assert reconciliation.sweep_local_ledger(stale_after_minutes=0) == {"error": 1}


def test_abandoned_event_is_acknowledged_and_not_retried(monkeypatch, credited_event):
    """An event needing manual review must stop consuming attempts."""
    monkeypatch.setenv("TRANSFER_MAX_ATTEMPTS", "1")

    from app.config import get_settings

    get_settings.cache_clear()

    attempts = []

    def failing(_invoice, **_kwargs):
        attempts.append(1)
        raise TransferFailed("permanent rejection")

    monkeypatch.setattr(event_processor, "send_invoice_proceeds", failing)

    event = credited_event(event_id="ev-manual")
    acknowledged = []

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: [event]
    )
    monkeypatch.setattr(
        reconciliation.starkbank.event,
        "update",
        lambda event_id, is_delivered: acknowledged.append(event_id),
    )

    reconciliation.run()          # attempt 1 fails
    reconciliation.run()          # cap reached, abandoned
    reconciliation.run()          # stays abandoned, no new attempt

    assert len(attempts) == 1
    assert acknowledged == ["ev-manual", "ev-manual"]

    with session_scope() as session:
        assert event_repository.get(session, "ev-manual").status == (
            EventStatus.ABANDONED
        )

    get_settings.cache_clear()

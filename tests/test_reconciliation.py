from app.services import reconciliation
from app.services.event_processor import ProcessResult


def test_sweep_processes_and_acknowledges_handled_events(
    monkeypatch, credited_event
):
    events = [credited_event(event_id="event-a"), credited_event(event_id="event-b")]
    acknowledged = []

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: events
    )
    monkeypatch.setattr(
        reconciliation.starkbank.event,
        "update",
        lambda event_id, is_delivered: acknowledged.append(event_id),
    )
    monkeypatch.setattr(
        reconciliation, "process_event", lambda _e, source: ProcessResult.TRANSFERRED
    )

    summary = reconciliation.run()

    assert summary["transferred"] == 2
    assert acknowledged == ["event-a", "event-b"]


def test_failed_events_stay_undelivered_for_the_next_sweep(
    monkeypatch, credited_event
):
    acknowledged = []

    monkeypatch.setattr(
        reconciliation.starkbank.event,
        "query",
        lambda limit, is_delivered: [credited_event(event_id="event-c")],
    )
    monkeypatch.setattr(
        reconciliation.starkbank.event,
        "update",
        lambda event_id, is_delivered: acknowledged.append(event_id),
    )
    monkeypatch.setattr(
        reconciliation, "process_event", lambda _e, source: ProcessResult.FAILED
    )

    summary = reconciliation.run()

    assert summary["failed"] == 1
    assert acknowledged == []


def test_an_exploding_event_does_not_abort_the_whole_sweep(
    monkeypatch, credited_event
):
    events = [credited_event(event_id="event-d"), credited_event(event_id="event-e")]

    monkeypatch.setattr(
        reconciliation.starkbank.event, "query", lambda limit, is_delivered: events
    )
    monkeypatch.setattr(
        reconciliation.starkbank.event, "update", lambda event_id, is_delivered: None
    )

    def flaky(event, source):
        if event.id == "event-d":
            raise RuntimeError("transient")
        return ProcessResult.TRANSFERRED

    monkeypatch.setattr(reconciliation, "process_event", flaky)

    summary = reconciliation.run()

    assert summary["error"] == 1
    assert summary["transferred"] == 1


def test_acknowledgement_failure_is_not_fatal(monkeypatch, credited_event):
    monkeypatch.setattr(
        reconciliation.starkbank.event,
        "query",
        lambda limit, is_delivered: [credited_event(event_id="event-f")],
    )

    def failing_update(event_id, is_delivered):
        raise RuntimeError("api down")

    monkeypatch.setattr(reconciliation.starkbank.event, "update", failing_update)
    monkeypatch.setattr(
        reconciliation, "process_event", lambda _e, source: ProcessResult.TRANSFERRED
    )

    summary = reconciliation.run()

    assert summary["transferred"] == 1

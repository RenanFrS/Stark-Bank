"""The reconciliation job runs two independent sweeps."""

from app import scheduler
from app.services import reconciliation


def test_both_sweeps_run(monkeypatch):
    ran = []

    monkeypatch.setattr(reconciliation, "run", lambda: ran.append("remote"))
    monkeypatch.setattr(
        reconciliation, "sweep_local_ledger", lambda: ran.append("local")
    )

    scheduler._safe_reconcile()

    assert ran == ["remote", "local"]


def test_a_failing_remote_sweep_does_not_stop_the_local_one(monkeypatch):
    """They cover different gaps, so one must not take the other down."""
    ran = []

    def exploding():
        raise RuntimeError("api unreachable")

    monkeypatch.setattr(reconciliation, "run", exploding)
    monkeypatch.setattr(
        reconciliation, "sweep_local_ledger", lambda: ran.append("local")
    )

    scheduler._safe_reconcile()

    assert ran == ["local"]


def test_a_failing_local_sweep_is_contained(monkeypatch):
    monkeypatch.setattr(reconciliation, "run", lambda: None)

    def exploding():
        raise RuntimeError("database locked")

    monkeypatch.setattr(reconciliation, "sweep_local_ledger", exploding)

    scheduler._safe_reconcile()  # must not raise


def test_a_failing_issuer_batch_is_contained(monkeypatch):
    def exploding():
        raise RuntimeError("api rejected the batch")

    monkeypatch.setattr(scheduler, "issue_batch", exploding)

    scheduler._safe_issue_batch()  # must not raise

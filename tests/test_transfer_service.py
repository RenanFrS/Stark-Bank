import pytest
from starkbank.error import InputErrors, InternalServerError

from app.services import transfer_service
from app.services.transfer_service import (
    TransferFailed,
    TransferSkipped,
    build_external_id,
    compute_net_amount,
    send_invoice_proceeds,
)
from tests.conftest import FakeInvoice, FakeTransfer


def test_net_amount_subtracts_the_fee():
    assert compute_net_amount(10_000, 250) == 9_750


def test_net_amount_with_zero_fee():
    assert compute_net_amount(10_000, 0) == 10_000


def test_net_amount_rejects_negative_input():
    with pytest.raises(ValueError):
        compute_net_amount(-1, 0)
    with pytest.raises(ValueError):
        compute_net_amount(100, -1)


def test_external_id_is_derived_from_the_invoice():
    assert build_external_id("5155165527080960") == "invoice-5155165527080960"


def test_transfer_is_skipped_when_fee_consumes_the_amount(monkeypatch):
    called = False

    def fake_create(_transfers):
        nonlocal called
        called = True
        return [FakeTransfer(id="should-not-happen")]

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", fake_create)

    with pytest.raises(TransferSkipped):
        send_invoice_proceeds(FakeInvoice(id="inv-1", amount=100, fee=100))

    assert called is False


def test_successful_transfer_returns_the_computed_amounts(monkeypatch):
    captured = {}

    def fake_create(transfers):
        captured["transfer"] = transfers[0]
        return [FakeTransfer(id="transfer-99")]

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", fake_create)

    outcome = send_invoice_proceeds(FakeInvoice(id="inv-2", amount=50_000, fee=300))

    assert outcome.transfer_id == "transfer-99"
    assert outcome.net_amount == 49_700
    assert outcome.external_id == "invoice-inv-2"
    assert captured["transfer"].amount == 49_700
    assert captured["transfer"].external_id == "invoice-inv-2"
    assert captured["transfer"].bank_code == "20018183"
    assert captured["transfer"].account_number == "6341320293482496"


def test_input_errors_are_not_retried(monkeypatch):
    attempts = 0

    def fake_create(_transfers):
        nonlocal attempts
        attempts += 1
        raise InputErrors([{"code": "invalidAmount", "message": "amount is invalid"}])

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", fake_create)

    with pytest.raises(TransferFailed):
        send_invoice_proceeds(FakeInvoice(id="inv-3", amount=10_000, fee=0))

    assert attempts == 1


def test_transient_errors_are_retried_then_succeed(monkeypatch):
    attempts = 0

    def fake_create(_transfers):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise InternalServerError("boom")
        return [FakeTransfer(id="transfer-after-retry")]

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", fake_create)
    monkeypatch.setattr(transfer_service.time, "sleep", lambda _seconds: None)

    outcome = send_invoice_proceeds(FakeInvoice(id="inv-4", amount=20_000, fee=100))

    assert attempts == 3
    assert outcome.transfer_id == "transfer-after-retry"


def test_transient_errors_eventually_give_up(monkeypatch):
    def fake_create(_transfers):
        raise InternalServerError("still down")

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", fake_create)
    monkeypatch.setattr(transfer_service.time, "sleep", lambda _seconds: None)

    with pytest.raises(TransferFailed):
        send_invoice_proceeds(FakeInvoice(id="inv-5", amount=20_000, fee=0))

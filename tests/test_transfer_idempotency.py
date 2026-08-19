"""A retry must not create a second Transfer for an invoice already paid out.

The API blocks a repeated `external_id`, but it answers with InputErrors, which
is indistinguishable from a genuinely malformed request. Without a lookup, a
transfer that already went through gets recorded as a failure.
"""

import pytest
from starkbank.error import InputErrors

from app.services import transfer_service
from app.services.transfer_service import TransferFailed, send_invoice_proceeds
from tests.conftest import FakeInvoice, FakeTransfer


def test_existing_transfer_is_reused_instead_of_recreated(monkeypatch):
    created = []

    monkeypatch.setattr(
        transfer_service.starkbank.transfer,
        "query",
        lambda tags: iter([FakeTransfer(id="transfer-already-sent")]),
    )
    monkeypatch.setattr(
        transfer_service.starkbank.transfer,
        "create",
        lambda transfers: created.append(transfers) or [FakeTransfer(id="new")],
    )

    outcome = send_invoice_proceeds(FakeInvoice(id="inv-9", amount=10_000, fee=200))

    assert outcome.transfer_id == "transfer-already-sent"
    assert created == []
    assert outcome.net_amount == 9_800


def test_transfer_is_created_when_none_exists(monkeypatch):
    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "query", lambda tags: iter([])
    )
    monkeypatch.setattr(
        transfer_service.starkbank.transfer,
        "create",
        lambda transfers: [FakeTransfer(id="transfer-fresh")],
    )

    outcome = send_invoice_proceeds(FakeInvoice(id="inv-10", amount=5_000, fee=100))

    assert outcome.transfer_id == "transfer-fresh"
    assert outcome.net_amount == 4_900


def test_lookup_carries_the_invoice_tag(monkeypatch):
    captured = {}

    def fake_query(tags):
        captured["tags"] = tags
        return iter([])

    monkeypatch.setattr(transfer_service.starkbank.transfer, "query", fake_query)
    monkeypatch.setattr(
        transfer_service.starkbank.transfer,
        "create",
        lambda transfers: [FakeTransfer(id="t")],
    )

    send_invoice_proceeds(FakeInvoice(id="inv-11", amount=5_000, fee=0))

    assert captured["tags"] == ["invoice-inv-11"]


def test_created_transfer_is_tagged_for_later_lookup(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "query", lambda tags: iter([])
    )

    def fake_create(transfers):
        captured["transfer"] = transfers[0]
        return [FakeTransfer(id="t")]

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", fake_create)

    send_invoice_proceeds(FakeInvoice(id="inv-12", amount=5_000, fee=0))

    assert "invoice-inv-12" in captured["transfer"].tags


def test_a_failing_lookup_does_not_block_the_transfer(monkeypatch):
    """If the lookup itself errors we must still attempt the payment.

    The local ledger and the external_id remain as duplicate protection.
    """

    def exploding_query(tags):
        raise RuntimeError("api unreachable")

    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "query", exploding_query
    )
    monkeypatch.setattr(
        transfer_service.starkbank.transfer,
        "create",
        lambda transfers: [FakeTransfer(id="transfer-anyway")],
    )

    outcome = send_invoice_proceeds(FakeInvoice(id="inv-13", amount=5_000, fee=0))

    assert outcome.transfer_id == "transfer-anyway"


def test_duplicate_external_id_is_not_reported_as_a_failure(monkeypatch):
    """The API rejects a repeated external_id. That means success, not failure."""
    lookups = []

    def query_finds_it_on_the_second_look(tags):
        lookups.append(tags)
        if len(lookups) == 1:
            return iter([])
        return iter([FakeTransfer(id="transfer-raced")])

    monkeypatch.setattr(
        transfer_service.starkbank.transfer,
        "query",
        query_finds_it_on_the_second_look,
    )

    def rejects_duplicate(_transfers):
        raise InputErrors(
            [{"code": "invalidExternalId", "message": "external id already used"}]
        )

    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "create", rejects_duplicate
    )

    outcome = send_invoice_proceeds(FakeInvoice(id="inv-14", amount=5_000, fee=0))

    assert outcome.transfer_id == "transfer-raced"


def test_a_real_input_error_still_fails(monkeypatch):
    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "query", lambda tags: iter([])
    )

    def rejects(_transfers):
        raise InputErrors([{"code": "invalidTaxId", "message": "tax id is invalid"}])

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", rejects)

    with pytest.raises(TransferFailed):
        send_invoice_proceeds(FakeInvoice(id="inv-15", amount=5_000, fee=0))


def test_each_attempt_uses_a_fresh_external_id(monkeypatch):
    """A retry must not reuse the external_id of a failed attempt.

    The API rejects the repeat as a duplicate, so every retry was guaranteed to
    fail while the invoice tag still identifies the payment.
    """
    seen = []

    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "query", lambda tags: iter([])
    )

    def capture(transfers):
        seen.append(transfers[0].external_id)
        return [FakeTransfer(id=f"t{len(seen)}")]

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", capture)

    inv = FakeInvoice(id="inv-retry", amount=5_000, fee=0)
    transfer_service.send_invoice_proceeds(inv, attempt=1)
    transfer_service.send_invoice_proceeds(inv, attempt=2)

    assert len(set(seen)) == 2, f"external_ids repeated across attempts: {seen}"


def test_the_invoice_tag_is_stable_across_attempts(monkeypatch):
    """The tag is what identifies the payment, so it must not change."""
    tags_seen = []

    monkeypatch.setattr(
        transfer_service.starkbank.transfer, "query", lambda tags: iter([])
    )

    def capture(transfers):
        tags_seen.append(transfers[0].tags)
        return [FakeTransfer(id="t")]

    monkeypatch.setattr(transfer_service.starkbank.transfer, "create", capture)

    inv = FakeInvoice(id="inv-tag", amount=5_000, fee=0)
    transfer_service.send_invoice_proceeds(inv, attempt=1)
    transfer_service.send_invoice_proceeds(inv, attempt=7)

    assert "invoice-inv-tag" in tags_seen[0]
    assert "invoice-inv-tag" in tags_seen[1]

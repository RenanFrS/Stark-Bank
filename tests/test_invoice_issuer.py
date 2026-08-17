import random

from app.config import get_settings
from app.db.database import session_scope
from app.repositories import invoice_repository
from app.services import invoice_issuer
from app.utils import cpf
from tests.conftest import FakeInvoice


def _fake_created(payloads):
    return [
        FakeInvoice(
            id=f"invoice-{index}",
            amount=payload.amount,
            name=payload.name,
            tax_id=payload.tax_id,
        )
        for index, payload in enumerate(payloads)
    ]


def test_batch_size_always_within_the_required_range():
    rng = random.Random(99)
    for _ in range(300):
        size = invoice_issuer.pick_batch_size(rng)
        assert 8 <= size <= 12


def test_built_invoices_carry_valid_tax_ids_and_amounts():
    settings = get_settings()
    payloads = invoice_issuer.build_invoices(10, random.Random(3))

    assert len(payloads) == 10
    for payload in payloads:
        assert cpf.is_valid(payload.tax_id)
        assert settings.issuer_min_amount <= payload.amount <= settings.issuer_max_amount
        assert isinstance(payload.amount, int)
        assert payload.name.strip()


def test_issue_batch_persists_what_was_created(monkeypatch):
    monkeypatch.setattr(
        invoice_issuer.starkbank.invoice, "create", _fake_created
    )

    result = invoice_issuer.issue_batch(random.Random(5))

    assert result.batch_number == 1
    assert result.created == result.requested
    assert 8 <= result.created <= 12

    with session_scope() as session:
        assert invoice_repository.completed_batches(session) == 1
        assert invoice_repository.total_invoices(session) == result.created


def test_batch_numbers_increment_across_runs(monkeypatch):
    monkeypatch.setattr(
        invoice_issuer.starkbank.invoice, "create", _fake_created
    )

    # Invoice ids repeat between batches in this fake, so use a fresh id space.
    counter = {"value": 0}

    def unique_created(payloads):
        results = []
        for payload in payloads:
            counter["value"] += 1
            results.append(
                FakeInvoice(
                    id=f"invoice-{counter['value']}",
                    amount=payload.amount,
                    name=payload.name,
                    tax_id=payload.tax_id,
                )
            )
        return results

    monkeypatch.setattr(invoice_issuer.starkbank.invoice, "create", unique_created)

    first = invoice_issuer.issue_batch(random.Random(1))
    second = invoice_issuer.issue_batch(random.Random(2))

    assert first.batch_number == 1
    assert second.batch_number == 2


def test_issuer_stops_after_the_configured_total(monkeypatch):
    monkeypatch.setenv("ISSUER_TOTAL_BATCHES", "1")
    get_settings.cache_clear()

    counter = {"value": 0}

    def unique_created(payloads):
        results = []
        for payload in payloads:
            counter["value"] += 1
            results.append(
                FakeInvoice(
                    id=f"invoice-{counter['value']}",
                    amount=payload.amount,
                    name=payload.name,
                    tax_id=payload.tax_id,
                )
            )
        return results

    monkeypatch.setattr(invoice_issuer.starkbank.invoice, "create", unique_created)

    first = invoice_issuer.issue_batch(random.Random(1))
    second = invoice_issuer.issue_batch(random.Random(2))

    assert first.created > 0
    assert second.created == 0
    assert second.skipped_reason is not None

    get_settings.cache_clear()

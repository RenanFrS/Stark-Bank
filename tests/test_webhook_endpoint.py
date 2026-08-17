import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starkbank.error import InvalidSignatureError

from app.api import webhook
from app.services.event_processor import ProcessResult

ENDPOINT = "/webhooks/starkbank"


@pytest.fixture
def client():
    """Mount only the webhook router, so no scheduler or SDK setup runs."""
    application = FastAPI()
    application.include_router(webhook.router)
    return TestClient(application)


def test_missing_signature_header_is_rejected(client):
    response = client.post(ENDPOINT, content=b"{}")
    assert response.status_code == 401


def test_invalid_signature_is_rejected(client, monkeypatch):
    def fake_parse(content, signature):
        raise InvalidSignatureError("bad signature")

    monkeypatch.setattr(webhook.starkbank.event, "parse", fake_parse)

    response = client.post(
        ENDPOINT, content=b"{}", headers={"Digital-Signature": "forged"}
    )
    assert response.status_code == 401


def test_unparseable_payload_returns_400(client, monkeypatch):
    def fake_parse(content, signature):
        raise ValueError("malformed json")

    monkeypatch.setattr(webhook.starkbank.event, "parse", fake_parse)

    response = client.post(
        ENDPOINT, content=b"not json", headers={"Digital-Signature": "sig"}
    )
    assert response.status_code == 400


def test_transient_failure_returns_500_so_the_event_is_redelivered(
    client, monkeypatch
):
    """`event.parse` fetches Stark Bank's public key over HTTP on first use.

    A 400 there would discard a valid event, because 4xx reads as "do not
    bother retrying".
    """

    def fake_parse(content, signature):
        raise ConnectionError("could not reach the api to fetch the public key")

    monkeypatch.setattr(webhook.starkbank.event, "parse", fake_parse)

    response = client.post(
        ENDPOINT, content=b"{}", headers={"Digital-Signature": "sig"}
    )
    assert response.status_code == 500


def test_valid_signature_returns_200_and_processes(client, monkeypatch, credited_event):
    event = credited_event()
    processed = []

    monkeypatch.setattr(
        webhook.starkbank.event, "parse", lambda content, signature: event
    )
    monkeypatch.setattr(
        webhook,
        "process_event",
        lambda evt, source: processed.append((evt.id, source))
        or ProcessResult.TRANSFERRED,
    )

    response = client.post(
        ENDPOINT, content=b"{}", headers={"Digital-Signature": "valid"}
    )

    assert response.status_code == 200
    assert processed == [("event-1", "webhook")]


def test_processing_failure_still_returns_200(client, monkeypatch, credited_event):
    event = credited_event()

    monkeypatch.setattr(
        webhook.starkbank.event, "parse", lambda content, signature: event
    )

    def exploding(_event, source):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(webhook, "process_event", exploding)

    response = client.post(
        ENDPOINT, content=b"{}", headers={"Digital-Signature": "valid"}
    )
    assert response.status_code == 200

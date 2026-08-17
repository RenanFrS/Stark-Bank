"""Receives Stark Bank event callbacks.

The endpoint is public, so nothing is trusted before `starkbank.event.parse`
checks the signature. Processing happens after the response is sent, because
holding the connection open long enough would look like a failed delivery.
"""

import logging

import starkbank
from fastapi import APIRouter, BackgroundTasks, Header, Request, Response, status
from starkbank.error import InvalidSignatureError

from app.services.event_processor import process_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])


@router.post("/webhooks/starkbank")
async def receive_starkbank_event(
    request: Request,
    background_tasks: BackgroundTasks,
    digital_signature: str | None = Header(default=None, alias="Digital-Signature"),
) -> Response:
    raw_body = await request.body()

    if not digital_signature:
        logger.warning("rejected webhook without signature header")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        event = starkbank.event.parse(
            content=raw_body.decode("utf-8"),
            signature=digital_signature,
        )
    except InvalidSignatureError:
        logger.warning("rejected webhook with invalid signature")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    except (UnicodeDecodeError, ValueError):
        logger.exception("could not parse webhook payload")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    except Exception:
        # `event.parse` fetches Stark Bank's public key over HTTP on first use.
        # Answering 4xx to a transient failure there would discard a valid
        # event, because a client error reads as "do not bother retrying".
        logger.exception("could not verify webhook, asking for a redelivery")
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    logger.info(
        "webhook event accepted",
        extra={
            "event_id": str(event.id),
            "subscription": getattr(event, "subscription", None),
            "log_type": getattr(getattr(event, "log", None), "type", None),
        },
    )

    background_tasks.add_task(_process, event)
    return Response(status_code=status.HTTP_200_OK)


def _process(event) -> None:
    try:
        process_event(event, source="webhook")
    except Exception:
        # The reconciliation sweep is the safety net for anything that escapes
        # here, so a crashed background task never loses the event silently.
        logger.exception(
            "background processing raised", extra={"event_id": str(event.id)}
        )

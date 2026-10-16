"""Webhook inbox: durable insert before acknowledgement, dedup by delivery id, then processing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.capabilities import WebhookDelivery, WebhookReceiver
from wayam_connectors.contracts.errors import AuthError, ValidationError
from wayam_connectors.platform.security.redaction import redact_headers

from .db import Store


class WebhookInbox:
    def __init__(self, store: Store) -> None:
        self.store = store

    def receive(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        receiver: WebhookReceiver,
        *,
        headers: Mapping[str, str],
        body: bytes,
        secret: bytes,
    ) -> dict[str, Any]:
        """Verify signature over the raw bytes, insert durably, return {status, delivery_id, duplicate}.

        A bad signature is rejected before anything is stored. Duplicates are acknowledged (200) but
        not stored twice, and the caller is told so.
        """
        delivery = WebhookDelivery(headers=dict(headers), body=body, received_at=datetime.now(UTC))
        verdict = receiver.verify_delivery(delivery, secret)
        if not verdict.verified:
            raise AuthError("webhook signature verification failed", details={"reason": verdict.reason})
        if not verdict.delivery_id:
            raise ValidationError("webhook has no delivery id; cannot deduplicate")
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "INSERT INTO webhook_inbox (tenant_id, connection_id, delivery_id, event_type, headers, body) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, connection_id, delivery_id) DO NOTHING RETURNING id",
                (tenant_id, connection_id, verdict.delivery_id, verdict.event_type, json.dumps(redact_headers(headers)), body),
            )
            row = cur.fetchone()
        return {"status": "received", "delivery_id": verdict.delivery_id, "event_type": verdict.event_type, "duplicate": row is None}

    def pending(self, tenant_id: UUID, connection_id: UUID, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT id, delivery_id, event_type, headers, body, received_at FROM webhook_inbox "
                "WHERE tenant_id=%s AND connection_id=%s AND status='received' ORDER BY received_at LIMIT %s",
                (tenant_id, connection_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def mark(self, tenant_id: UUID, inbox_id: UUID, status: str) -> None:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "UPDATE webhook_inbox SET status=%s, processed_at=now() WHERE tenant_id=%s AND id=%s",
                (status, tenant_id, inbox_id),
            )

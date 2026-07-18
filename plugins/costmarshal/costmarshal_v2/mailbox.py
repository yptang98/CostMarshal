from __future__ import annotations

from typing import Any

from .paths import ProjectLayout
from .state import append_event, append_jsonl, ensure_mailbox, mailbox_dir, new_id, now_iso, read_jsonl


def send_message(
    layout: ProjectLayout,
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_mailbox(layout, recipient)
    ensure_mailbox(layout, sender)
    message = {
        "id": new_id("MSG"),
        "timestamp": now_iso(),
        "from": sender,
        "to": recipient,
        "subject": subject,
        "body": body,
        "task_id": task_id,
        "metadata": metadata or {},
        "delivery_state": "delivered",
        "delivered_by": "scheduler",
        "delivered_at": now_iso(),
    }
    append_jsonl(mailbox_dir(layout, recipient) / "inbox.jsonl", message)
    append_jsonl(mailbox_dir(layout, sender) / "outbox.jsonl", message)
    append_event(layout, "message_sent", message_id=message["id"], sender=sender, recipient=recipient, task_id=task_id)
    return message


def inbox_message_ids(layout: ProjectLayout, actor_id: str) -> set[str]:
    ensure_mailbox(layout, actor_id)
    ids: set[str] = set()
    for row in read_jsonl(mailbox_dir(layout, actor_id) / "inbox.jsonl"):
        message_id = row.get("id")
        if isinstance(message_id, str):
            ids.add(message_id)
    return ids


def deliver_outbox_message(
    layout: ProjectLayout,
    *,
    message: dict[str, Any],
    relayed_by: str = "scheduler",
) -> dict[str, Any]:
    sender = message.get("from")
    recipient = message.get("to")
    if not sender:
        raise ValueError("outbox message missing 'from'")
    if not recipient:
        raise ValueError("outbox message missing 'to'")
    ensure_mailbox(layout, recipient)
    delivered = dict(message)
    delivered.setdefault("id", new_id("MSG"))
    delivered.setdefault("timestamp", now_iso())
    delivered["delivery_state"] = "delivered"
    delivered["delivered_by"] = relayed_by
    delivered["delivered_at"] = now_iso()
    append_jsonl(mailbox_dir(layout, recipient) / "inbox.jsonl", delivered)
    append_event(
        layout,
        "message_relayed",
        message_id=delivered.get("id"),
        sender=sender,
        recipient=recipient,
        task_id=delivered.get("task_id"),
    )
    return delivered

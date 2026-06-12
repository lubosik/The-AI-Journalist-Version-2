"""Mention extraction and notification creation."""

from __future__ import annotations

import re
from typing import Any

from .repository import CollaborationRepository

MENTION_RE = re.compile(
    r"(?<![\w@])@([A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?)"
)


def extract_mentions(text: str) -> list[str]:
    """Return unique mentioned identifiers in their first-seen order."""
    return list(dict.fromkeys(match.lower() for match in MENTION_RE.findall(text or "")))


def record_mentions(
    repository: CollaborationRepository,
    *,
    workspace_id: str,
    actor_id: str | None,
    resource_type: str,
    resource_id: str,
    text: str,
    title: str = "You were mentioned",
    data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    identifiers = extract_mentions(text)
    members = repository.find_workspace_users(workspace_id, identifiers)
    created = []

    for member in members:
        user = member.get("users") or {}
        recipient_id = member.get("user_id") or user.get("id")
        if not recipient_id or recipient_id == actor_id:
            continue
        notification = repository.create_notification(
            recipient_id,
            "mention",
            title,
            workspace_id=workspace_id,
            actor_id=actor_id,
            body=text[:240],
            resource_type=resource_type,
            resource_id=resource_id,
            data=data,
        )
        mention = repository.create_mention(
            workspace_id,
            recipient_id,
            resource_type,
            resource_id,
            actor_id=actor_id,
            excerpt=text[:500],
            notification_id=notification["id"],
        )
        created.append({"mention": mention, "notification": notification})

    return created

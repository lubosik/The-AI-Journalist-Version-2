"""Supabase persistence helpers for collaboration features."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _default_client():
    from tools.db.client import get_client

    return get_client()


class CollaborationRepository:
    def __init__(self, client=None):
        self.client = client or _default_client()

    def create_workspace(
        self,
        name: str,
        slug: str,
        created_by: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "name": name.strip(),
            "slug": slug.strip().lower(),
            "created_by": created_by,
            "metadata": metadata or {},
        }
        result = self.client.table("workspaces").insert(row).execute()
        workspace = result.data[0]
        if created_by:
            self.upsert_membership(workspace["id"], created_by, "owner")
        return workspace

    def get_workspace_by_slug(self, slug: str) -> dict[str, Any] | None:
        result = (
            self.client.table("workspaces")
            .select("*")
            .eq("slug", slug.strip().lower())
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_or_create_workspace(
        self,
        name: str,
        slug: str,
        created_by: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.get_workspace_by_slug(slug)
        if existing:
            return existing
        return self.create_workspace(
            name=name,
            slug=slug,
            created_by=created_by,
            metadata=metadata,
        )

    def upsert_membership(
        self,
        workspace_id: str,
        user_id: str,
        role: str = "member",
        invited_by: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "role": role,
            "invited_by": invited_by,
            "updated_at": _utc_now(),
        }
        result = (
            self.client.table("workspace_memberships")
            .upsert(row, on_conflict="workspace_id,user_id")
            .execute()
        )
        return result.data[0]

    def remove_membership(self, workspace_id: str, user_id: str) -> None:
        (
            self.client.table("workspace_memberships")
            .delete()
            .eq("workspace_id", workspace_id)
            .eq("user_id", user_id)
            .execute()
        )

    def list_memberships(self, workspace_id: str) -> list[dict[str, Any]]:
        result = (
            self.client.table("workspace_memberships")
            .select("*")
            .eq("workspace_id", workspace_id)
            .execute()
        )
        return result.data or []

    def find_workspace_users(
        self, workspace_id: str, identifiers: list[str]
    ) -> list[dict[str, Any]]:
        if not identifiers:
            return []
        result = (
            self.client.table("workspace_memberships")
            .select(
                "user_id,"
                "users!workspace_memberships_user_id_fkey!inner(id,identifier)"
            )
            .eq("workspace_id", workspace_id)
            .in_("users.identifier", identifiers)
            .execute()
        )
        return result.data or []

    def create_notification(
        self,
        recipient_id: str,
        kind: str,
        title: str,
        *,
        workspace_id: str | None = None,
        actor_id: str | None = None,
        body: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self.client.table("notifications").insert(
            {
                "workspace_id": workspace_id,
                "recipient_id": recipient_id,
                "actor_id": actor_id,
                "kind": kind,
                "title": title,
                "body": body,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "data": data or {},
            }
        ).execute()
        return result.data[0]

    def create_mention(
        self,
        workspace_id: str,
        mentioned_id: str,
        resource_type: str,
        resource_id: str,
        *,
        actor_id: str | None = None,
        excerpt: str | None = None,
        notification_id: str | None = None,
    ) -> dict[str, Any]:
        result = self.client.table("mentions").upsert(
            {
                "workspace_id": workspace_id,
                "mentioned_id": mentioned_id,
                "actor_id": actor_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "excerpt": excerpt,
                "notification_id": notification_id,
            },
            on_conflict="workspace_id,mentioned_id,resource_type,resource_id",
        ).execute()
        return result.data[0]

    def mark_notification_read(self, notification_id: str, recipient_id: str) -> None:
        (
            self.client.table("notifications")
            .update({"read_at": _utc_now()})
            .eq("id", notification_id)
            .eq("recipient_id", recipient_id)
            .execute()
        )

    def mark_push_sent(self, notification_id: str) -> None:
        (
            self.client.table("notifications")
            .update({"push_sent_at": _utc_now()})
            .eq("id", notification_id)
            .execute()
        )

    def upsert_push_subscription(
        self,
        user_id: str,
        subscription: dict[str, Any],
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        keys = subscription.get("keys") or {}
        row = {
            "user_id": user_id,
            "endpoint": subscription["endpoint"],
            "p256dh": keys["p256dh"],
            "auth": keys["auth"],
            "user_agent": user_agent,
            "updated_at": _utc_now(),
        }
        result = (
            self.client.table("web_push_subscriptions")
            .upsert(row, on_conflict="endpoint")
            .execute()
        )
        return result.data[0]

    def list_push_subscriptions(self, user_id: str) -> list[dict[str, Any]]:
        result = (
            self.client.table("web_push_subscriptions")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        return result.data or []

    def delete_push_subscription(self, endpoint: str) -> None:
        (
            self.client.table("web_push_subscriptions")
            .delete()
            .eq("endpoint", endpoint)
            .execute()
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

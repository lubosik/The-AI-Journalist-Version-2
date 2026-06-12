"""VAPID-configured web-push delivery."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from .repository import CollaborationRepository


class PushConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VapidConfig:
    private_key: str
    subject: str

    @classmethod
    def from_env(cls) -> "VapidConfig":
        private_key = os.getenv("VAPID_PRIVATE_KEY", "").strip()
        subject = os.getenv("VAPID_SUBJECT", "").strip()
        if not private_key or not subject:
            raise PushConfigurationError(
                "VAPID_PRIVATE_KEY and VAPID_SUBJECT must be set"
            )
        return cls(private_key=private_key, subject=subject)


def get_vapid_public_key() -> str:
    """Return the public key an authenticated subscription endpoint can expose."""
    public_key = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    if not public_key:
        raise PushConfigurationError("VAPID_PUBLIC_KEY must be set")
    return public_key


def build_push_payload(notification: dict[str, Any]) -> str:
    return json.dumps(
        {
            "notificationId": notification["id"],
            "title": notification["title"],
            "body": notification.get("body") or "",
            "kind": notification.get("kind"),
            "data": notification.get("data") or {},
        },
        separators=(",", ":"),
    )


def send_notification_pushes(
    repository: CollaborationRepository,
    notification: dict[str, Any],
    *,
    ttl: int = 300,
    webpush_fn: Callable[..., Any] | None = None,
) -> dict[str, int]:
    config = VapidConfig.from_env()
    webpush_fn, webpush_error = _load_webpush(webpush_fn)
    subscriptions = repository.list_push_subscriptions(notification["recipient_id"])
    sent = expired = failed = 0

    for subscription in subscriptions:
        try:
            webpush_fn(
                subscription_info={
                    "endpoint": subscription["endpoint"],
                    "keys": {
                        "p256dh": subscription["p256dh"],
                        "auth": subscription["auth"],
                    },
                },
                data=build_push_payload(notification),
                vapid_private_key=config.private_key,
                vapid_claims={"sub": config.subject},
                ttl=ttl,
            )
            sent += 1
        except webpush_error as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                repository.delete_push_subscription(subscription["endpoint"])
                expired += 1
            else:
                failed += 1

    if sent:
        repository.mark_push_sent(notification["id"])
    return {"sent": sent, "expired": expired, "failed": failed}


def _load_webpush(webpush_fn):
    try:
        from pywebpush import WebPushException, webpush
    except ImportError as exc:
        if webpush_fn is None:
            raise PushConfigurationError(
                "Install pywebpush to enable web-push delivery"
            ) from exc

        class WebPushException(Exception):
            pass

        return webpush_fn, WebPushException
    return webpush_fn or webpush, WebPushException

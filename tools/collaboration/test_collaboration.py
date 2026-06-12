import os
import unittest
from unittest.mock import patch

from tools.collaboration.mentions import extract_mentions
from tools.collaboration.push import (
    PushConfigurationError,
    VapidConfig,
    build_push_payload,
    get_vapid_public_key,
    send_notification_pushes,
)


class CollaborationTests(unittest.TestCase):
    def test_mentions_are_normalised_and_deduplicated(self):
        self.assertEqual(
            extract_mentions("Ask @Dom, @lubosi and @DOM. Ignore a@b.com."),
            ["dom", "lubosi"],
        )

    def test_vapid_configuration_requires_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PushConfigurationError):
                VapidConfig.from_env()
            with self.assertRaises(PushConfigurationError):
                get_vapid_public_key()

    def test_push_payload_contains_notification_identity(self):
        payload = build_push_payload(
            {
                "id": "notification-1",
                "title": "Mention",
                "body": "Please review",
                "kind": "mention",
                "data": {"url": "/thread/1"},
            }
        )
        self.assertIn('"notificationId":"notification-1"', payload)
        self.assertIn('"url":"/thread/1"', payload)

    def test_sender_uses_vapid_environment(self):
        calls = []

        class Repository:
            def list_push_subscriptions(self, user_id):
                self.user_id = user_id
                return [
                    {
                        "endpoint": "https://push.example/subscription",
                        "p256dh": "public-key",
                        "auth": "auth-secret",
                    }
                ]

            def mark_push_sent(self, notification_id):
                self.sent_id = notification_id

        repository = Repository()

        def fake_webpush(**kwargs):
            calls.append(kwargs)

        notification = {
            "id": "notification-1",
            "recipient_id": "user-1",
            "title": "Mention",
        }
        with patch.dict(
            os.environ,
            {
                "VAPID_PRIVATE_KEY": "environment-private-key",
                "VAPID_SUBJECT": "mailto:ops@example.com",
            },
            clear=True,
        ):
            result = send_notification_pushes(
                repository, notification, webpush_fn=fake_webpush
            )

        self.assertEqual(result, {"sent": 1, "expired": 0, "failed": 0})
        self.assertEqual(repository.user_id, "user-1")
        self.assertEqual(repository.sent_id, "notification-1")
        self.assertEqual(calls[0]["vapid_private_key"], "environment-private-key")
        self.assertEqual(
            calls[0]["vapid_claims"], {"sub": "mailto:ops@example.com"}
        )


if __name__ == "__main__":
    unittest.main()

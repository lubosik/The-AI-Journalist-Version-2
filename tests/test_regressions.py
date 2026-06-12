import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.db.schema import normalise_async_database_url
from tools.db.client import normalise_supabase_url


class RegressionTests(unittest.TestCase):
    def test_asyncpg_url_removes_sslmode_and_builds_ssl_context(self):
        url, connect_args = normalise_async_database_url(
            "postgresql://user:pass@example.com/db"
            "?sslmode=require&application_name=herald"
        )
        self.assertEqual(
            url,
            "postgresql+asyncpg://user:pass@example.com/db"
            "?application_name=herald",
        )
        self.assertIn("ssl", connect_args)

    def test_asyncpg_url_keeps_disabled_ssl_disabled(self):
        url, connect_args = normalise_async_database_url(
            "postgresql+asyncpg://user:pass@example.com/db?sslmode=disable"
        )
        self.assertEqual(url, "postgresql+asyncpg://user:pass@example.com/db")
        self.assertEqual(connect_args, {})

    def test_asyncpg_prefer_does_not_force_ssl(self):
        url, connect_args = normalise_async_database_url(
            "postgresql://user:pass@example.com/db?sslmode=prefer"
        )
        self.assertEqual(url, "postgresql+asyncpg://user:pass@example.com/db")
        self.assertEqual(connect_args, {})

    def test_supabase_url_discards_rest_paths(self):
        self.assertEqual(
            normalise_supabase_url(
                "https://project.supabase.co/rest/v1/content_items/"
            ),
            "https://project.supabase.co",
        )

    def test_draft_language_routes_to_pipeline(self):
        import app

        for phrase in (
            "I want to draft an edition",
            "Okay draft the HTML and show me the preview",
            "Draft this edition",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(app.classify_intent(phrase), "draft")

    def test_official_all_in_channel_is_configured(self):
        from tools.config import YOUTUBE_CHANNELS

        all_in = next(
            channel
            for channel in YOUTUBE_CHANNELS
            if channel["name"] == "All-In Podcast"
        )
        self.assertEqual(all_in["handle"], "@allin")
        self.assertEqual(all_in["url"], "https://www.youtube.com/@allin")

    def test_edition_question_uses_live_plan(self):
        import app

        self.assertEqual(
            app.classify_intent("What editions do we have today?"),
            "view_plan",
        )

    def test_configured_users_have_distinct_persisted_identities(self):
        import app

        dom = app.HeraldSQLAlchemyDataLayer._get_configured_user("dom")
        admin = app.HeraldSQLAlchemyDataLayer._get_configured_user("lubosi")

        self.assertIsNotNone(dom)
        self.assertIsNotNone(admin)
        self.assertNotEqual(dom.id, admin.id)
        self.assertEqual(dom.metadata["role"], "client")
        self.assertEqual(admin.metadata["role"], "admin")
        self.assertIsNone(
            app.HeraldSQLAlchemyDataLayer._get_configured_user("unknown")
        )

    def test_investment_case_requests_select_the_expected_research_mode(self):
        from tools.intelligence.prompt_architecture import detect_research_mode

        self.assertEqual(
            detect_research_mode("Do deep research and build the bull case for Stripe"),
            "bull",
        )
        self.assertEqual(
            detect_research_mode("Stress test the bear case for this company"),
            "bear",
        )
        self.assertEqual(
            detect_research_mode("Underwrite both sides and give me a balanced view"),
            "balanced",
        )

    def test_newsletter_footer_contains_mandatory_disclosure(self):
        from tools.newsletter.sections import render_footer

        footer = render_footer()
        self.assertIn(
            "This newsletter is for informational purposes only",
            footer,
        )
        self.assertIn(
            "Investing in private securities involves substantial risk",
            footer,
        )
        self.assertNotIn("\u2014", footer)
        self.assertNotIn("\u2013", footer)

    def test_configured_users_share_one_workspace(self):
        import app

        dom = app.HeraldSQLAlchemyDataLayer._get_configured_user("dom")
        admin = app.HeraldSQLAlchemyDataLayer._get_configured_user("lubosi")
        self.assertEqual(
            dom.metadata["workspace_id"],
            admin.metadata["workspace_id"],
        )


if __name__ == "__main__":
    unittest.main()

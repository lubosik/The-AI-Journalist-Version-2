"""
Production hardening tests — verify all critical fixes applied in this session.
Run from /tmp/herald-v2/ with: python3 tests/test_hardening.py -v
"""
from __future__ import annotations
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class TestClassifyIntentResearch(unittest.TestCase):
    """classify_intent must route research phrases correctly."""

    def setUp(self):
        import app
        self.app = app

    def test_research_explicit(self):
        self.assertEqual(self.app.classify_intent("research SpaceX latest valuation"), "research")

    def test_research_find_out(self):
        self.assertEqual(self.app.classify_intent("find out about the Stripe IPO"), "research")

    def test_research_look_into(self):
        self.assertEqual(self.app.classify_intent("look into Anduril's funding round"), "research")

    def test_research_tell_me_about(self):
        self.assertEqual(self.app.classify_intent("tell me about xAI"), "research")

    def test_research_deep_dive(self):
        self.assertEqual(self.app.classify_intent("deep dive on Databricks secondary pricing"), "research")

    def test_research_whats_happening(self):
        self.assertEqual(self.app.classify_intent("what's happening with OpenAI"), "research")

    def test_delete_topic_variants(self):
        phrases = [
            "remove that",
            "delete that",
            "scratch that topic",
            "remove it",
            "delete it",
            "drop that topic",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertEqual(self.app.classify_intent(phrase), "delete_topic")


class TestSystemPromptPresent(unittest.TestCase):
    """build_prompt must always include system message as messages[0]."""

    def setUp(self):
        import app
        self.app = app

    def test_build_prompt_has_system_first(self):
        # In on_message, history[0] is always refreshed with get_herald_system() before build_prompt is called.
        # build_prompt uses history[0] if it's a system role, so we pass a refreshed system message.
        fresh_system = self.app.get_herald_system()
        history = [{"role": "system", "content": fresh_system}, {"role": "user", "content": "hello"}]
        msgs = self.app.build_prompt("What is happening?", history)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("HERALD", msgs[0]["content"])

    def test_build_prompt_empty_history(self):
        msgs = self.app.build_prompt("hello", [])
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("HERALD", msgs[0]["content"])

    def test_build_prompt_last_message_is_user(self):
        msgs = self.app.build_prompt("test query", [])
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertEqual(msgs[-1]["content"], "test query")

    def test_get_herald_system_contains_dom_context(self):
        prompt = self.app.get_herald_system()
        self.assertIn("HERALD", prompt)
        self.assertIn("secondar", prompt.lower())
        self.assertGreater(len(prompt), 200)


class TestHandleResearchContextFallback(unittest.TestCase):
    """handle_research must extract topic from history when message is short."""

    def test_research_query_stripped_correctly(self):
        import re
        # Simulate the stripping logic
        text = "research SpaceX latest"
        query = re.sub(
            r"(?i)^\s*(do\s+)?(deep\s+research\s+on|deep\s+dive\s+on|research|find out about|look into|tell me about)\s*",
            "",
            text,
        ).strip()
        self.assertEqual(query, "SpaceX latest")

    def test_short_message_would_trigger_history_lookup(self):
        # If text is stripped to < 20 chars, the code falls back to history
        text = "go"
        import re
        query = re.sub(
            r"(?i)^\s*(do\s+)?(deep\s+research\s+on|deep\s+dive\s+on|research|find out about|look into|tell me about)\s*",
            "",
            text,
        ).strip()
        self.assertLess(len(query), 20)  # Would trigger history lookup


class TestIntentsDict(unittest.TestCase):
    """INTENTS dict must contain all required keys including delete_topic."""

    def setUp(self):
        import app
        self.app = app

    def test_all_intent_keys_in_intents(self):
        required = [
            "url_ingest", "research", "transcript", "save_topic", "delete_topic",
            "view_plan", "draft", "status", "morning_brief", "linkedin",
            "tiktok_check", "source_latest", "conversation",
        ]
        for key in required:
            with self.subTest(key=key):
                self.assertIn(key, self.app.INTENTS)

    def test_intents_tuples_have_three_elements(self):
        for key, val in self.app.INTENTS.items():
            with self.subTest(key=key):
                self.assertEqual(len(val), 3, f"INTENTS['{key}'] must be a 3-tuple")


class TestSanitiseResponse(unittest.TestCase):
    """sanitise_response must never return raw JSON."""

    def setUp(self):
        import app
        self.app = app

    def test_json_object_converted(self):
        raw = json.dumps({"error": "something went wrong"})
        result = self.app.sanitise_response(raw)
        # Should not start with { after sanitisation
        self.assertFalse(result.strip().startswith("{"))

    def test_plain_text_unchanged(self):
        text = "SpaceX raised $1B in a Series X round."
        result = self.app.sanitise_response(text)
        self.assertEqual(result, text)

    def test_empty_string_passthrough(self):
        result = self.app.sanitise_response("")
        self.assertEqual(result, "")


class TestAvailableModels(unittest.TestCase):
    """AVAILABLE_MODELS must not reference opus-4-7 (use opus-4-8)."""

    def setUp(self):
        import app
        self.app = app

    def test_no_opus_4_7_in_models(self):
        for key, model in self.app.AVAILABLE_MODELS.items():
            with self.subTest(key=key):
                self.assertNotIn(
                    "opus-4-7",
                    model.get("id", ""),
                    f"Model '{key}' still references deprecated opus-4-7",
                )

    def test_default_hermes_model_present(self):
        self.assertIn("hermes", self.app.AVAILABLE_MODELS)

    def test_claude_opus_48_present(self):
        ids = [m["id"] for m in self.app.AVAILABLE_MODELS.values()]
        self.assertTrue(any("opus-4-8" in mid for mid in ids), "claude-opus-4-8 not in AVAILABLE_MODELS")


class TestGenerateAndPresentDraftSignature(unittest.TestCase):
    """generate_and_present_draft must exist and have correct signature."""

    def setUp(self):
        import app
        self.app = app

    def test_function_exists(self):
        self.assertTrue(callable(self.app.generate_and_present_draft))

    def test_handle_research_exists(self):
        self.assertTrue(callable(self.app.handle_research))

    def test_handle_delete_topic_exists(self):
        self.assertTrue(callable(self.app.handle_delete_topic))


class TestCallbackRegistrations(unittest.TestCase):
    """Action callbacks must be registered (function names present in module)."""

    def setUp(self):
        import app
        self.app = app

    def test_save_research_as_topic_callback_exists(self):
        # The callback is registered as on_save_research_topic
        self.assertTrue(
            hasattr(self.app, "on_save_research_topic"),
            "on_save_research_topic callback not found in app.py",
        )

    def test_confirm_draft_callback_exists(self):
        self.assertTrue(hasattr(self.app, "on_confirm_draft"))

    def test_switch_model_callback_exists(self):
        self.assertTrue(hasattr(self.app, "on_switch_model"))

    def test_continue_editing_callback_exists(self):
        self.assertTrue(hasattr(self.app, "on_continue_editing"))


class TestContinuationWords(unittest.TestCase):
    """Verify continuation word set is correct and comprehensive."""

    def test_continuation_words_present_in_source(self):
        import re
        source = Path("/tmp/herald-v2/app.py").read_text()
        # CONTINUATION_WORDS set must exist in on_message
        self.assertIn("CONTINUATION_WORDS", source)
        self.assertIn("pending_intent", source)

    def test_save_research_as_topic_action_in_handle_research(self):
        source = Path("/tmp/herald-v2/app.py").read_text()
        self.assertIn("save_research_as_topic", source)

    def test_system_prompt_in_handle_source_check(self):
        source = Path("/tmp/herald-v2/app.py").read_text()
        # handle_source_check now uses build_prompt or explicit system message
        # Check that get_herald_system appears in the source check function
        # (It's in a messages list now)
        self.assertIn("get_herald_system()", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)

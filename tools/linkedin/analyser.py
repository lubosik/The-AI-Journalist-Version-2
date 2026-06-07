"""
linkedin/analyser.py

Builds a LinkedIn-specific style bible from Dom's posts, cross-referenced
with Elena's TikTok hook style.
"""

import asyncio
import json
import logging
import os

from openai import OpenAI
from dotenv import load_dotenv

from config import MODELS, OPENROUTER_BASE_URL
from db.client import get_client
from training.style_analyser import get_active_style_bible

load_dotenv()
logger = logging.getLogger(__name__)


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(text))
        except Exception as e:
            logger.error(f"[linkedin_analyser] JSON parse failed: {e}")
            return {}


LINKEDIN_STYLE_SYSTEM = """You are a LinkedIn content strategist and linguistic analyst.

Analyse Dom's LinkedIn posts corpus and produce a comprehensive LinkedIn style guide.

Return JSON with this exact structure:
{
  "voice_summary": "How Dom writes on LinkedIn in one paragraph",
  "post_structure": {
    "typical_opening": "how his posts start",
    "hook_patterns": ["pattern1", "pattern2", "pattern3"],
    "body_style": "how he develops his point",
    "closing_pattern": "how posts end",
    "cta_style": "how he drives engagement"
  },
  "linguistic_patterns": {
    "avg_post_length": 0,
    "sentence_style": "short/medium/long",
    "use_of_line_breaks": "description of how he uses white space",
    "vocabulary": ["key terms and phrases he uses"],
    "tone_words": ["tone descriptors"]
  },
  "top_performing_patterns": [
    "pattern observed in highest engagement posts"
  ],
  "elena_crossover": {
    "hooks_that_translate": "which of Elena's techniques work on LinkedIn",
    "what_to_borrow": "specific techniques from Elena to apply to Dom's LinkedIn",
    "what_to_keep_linkedin": "what must stay pure Dom LinkedIn voice"
  },
  "master_formula": {
    "line_1": "formula for the opening hook line",
    "lines_2_4": "formula for the body development",
    "line_5": "formula for the closing",
    "hashtag_rule": "exactly how to use or not use hashtags"
  },
  "templates": [
    {
      "type": "deal_announcement",
      "template": "full post template with [PLACEHOLDERS] showing exact structure"
    },
    {
      "type": "market_insight",
      "template": "full post template with [PLACEHOLDERS]"
    },
    {
      "type": "newsletter_promo",
      "template": "full post template with [PLACEHOLDERS]"
    }
  ]
}

Return only valid JSON. Be specific and based on actual evidence from the posts."""


async def build_linkedin_style_bible() -> dict:
    """
    Analyse Dom's LinkedIn posts and build a style bible.
    Cross-references with Elena's TikTok style for hook guidance.
    Returns the analysis dict.
    """
    client_db = get_client()
    client_llm = _get_client()

    # Get all of Dom's posts sorted by engagement
    try:
        posts_result = client_db.table("linkedin_posts").select("*").execute()
        posts = posts_result.data or []
    except Exception as e:
        logger.error(f"[linkedin_analyser] Failed to fetch posts: {e}")
        return {"error": f"Could not fetch posts: {e}"}

    if len(posts) < 3:
        return {"error": "Not enough posts to analyse. Run /linkedin_setup to scrape first."}

    logger.info(f"[linkedin_analyser] Analysing {len(posts)} LinkedIn posts")

    # Sort by engagement
    top_posts = sorted(
        posts,
        key=lambda x: (x.get("likes", 0) + x.get("comments", 0) * 3),
        reverse=True,
    )[:20]

    all_posts_text = "\n\n---\n\n".join([p["post_text"] for p in posts if p.get("post_text")])
    top_posts_text = "\n\n---\n\n".join([p["post_text"] for p in top_posts if p.get("post_text")])

    # Get Elena's style for cross-reference
    elena_style = await get_active_style_bible()
    elena_analysis = elena_style.get("analysis_text", "Not available yet.")

    try:
        response = await asyncio.to_thread(
            client_llm.chat.completions.create,
            model=MODELS["writer"],
            messages=[
                {"role": "system", "content": LINKEDIN_STYLE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Dom's LinkedIn posts (all {len(posts)}):\n{all_posts_text[:12000]}\n\n"
                        f"Top performing posts:\n{top_posts_text[:4000]}\n\n"
                        f"Elena's TikTok style for cross-reference:\n{elena_analysis[:2000]}"
                    ),
                },
            ],
            temperature=0.2,
        )
        raw = (response.choices[0].message.content or "").strip()
        analysis = _parse_json_response(raw)
    except Exception as e:
        logger.error(f"[linkedin_analyser] LLM call failed: {e}")
        raise

    if not analysis or "error" in analysis:
        return {"error": "Analysis failed to parse"}

    # Deactivate old style bibles
    try:
        client_db.table("linkedin_style_bible").update({"is_active": False}).eq("is_active", True).execute()
    except Exception:
        pass

    # Store new one
    try:
        client_db.table("linkedin_style_bible").insert({
            "version": len(posts),
            "analysis_json": analysis,
            "post_count_analysed": len(posts),
            "is_active": True,
        }).execute()
        logger.info(f"[linkedin_analyser] LinkedIn style bible stored ({len(posts)} posts analysed)")
    except Exception as e:
        logger.error(f"[linkedin_analyser] Failed to store style bible: {e}")
        raise

    return analysis


async def get_linkedin_style_bible() -> dict:
    """Retrieve the active LinkedIn style bible."""
    try:
        client_db = get_client()
        result = client_db.table("linkedin_style_bible").select("*").eq("is_active", True).limit(1).execute()
        if result.data:
            return result.data[0]
        return {}
    except Exception as e:
        logger.error(f"[linkedin_analyser] get_linkedin_style_bible error: {e}")
        return {}

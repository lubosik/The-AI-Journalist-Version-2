"""
linkedin/repurposer.py

Converts any content into a LinkedIn post in Dom's voice.
"""

import asyncio
import json
import logging
import os

from openai import OpenAI
from dotenv import load_dotenv

from config import MODELS, OPENROUTER_BASE_URL
from filters.response_filter import filter_response
from linkedin.analyser import get_linkedin_style_bible

load_dotenv()
logger = logging.getLogger(__name__)


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )


async def repurpose_to_linkedin(
    source_content: str,
    topic: str = "",
    post_type: str = "market_insight",
) -> str:
    """
    Takes any content and repurposes it into a LinkedIn post in Dom's voice.
    Completely separate mode from HERALD newsletter voice.
    """
    style_row = await get_linkedin_style_bible()

    if not style_row:
        return (
            "LinkedIn style bible not built yet. Send /linkedin_setup to scrape "
            "Dom's posts and build the style guide first."
        )

    style = style_row.get("analysis_json", {})
    templates = style.get("templates", [])
    template = next(
        (t.get("template", "") for t in templates if t.get("type") == post_type),
        "",
    )

    style_json_str = json.dumps(style, indent=2)[:3000]

    system = f"""You write LinkedIn posts in Dom Pandolfo's voice. His voice on LinkedIn is DIFFERENT from any newsletter voice.

Dom's LinkedIn style guide:
{style_json_str}

Rules you must follow:
- Match his exact sentence rhythm, line breaks, and phrasing patterns
- No asterisks, no markdown, no AI slop phrases
- Use line breaks exactly the way he does
- Apply Elena's hook energy to the first line but keep the rest pure Dom
- Sound like Dom talking to his network, not like a journalist writing a newsletter
- No em dashes
- End with a subtle insight or engagement prompt, never a generic CTA
- Never use: "It's worth noting", "In conclusion", "Key takeaways", hashtag spam"""

    template_instruction = f"\n\nUse this {post_type} template as structure:\n{template}" if template else ""

    user = f"""Write a LinkedIn post about: {topic or 'the following content'}

Source material:
{source_content[:2000]}
{template_instruction}"""

    client = _get_client()
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["writer"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.7,
        )
        result = (response.choices[0].message.content or "").strip()
        return filter_response(result)
    except Exception as e:
        logger.error(f"[linkedin_repurposer] Error: {e}")
        return f"Could not generate LinkedIn post: {str(e)[:200]}"

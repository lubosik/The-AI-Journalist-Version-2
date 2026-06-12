"""
processing/humanizer.py — AI pattern removal post-processing pass

Strips AI writing tells from newsletter sections after the editorial review loop.
Based on the 29-pattern humanizer approach (github.com/blader/humanizer).
Runs as the final prose pass before HTML assembly.
"""

import asyncio
import logging
import os

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_HUMANIZER_SYSTEM_PROMPT = """\
CARE TRANSFORMATION BRIEF

CONTEXT:
You are a writing editor that strips AI-generated writing patterns from financial newsletter text.

ASK:
Rewrite the supplied newsletter section only where needed to remove AI writing tells. Preserve every fact, name, number, deal term, date, core meaning, and established voice.

RULES:
PATTERNS TO REMOVE (in order of priority):

VOCABULARY TELLS — delete or replace every instance:
additionally, align with, crucial, delve, emphasizing, enduring, enhance, fostering, garner,
highlight (verb), interplay, intricate/intricacies, key (adjective), landscape (abstract noun),
pivotal, showcase, tapestry, testament, underscore (verb), valuable, vibrant, robust,
transformative, seamless, revolutionary, cutting-edge, groundbreaking, leverage (as verb)

STRUCTURAL TELLS:
- Significance inflation: remove "stands as/serves as/marks a pivotal moment/is a testament to/reflects broader/symbolizing/contributing to/setting the stage for"
- Superficial -ing tails: cut trailing "-ing" phrases that add no new information ("highlighting X", "symbolizing Y", "showcasing Z", "underscoring the importance of")
- Copula avoidance: replace "serves as", "stands as", "boasts", "features" used as fancy versions of "is/has" with the plain verb
- Negative parallelisms: cut "It's not just X, it's Y" and "Not only... but also..." constructions
- Synonym cycling: when the same person/thing is called three different names to avoid repetition, pick one name and use it
- Passive voice: convert to active where the actor is known
- Vague attributions: cut "Experts believe", "Industry reports suggest", "Observers note" if no real source is named
- Filler phrases: remove "In order to", "Due to the fact that", "At this point in time", "It is important to note that", "It is worth noting"
- Excessive hedging: cut "could potentially possibly", over-stacked qualifiers
- Generic positive conclusions: kill "The future looks bright", "exciting times lie ahead", "a step in the right direction"
- Persuasive authority tropes: remove "At its core", "The real question is", "what really matters is", "fundamentally", "the heart of the matter"
- Signposting: remove "Let's dive into", "Here's what you need to know", "Let's explore", "Without further ado"
- Em dashes (—): replace with a period or a comma

VOICE TO PRESERVE after removing AI tells:
- Preserve the existing point of view through fact selection and specific reactions. Do not add a new implication.
- Vary sentence length. Short. Then a longer one that takes its time getting to the point.
- Acknowledge complexity where it exists: "impressive but also unsettling" beats neutral.
- Be specific about reactions, not vague ("something is off about the GP logic here" beats "this raises questions").

CRITICAL CONSTRAINTS:
- Do NOT change any fact, name, number, valuation, fund size, deal term, or date
- Do NOT add information that was not in the original
- Do NOT change the newsletter's established dry, insider financial journalism voice
- Do NOT add a conclusion, summary, advice, or interpretation that was not already present
- Return ONLY the humanized prose. No commentary, no "here's the revised version:", no preamble.
- If a section is already clean, return it unchanged.

SELF-REFINE QUALITY GATE:
Privately compare the revision with the source for factual fidelity, omissions, added claims, voice drift, residual AI tells, em dashes, and accidental conclusions. Correct any issue you find. Do not output the check or reasoning.

RESPONSE:
Return only the final section prose.
"""


async def humanize_sections(sections: list[dict]) -> list[dict]:
    """
    Run a humanizer pass on newsletter sections.
    Rewrites content fields to remove AI writing patterns.
    Skips sections under 50 chars — nothing to fix.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("humanize_sections: OPENROUTER_API_KEY not set — skipping humanizer")
        return sections

    from config import MODELS, OPENROUTER_BASE_URL
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)

    humanized = []
    for section in sections:
        content = section.get("content", "")
        section_id = section.get("id", "unknown")
        if not content or len(content) < 50:
            humanized.append(section)
            continue

        try:
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=MODELS["writer"],
                messages=[
                    {"role": "system", "content": _HUMANIZER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"SECTION ID: {section_id}\n"
                            f"Transform only the text inside SOURCE SECTION. Follow the system rules and return prose only.\n\n"
                            f"SOURCE SECTION:\n{content}"
                        ),
                    },
                ],
                temperature=0.35,
                max_tokens=2000,
            )
            result = (response.choices[0].message.content or "").strip()
            if result:
                humanized.append({**section, "content": result})
                logger.info(
                    "humanize_sections: '%s' humanized (%d -> %d chars)",
                    section_id, len(content), len(result)
                )
            else:
                humanized.append(section)
        except Exception as e:
            logger.error("humanize_sections: error on section '%s': %s", section_id, e)
            humanized.append(section)

    return humanized

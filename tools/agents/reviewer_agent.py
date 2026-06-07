"""
agents/reviewer_agent.py — EDITORIAL REVIEWER

A deliberately context-free quality gate between Hermes and Telegram delivery.

This agent knows:
  - Who the audience is and what the newsletter must achieve
  - The editorial standard and style rules
  - How many review iterations have occurred in this pipeline run

This agent does NOT know:
  - How the newsletter was produced
  - Internal system names (HERALD)
  - Pipeline state, Beehiiv, Supabase, or any infrastructure

Verdict: PASS (delivers to Telegram) or FAIL (returns numbered issues to Hermes).
Max 5 iterations per pipeline run — on the 5th pass it delivers regardless, with flags noted.
"""

import asyncio
import logging
import os
from dataclasses import dataclass

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 5

# ---------------------------------------------------------------------------
# Reviewer brief — everything the reviewer needs to judge quality.
# No pipeline internals. Fresh pair of eyes.
# ---------------------------------------------------------------------------

_REVIEWER_BRIEF = """\
YOU ARE A SENIOR EDITOR WITH 20 YEARS CUTTING AI SLOP FROM FINANCIAL COPY.

You have no knowledge of how this newsletter was produced. You are seeing it for the first time. Your default assumption is that this draft FAILS. Your job is to prove that assumption — or be proven wrong.

You work at the editorial standard of The Economist crossed with a top-tier LP letter. You have a zero-tolerance policy for AI-generated prose, lazy transitions, news-summary thinking, and anything a sophisticated investor would find patronising or obvious.

Output your SCORE and VERDICT at the very top of your response — before any analysis or explanation. Then provide your detailed breakdown below. The score line must appear first: this is mandatory.

Be surgical. Be harsh. Most drafts are not ready.

═══════════════════════════════════════════════════════════
THE BRIEF
═══════════════════════════════════════════════════════════

PUBLICATION: The Secondaries Intelligence Report
PUBLISHER: Dominic — a pre-IPO secondaries deal-maker and advisor
AUDIENCE: LPs, GPs, family offices, RIAs, and sophisticated institutional investors in the VC secondaries market. These readers are highly sophisticated. They do not need background explained. They consume Bloomberg, deal memos, and LP letters daily.
PURPOSE: Give readers intelligence they cannot get elsewhere. Not a news summary. Not a market recap. Insider signal, specific data, actionable perspective. The test: would a GP forward this to their IC? Would an LP forward it to their advisor?
FORMAT: A short weekly ping in the voice of Elena Nisonoff — storytelling journalist with dry wit. Length adapts to how many topics Dom requested: approximately 200-300 words per requested topic, or 200-400 words total when no topics were specified (covering the 1-2 best stories from the last 48 hours). The editorial note covers ALL of Dom's requested topics if he gave any, or the 1-2 best stories if he gave none.

═══════════════════════════════════════════════════════════
EDITORIAL STANDARD
═══════════════════════════════════════════════════════════

VOICE (what good looks like):
- Elena Nisonoff's voice: storytelling, dry wit, specific, human, slightly sardonic.
- Dense with information. No hand-holding.
- Reads like a well-sourced journalist who finds the human absurdity in financial events.
- The best subject lines feel like insider intel: "The deal Goldman didn't want public" or "Three funds repriced 40% below NAV this week".
- Opening sentences MUST open on a universally relatable human experience, then pivot to the story. See Elena Voice Requirement below.

EXAMPLES OF GOOD OPENING SENTENCES (Elena-style):
  "If you have ever tried to buy something and been told there is nothing available at any price, you know the feeling."
  "There is a specific type of person who, when told they cannot buy something, calls three brokers before lunch. Every market has that person."
  "At some point in most federal trials, things get uncomfortable. Musk v. Altman reached that point Tuesday."

EXAMPLES OF BAD OPENING SENTENCES (automatic FAIL):
  "This week in VC secondaries..."
  "In today's issue, we cover..."
  "Welcome to another edition..."
  "It has been a busy week for..."
  Any sentence that starts with a company name, a valuation number, or a raw data point.

═══════════════════════════════════════════════════════════
HARD RULES — ANY VIOLATION = FAIL
═══════════════════════════════════════════════════════════

1. ZERO em dashes (— or –). Not one anywhere in the entire newsletter.
2. No AI-sounding phrases or vocabulary. Automatic fail on any of these: "it's worth noting", "it's important to understand", "delve", "tapestry", "nuanced", "robust", "transformative", "pivotal", "furthermore", "moreover", "notably", "crucially", "that being said", "at the end of the day", "game changer", "deep dive", "treasure trove", "shed light on", "unlock", "leverage" (as a verb), "cutting-edge", "seamless", "revolutionize", "in today's fast-paced world", "it's no secret that", "the bottom line", "a testament to", "additionally", "align with", "enduring", "enhance", "fostering", "garner", "highlight" (as verb), "interplay", "intricate", "landscape" (abstract), "showcase", "valuable", "vibrant", "groundbreaking".
2b. No AI structural patterns: significance inflation ("marks a pivotal moment", "reflects broader trends", "symbolizing its", "contributing to the narrative"), copula avoidance ("serves as", "stands as", "boasts" instead of "is/has"), negative parallelisms ("It's not just X, it's Y"), signposting ("Let's dive into", "Here's what you need to know"), persuasive authority tropes ("At its core", "The real question is"), or generic positive conclusions ("the future looks bright", "exciting times ahead").
3. No publication name visible to readers (no system/internal names — subscribers should never see the internal name of the system that produced this).
4. No markdown formatting (no **, no ##, no bullet points using *).
5. No passive voice where active is possible.
6. Subject line must be under 50 characters and feel like insider intel — not a generic headline.
7. The newsletter must not read like a news summary. Each section must have a specific angle, not just "here is what happened".
8."Heard on the Street" section (if present): every anecdote must be grounded in real, verifiable information from the research — no invented facts, no fabricated quotes, no made-up statements attributed to real people. The comedy must come from how real information is framed, not from fiction. If any anecdote contains a fabricated quote, an invented event, or a statement attributed to someone that was not actually reported, that is a FAIL. Dry wit applied to true facts is the standard.
10. LENGTH AND TOPIC SCOPE: When Dom specified topics, The Note must cover ALL of them — each with dedicated paragraphs. When Dom specified no topics, The Note covers the 1-2 best stories from the last 48 hours (1 if only one strong story exists, 2 if two connect naturally). A newsletter that ignores Dom's requested topics is a FAIL. A newsletter that covers only some of Dom's requested topics is a FAIL.
11. CONTENT FOCUS: The newsletter must cover ONLY Anthropic, OpenAI, SpaceX, Anduril, xAI, Stripe, Databricks, or direct peers. Any coverage of general PE, mid-market deals, or companies outside this universe is an automatic FAIL.
12. NO CONCLUSIONS — HARD RULE: The newsletter must NOT contain a closing paragraph, summary paragraph, or thematic wrap-up. Any paragraph that ties two stories together ("Both companies...", "One company... another...", "Taken together..."), zooms out to explain what it all means, or tells the reader how to feel about the stories is an automatic FAIL. The last sentence must be a fact or a flat reaction from the final story — not a conclusion. Filler paragraphs (any paragraph that adds no new information and whose removal makes the piece better) are a FAIL.

═══════════════════════════════════════════════════════════
ELENA VOICE REQUIREMENT — NON-NEGOTIABLE
═══════════════════════════════════════════════════════════

This newsletter is written in the voice of Elena Nisonoff — a specific comedic storytelling style. Any draft that does not meet all three of the following is an automatic FAIL regardless of other scores:

REQUIREMENT A — RELATABLE HOOK: The very first sentence must open on a universal human experience that the reader has felt, then pivot to the specific story. It must NOT open with a valuation number, a data point, "This week", or a company name. The pattern: "If you've ever [felt X], it's unlikely you did so with as much [Y] as [person/company] did when [story]." OR "There is a specific type of person who [universal behaviour]. [Name] is that person."

REQUIREMENT B — FLAT REACTION LINES: Every paragraph must contain exactly one flat reaction — a 3 to 7 word sentence, its own line, bone-dry, that understates something genuinely absurd. These reactions are where the comedy lives. Examples of the correct register: "Dream big, I guess." / "This did not inspire confidence." / "Can't afford to buy." / "He kept repeating it like a broken action figure." / "Completely normal secondary market behaviour." A draft with zero flat reactions in any paragraph automatically FAILS.

REQUIREMENT C — STORY WALK: Events must be narrated in sequence, reacting as they unfold. The reader discovers what happened alongside the narrator. The draft must NOT front-load the conclusion and then explain it. The structure: name the players simply, introduce the absurd fact, pause with a flat reaction, walk through what happened, react again, end with where things stand. If the draft states the full picture in the first paragraph and then elaborates, that is a FAIL.

═══════════════════════════════════════════════════════════
SCORING RUBRIC
═══════════════════════════════════════════════════════════

Judge the newsletter on these six dimensions:

1. SUBJECT LINE (0-15): Specificity, intrigue, insider feel, length compliance
2. OPENING HOOK (0-15): Opens on relatable human frame — not a fact, not a company name, not a number. Automatic 0 if it opens with a valuation, "This week", or a data point.
3. ELENA VOICE (0-25): Flat reaction lines present in every paragraph. Story walk structure. First-person casual reactions. Specific vivid metaphors over generic descriptions. Deduce a point for every paragraph missing a flat reaction. This is the most important dimension.
4. INSIDER VOICE (0-20): Reads like someone with actual sources, not a news aggregator. Specific named people doing specific things. Deduct 10 points if the newsletter covers companies or topics outside Dom's explicit preference universe (general PE, mid-market, broad macro).
5. HARD RULES COMPLIANCE (0-15): Zero violations of the rules above (em dashes, AI phrases, passive voice)
6. STRUCTURAL QUALITY (0-10): Short ping format, one clear angle, no stray sections

Total out of 100. PASS threshold: 80+
Elena Voice score of 15 or below is an automatic FAIL regardless of total score.
"""

_REVIEWER_TASK = """\
Review the newsletter draft below.

This is iteration {iteration} of {max_iterations} for this pipeline run.
{iteration_context}

OUTPUT YOUR SCORE AND VERDICT FIRST — at the very top, before any analysis:

SCORE: [number]/100
VERDICT: PASS or FAIL

Then provide sub-dimension scores and your full analysis below.

SUBJECT LINE SCORE: [n]/15 — [one sentence reason]
OPENING HOOK SCORE: [n]/15 — [did it open with a relatable human frame? Quote the first sentence and judge it]
ELENA VOICE SCORE: [n]/25 — [list every paragraph that has a flat reaction line and every paragraph that is missing one. Quote any flat reactions found. If Story Walk is absent, say so explicitly.]
INSIDER VOICE SCORE: [n]/20 — [one sentence reason]
HARD RULES SCORE: [n]/15 — [list any violations found, or "Clean"]
STRUCTURAL QUALITY SCORE: [n]/10 — [one sentence reason]

{fail_section}

NEWSLETTER DRAFT:
{newsletter_text}
"""

_FAIL_SECTION = """\
REQUIRED FIXES (numbered, specific, actionable — the writer will action these exactly):
1. [specific fix]
2. [specific fix]
...

Be precise. Quote the problematic text and state exactly what it should become.
Do not give general guidance. Give surgical edits.
"""


@dataclass
class ReviewResult:
    passed: bool
    score: int
    verdict_text: str
    issues: list[str]
    iteration: int


def _format_newsletter_for_review(
    subject_line: str,
    preview_text: str,
    sections: list[dict],
) -> str:
    """Format the newsletter content cleanly for the reviewer — no HTML, no metadata."""
    lines = [
        f"SUBJECT LINE: {subject_line}",
        f"PREVIEW TEXT: {preview_text}",
        "",
    ]
    for section in sections:
        title = section.get("title", section.get("id", ""))
        content = section.get("content", "")
        lines.append(f"[{title.upper()}]")
        lines.append(content)

        deals = section.get("deals_table", [])
        if deals:
            lines.append("DEALS:")
            for deal in deals:
                company = deal.get("company", "")
                stage = deal.get("stage", "")
                deal_type = deal.get("deal_type", "")
                size = deal.get("reported_size", "")
                signal = deal.get("signal", "")
                lines.append(f"  {company} | {stage} | {deal_type} | {size} | {signal}")
        lines.append("")
    return "\n".join(lines)


def _parse_review_response(text: str, iteration: int) -> ReviewResult:
    """Parse the reviewer's response into a structured result.

    Deliberately permissive — Haiku formats its output differently depending on
    draft quality and response length. We scan the full text rather than relying
    on exact line-start patterns.
    """
    import re

    passed = False
    score = 0
    issues: list[str] = []

    lines = text.splitlines()

    # ── Score: find the first N/100 that is NOT a sub-dimension N/20 line ────
    # Sub-dimension lines always contain "/20". Aggregate lines say "/100".
    # We accept any line that has digits/100 and doesn't also have /20.
    for line in lines:
        stripped = line.strip()
        if re.search(r"\d+\s*/\s*100", stripped):
            # Skip lines that are clearly sub-dimension scores (they also have /20)
            if not re.search(r"\d+\s*/\s*20", stripped):
                m = re.search(r"(\d+)\s*/\s*100", stripped)
                if m:
                    try:
                        candidate = int(m.group(1))
                        if 0 <= candidate <= 100:
                            score = candidate
                            break  # take the first valid aggregate score
                    except ValueError:
                        pass

    # ── Verdict: scan entire text for PASS/FAIL near the word VERDICT ────────
    # Handles: "VERDICT: FAIL", "**VERDICT**: PASS", "VERDICT — FAIL", etc.
    verdict_block = re.search(
        r"VERDICT\b.*?\b(PASS|FAIL)\b",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if verdict_block:
        passed = verdict_block.group(1).upper() == "PASS"
    else:
        # Fallback: score >= 80 means pass even if verdict word not found
        passed = score >= 80

    # ── Issues: numbered list anywhere after FIXES / ISSUES / REVISION header ─
    # Accept several section header variants Haiku uses in practice.
    in_fixes = False
    for line in lines:
        stripped = line.strip()
        if re.search(
            r"(REQUIRED FIXES|FIXES REQUIRED|ISSUES TO FIX|REVISION NOTES|CHANGES REQUIRED)",
            stripped,
            re.IGNORECASE,
        ):
            in_fixes = True
            continue
        if in_fixes:
            if re.match(r"NEWSLETTER DRAFT", stripped, re.IGNORECASE):
                break
            m = re.match(r"^(\d+)[\.\)]\s+(.+)", stripped)
            if m:
                issue_text = m.group(2).strip()
                if issue_text and "[specific fix]" not in issue_text:
                    issues.append(issue_text)

    logger.debug(
        "_parse_review_response: score=%d passed=%s issues=%d", score, passed, len(issues)
    )
    return ReviewResult(
        passed=passed,
        score=score,
        verdict_text=text,
        issues=issues,
        iteration=iteration,
    )


async def review_newsletter(
    subject_line: str,
    preview_text: str,
    sections: list[dict],
    iteration: int = 1,
    mandatory_topics: list[str] | None = None,
) -> ReviewResult:
    """
    Run a single review pass on the newsletter draft.

    Args:
        subject_line: The email subject line
        preview_text: The email preview/subtitle text
        mandatory_topics: Topics Dom explicitly requested — reviewer checks all are covered
        sections: The newsletter sections as dicts
        iteration: Which review iteration this is (1, 2, or 3)

    Returns:
        ReviewResult with passed, score, issues, and full verdict text
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("review_newsletter: OPENROUTER_API_KEY not set — skipping review")
        return ReviewResult(passed=True, score=100, verdict_text="Review skipped (no API key)", issues=[], iteration=iteration)

    from config import MODELS, OPENROUTER_BASE_URL
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)

    newsletter_text = _format_newsletter_for_review(subject_line, preview_text, sections)

    # Build iteration context — reviewer knows how many rounds have happened
    # but not WHY (no pipeline context)
    if iteration == 1:
        iteration_context = "This is the first time you are seeing this draft."
    elif iteration == 2:
        iteration_context = "This draft has been revised once following your previous feedback."
    else:
        iteration_context = (
            f"This draft has been through {iteration - 1} revision rounds. "
            f"Apply the same rigorous standard — do not lower your bar because of revision count."
        )

    # Inject mandatory topics so reviewer can verify all are covered
    mandatory_block = ""
    if mandatory_topics:
        numbered = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(mandatory_topics))
        mandatory_block = (
            f"\n\nMANDATORY TOPICS FOR THIS EDITION (Dom explicitly requested all of these):\n"
            f"{numbered}\n"
            f"Check The Note carefully. If ANY of these topics is absent from the newsletter, "
            f"that is an automatic FAIL regardless of other scores. Add it to your REQUIRED FIXES list.\n"
        )

    fail_section = _FAIL_SECTION

    task_prompt = _REVIEWER_TASK.format(
        iteration=iteration,
        max_iterations=MAX_REVIEW_ITERATIONS,
        iteration_context=iteration_context + mandatory_block,
        fail_section=fail_section,
        newsletter_text=newsletter_text,
    )

    try:
        logger.info(
            "review_newsletter: iteration=%d, model=%s, newsletter_len=%d chars",
            iteration, MODELS.get("reviewer", MODELS["writer"]), len(newsletter_text)
        )
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS.get("reviewer", MODELS["writer"]),
            messages=[
                {"role": "system", "content": _REVIEWER_BRIEF},
                {"role": "user", "content": task_prompt},
            ],
            temperature=0.3,
            max_tokens=4000,
        )
        raw = response.choices[0].message.content or ""
        logger.info("review_newsletter: response received (%d chars)", len(raw))
        result = _parse_review_response(raw, iteration)
        logger.info(
            "review_newsletter: iteration=%d VERDICT=%s score=%d issues=%d",
            iteration, "PASS" if result.passed else "FAIL", result.score, len(result.issues)
        )
        return result

    except Exception as exc:
        logger.error("review_newsletter: error on iteration %d: %s", iteration, exc)
        return ReviewResult(passed=False, score=0, verdict_text=f"Review error: {exc}", issues=[], iteration=iteration)


def _build_revision_prompt(issues: list[str], original_research: str, iteration: int = 1) -> str:
    """Build the user message to Hermes asking it to fix specific issues."""
    issue_list = "\n".join(f"{i+1}. {issue}" for i, issue in enumerate(issues))
    urgency = ""
    if iteration >= 3:
        urgency = (
            "\n\nCRITICAL — THIS IS REVISION ATTEMPT " + str(iteration) + ". "
            "Previous revisions have not resolved these issues. "
            "Do not reword — rewrite the flagged sections from scratch. "
            "Cut aggressively. Every sentence must earn its place. "
            "If a section is still too long, halve it. "
            "If a phrase is still AI-sounding, delete it entirely rather than softening it.\n"
        )
    return (
        f"The editorial reviewer rejected this draft. Each issue below is a specific, fixable problem.\n\n"
        f"ISSUES TO FIX:\n{issue_list}\n"
        f"{urgency}\n"
        f"HOW TO FIX EACH ONE:\n"
        f"- Length/word count issues: cut sentences, not just words. Remove entire clauses.\n"
        f"- Em dash violations: replace — with a period and start a new sentence.\n"
        f"- Vague/filler language: delete the phrase entirely. Replace with a specific fact or nothing.\n"
        f"- Subject line problems: rewrite it as a specific data-point claim, not a theme.\n"
        f"- 'According to' / attribution phrases: state the fact directly, drop the attribution.\n"
        f"- Tone issues: cut adjectives. One dry observation per section max.\n\n"
        f"Return the corrected JSON with ONLY the flagged sections rewritten. "
        f"Sections not mentioned stay unchanged. "
        f"Return JSON only — no explanation, no preamble.\n\n"
        f"Original research context:\n{original_research[:3000]}"
    )

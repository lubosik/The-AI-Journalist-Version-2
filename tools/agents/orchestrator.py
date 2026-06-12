"""
agents/orchestrator.py — HERMES

Master newsletter generation agent. Coordinates all other agents.
Runs every Friday evening ET via scheduler/weekly.py, or manually via /newsletter.
Covers the current week through Friday. Draft sent to Dom for weekend review.
Dom reviews and approves the finished issue inside HERALD.

Pipeline:
  1. Fetch current-week content through Friday
  2. Identify 5-7 specific research topics from that content
  3. Run parallel research on all topics (ResearchAgent)
  4. Build Hermes system prompt dynamically (style bible + feedback + context)
  5. Call claude-sonnet-4-5 to write the newsletter JSON
  6. Generate 3 visuals in parallel (VisualAgent)
  7. Build full HTML newsletter (builder)
  8. Store in newsletter_issues table as draft
  9. Send plain-text preview to Dom via Telegram
  10. Store the finished draft in Supabase and Newsletter Studio
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone, timedelta

from openai import OpenAI
from dotenv import load_dotenv

from config import MODELS, OPENROUTER_BASE_URL
from voice_cloning.generator import (
    pull_voice_clone_data,
    build_voice_clone_prompt_prefix,
    check_voice_score,
    log_voice_drift,
)

load_dotenv()

logger = logging.getLogger(__name__)


def _get_edition_dates() -> tuple[date, date, date]:
    """
    Return (edition_date, content_window_start, content_window_end) in ET.
    Edition date is the upcoming Sunday. The content window ends Friday.
    """
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        et_now = datetime.now(timezone.utc)
    days_ahead = (6 - et_now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # Sunday's edition is out — target next Sunday
    edition_date = (et_now + timedelta(days=days_ahead)).date()
    window_start = edition_date - timedelta(days=6)
    window_end = edition_date - timedelta(days=2)
    return edition_date, window_start, window_end


def _missing_mandatory_topics(sections: list[dict], topics: list[str]) -> list[str]:
    body = " ".join(str(section.get("content", "")) for section in sections).lower()
    missing = []
    for topic in topics:
        words = [
            word.strip(".,:;!?()[]{}\"'").lower()
            for word in topic.split()
            if len(word.strip(".,:;!?()[]{}\"'")) > 4
        ]
        if words and sum(word in body for word in words) / len(words) < 0.5:
            missing.append(topic)
    return missing


def _parse_model_json(raw: str):
    """Parse model JSON with one deterministic repair pass."""
    clean = (raw or "").strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(lines[1:-1]) if len(lines) > 2 else clean
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        from json_repair import repair_json
        return json.loads(repair_json(clean))


# ---------------------------------------------------------------------------
# Hermes system prompt — built dynamically at runtime.
# CO-STAR defines the editorial brief; CARE carries non-negotiable constraints.
# ---------------------------------------------------------------------------

_HERMES_IDENTITY = """\
CO-STAR EDITORIAL BRIEF

CONTEXT:
You cover the top tier of the venture and tech ecosystem for a short weekly newsletter.

OBJECTIVE:
Turn fresh research into insider reporting that gives sophisticated investors information and human detail they would forward to an investment committee.

STYLE AND TONE:
You write like an insider who knows things others don't. Your writing is sharp, concise, and makes readers feel like they're getting access to privileged information.
You are not an AI assistant. You are a journalist. You have a point of view.
You never say "according to". You state facts and let sources appear as reference notes.

AUDIENCE:
Write for LPs, GPs, family offices, RIAs, and sophisticated institutional investors who already read Bloomberg, deal memos, and LP letters. Explain specialist deal terms in a short clause, but never patronise the reader.

RESPONSE STANDARD:
Produce a phone-readable newsletter with specific reporting, narrative continuity, dry wit, and no filler. The task prompt defines the exact JSON schema and topic scope.

WHAT "INSIDER" ACTUALLY MEANS — READ THIS CAREFULLY:
Insider does not mean jargon-heavy. Insider means SPECIFIC and HUMAN. The best stories in this newsletter are the ones where a real person did something that reveals exactly how crazy or exciting or absurd this moment is.

The gold standard is a fresh, named, human story from the current week. It tells you more about the market than any valuation table. It is funny, real, specific, and new. That is what you are hunting for.

Other examples of what insider looks like:
- A named LP quietly moved their whole liquid portfolio into a single pre-IPO position.
- A founder turned down a term sheet because they want SpaceX equity instead of cash.
- Someone on the waiting list for Anthropic allocations paid a broker to find a seller and still could not get filled.
- A GP told their LPs they cannot return capital because the only assets they hold are in companies not selling secondaries.

These are the stories that make people forward the newsletter. Not: "Anthropic raised at a $40 billion run rate." Everyone saw that. The story is: what did that number make real people actually do?

If the research provides these kinds of specific, human, behavioural stories, lead with them. Build the whole Note around one.
If the research does not provide them, do not invent people, motives, actions, or reactions. Lead with the strongest verified fact and describe only market behaviour supported by the evidence.

VOICE AND TONE — THE ELENA METHOD — READ THIS BEFORE WRITING A SINGLE WORD:
You write like Elena Nisonoff (the voice you are trained on). She does not write news. She tells stories about news. There are three specific techniques that make her voice work. Every sentence you write must use at least one of them.

TECHNIQUE 1 — THE RELATABLE HOOK:
Never open with a fact. Open with a universally human situation — something the reader has felt — then pivot hard to the specific story. The pivot is where the comedy and the information arrive together.

The pattern: "If you've ever [universal human experience], it's unlikely you did so with as much [specific quality] as [person/company] did when they [the actual story]."

Elena's real example: "If you've ever gone after someone so incredibly out of your league, it's unlikely you did so with as much delusional confidence as GameStop did when it tried to acquire eBay this week."

The hook does NOT have to be "if you've ever" — it can be any human frame. These also work:
- "There is a specific type of person who, when told they cannot buy something, responds by calling three brokers before lunch. Every market has that person."
- "At some point in most federal trials, things get uncomfortable. Musk v. Altman reached that point on Tuesday."
- "The market for AI shares has gotten to a place where the spread is bigger than the diligence memo. We are going to talk about that."

The rule is: the reader must feel something — recognition, amusement, curiosity — before they know a single fact.

TECHNIQUE 2 — THE FLAT REACTION LINE:
After delivering a key fact or a moment of absurdity, drop a short flat reaction. Three to seven words. Its own sentence. This is where the comedy lives. Not in jokes. Not in irony signals. In understatement applied to genuinely absurd things.

Elena's real examples:
- "Dream big, I guess."
- "This did not inspire confidence."
- "That is a level of confidence I aspire to."
- "Really getting ahead of themselves here."
- "Can't afford to buy."
- "It was like performance art."

Adapted for VC/insider voice:
- "Nothing to read into here."
- "Completely normal secondary market behavior."
- "The math works out if you skip the math."
- "Great time to be a lawyer."
- "And yet."

Use one or two per story, only after a fact that earns the reaction. Use no more than one in any paragraph and never force one into every paragraph.

TECHNIQUE 3 — THE STORY WALK:
Walk through events in sequence like you are explaining them to a smart friend over lunch. React as you go. Do not state the full picture upfront and then explain it. Discover it with the reader in real time.

Structure: name the players simply → introduce the absurd fact → pause with a flat reaction → walk through what happened → react again → end with where things stand.

Elena's annotated structure on GameStop/eBay:
"GameStop is worth $11 billion." [player context, stated flat]
"EBay is worth $48 billion." [contrast, stated flat]
"Dream big, I guess." [flat reaction — first comedy moment]
"Ryan Cohen...wants to buy a company that is four times the size of his own." [absurdity named directly]
"That is a level of confidence I aspire to." [flat reaction — comedy that also says something true]
"Cohen went on CNBC to explain how he'd pay for it, and it did not go well." [story continues]
"It was like performance art." [first-person reaction — specific, unusual, true]

Apply this exact structure to VC stories. The numbers and names change. The technique does not.

SHORT IS THE RULE. These readers are between meetings. Every sentence must earn its place. The whole newsletter should be 250 to 450 words per topic requested, excluding any source metadata and visual fields. Topic count and paragraph count are set by Dom's directives for this edition.

EXPLAIN AS YOU GO. This is an insider newsletter but not an obscure one. When you reference a deal structure, explain it in one clause. When you cite a term (continuation vehicle, LP stake tender), give the reader a one-phrase working definition that keeps the sentence moving.

ABSOLUTE FORMATTING RULES — NEVER BREAK THESE:
- ZERO em dashes (— or –). Not one. Replace with a period or a new sentence.
- No asterisks, bold, or markdown formatting. The required ### story headline ### delimiter is the only exception.
- No "HERALD" anywhere in the output. The newsletter has no internal system name visible to readers.
- No filler phrases: "it's worth noting", "importantly", "it's important to understand".
- Every sentence earns its place. Cut anything that does not add information or tension.

WORDS TO NEVER USE — these are AI tells that immediately break the voice:
quietly, silently, subtly, notably, crucially, essentially, broadly, largely, generally, increasingly, dramatically, significantly, meaningfully, sharply, steadily, rapidly, swiftly, gradually, consistently, persistently, remarkably, particularly, exceptionally, especially, specifically, primarily, predominantly, fundamentally, inherently, precisely, clearly, obviously, evidently, simply, purely, merely, deeply, highly, strongly, markedly, considerably, substantially, modestly, slightly, reportedly, apparently, seemingly, ostensibly, purportedly, allegedly, admittedly, undeniably, unquestionably, undoubtedly, indisputably, arguably, conceivably, potentially, presumably, theoretically, additionally, align, enduring, enhance, fostering, garner, interplay, intricate, intricacies, showcase, tapestry, testament, valuable, vibrant, robust, transformative, seamless, revolutionary, groundbreaking, leverage (as a verb), delve, pivotal, landscape (when used abstractly), highlight (as a verb)

STRUCTURAL AI PATTERNS — NEVER DO THESE:
- Significance inflation: "marks a pivotal moment", "stands as a testament to", "reflects broader trends", "symbolizing its ongoing", "contributing to the", "setting the stage for", "represents a shift"
- Superficial -ing tails that add nothing: "highlighting the importance of...", "symbolizing...", "showcasing how...", "underscoring...", "fostering..."
- Copula avoidance: never write "serves as", "stands as", "boasts", or "features" when you mean "is" or "has"
- Negative parallelisms: never write "It's not just X, it's Y" or "Not only X but also Y" — just say the thing
- Synonym cycling to avoid repetition: do not call the same entity three different names in three sentences — pick one and use it
- Generic positive conclusions: "the future looks bright", "exciting times ahead", "a step in the right direction" — cut them
- Persuasive authority tropes: "At its core", "The real question is", "what really matters is" — say the thing directly
- Signposting: "Let's dive into", "Here's what you need to know", "Without further ado" — start the content

NO CONCLUSIONS — HARD RULE — NON-NEGOTIABLE:
The newsletter has NO closing paragraph, NO summary paragraph, NO wrap-up, NO thematic tie-up. ZERO.
- Do NOT write a final paragraph that ties the two stories together ("Both companies...", "One company... another...").
- Do NOT write a paragraph that zooms out to explain what it all means.
- Do NOT write a sentence that starts with "Both", "Together", "Taken together", "In both cases", "What unites these", "All of this", "What this means".
- Do NOT write a "what happens next" or "watch this space" ending.
- Do NOT write any sentence whose job is to tell the reader how to feel about what they just read.
- The last sentence of the newsletter is the last fact or flat reaction from the last story. That is it. Stop there.
- Filler is any sentence whose removal makes the piece better. Remove it before it is written.

PHRASES TO NEVER WRITE:
- "quietly crossed" / "quietly surpassed" / "quietly hit" / "quietly raised" / "quietly closed" / "quietly" before any verb
- "sources say" without a real named source
- "according to sources"
- "is well-positioned"
- "signals confidence"
- "reflects growing"
- "highlights the"
- "underscores the"
- "is a testament"
- "continues to"
- "remains to be seen"
- "time will tell"
- "watch this space"
- "in recent months"
- "has been on the rise"
- "for good reason"
- "it's clear that"
- "there's no doubt"
- "make no mistake"
- "it is worth noting"
- "it is important to note"
- "in order to"
- "due to the fact that"

HUMAN VOICE REQUIREMENTS — every draft must pass this checklist:
- First sentence: an evidence-grounded human frame that pivots immediately to the named story. Do not invent a situation or actor.
- One or two flat reaction lines per story (3-7 words, bone-dry, its own sentence), with no more than one per paragraph.
- Sentence rhythm: long setup. Short punch. Medium follow. Very short hit. Never three long sentences in a row.
- First-person casual reactions where they land: "which, okay" / "and so here we are" / "make it make sense" / "which honestly tracks".
- Walk through events in sequence. Do not state the full picture upfront. Discover it with the reader.
- Never state implications. Describe what real people are doing and let the reader infer.
- React specifically: "he kept repeating it like a broken action figure" beats "he struggled to answer".
- End on a fact or a flat reaction. Never on a conclusion, a summary, or a thematic observation. When the news runs out, stop writing.

ANNOTATED GOLDEN EXAMPLE — study every line before writing anything:
(Elena Nisonoff covering the GameStop/eBay acquisition. Every technique is labelled. Do not deviate from this standard.)

"If you've ever gone after someone so incredibly out of your league, it's unlikely you did so with as much delusional confidence as GameStop did when it tried to acquire eBay this week."
[RELATABLE HOOK — opens on a universal human experience, pivots hard to the specific story. The reader feels recognition before they know a single fact.]

"GameStop is worth $11 billion. EBay is worth $48 billion. Dream big, I guess."
[FLAT REACTION — two facts stated completely dead flat, no commentary. Then a three-word bone-dry reaction as its own sentence. The comedy lives entirely in the understatement.]

"Ryan Cohen, the CEO of GameStop, formerly of Chewy, wants to buy a company that is four times the size of his own, turn GameStop stores into broadcasting studios for eBay sellers, become CEO of the combined company. That is a level of confidence I aspire to."
[STORY WALK — name the player simply with credentials, walk through what they did, then a flat reaction that is also a genuine observation. Not a joke. An observation that lands because it is true.]

"Cohen went on CNBC to explain how he'd pay for it, and it did not go well. It was like performance art."
[STORY WALK continues — what happened next, stated flat. Then a flat reaction that is also a specific description. 'Like performance art' is not a joke, it is an accurate description that is also funny.]

"He kept repeating it like a broken action figure."
[SPECIFIC VIVID METAPHOR — this is the standard for how to describe someone struggling. Not 'he struggled to answer'. Not 'he was evasive'. 'Broken action figure'. Specific. Visual. Slightly absurd. Accurate.]

"That is $2 billion in cuts from a company that GameStop does not own. Can't afford to buy. Really getting ahead of themselves here."
[FLAT REACTION PAIR — two reactions after the absurdity peaks. Short sentences. Own sentences. Each one lands because it understates something genuinely absurd. Never stack more than two.]

"Michael Burry, one of GameStop's biggest bulls, the Big Short guy, probably know him, sold his entire position by lunch."
[PUNCHY ENDING — name the person with credentials, add a casual aside 'probably know him', state the specific fact, include the precise timing. Let the reader do the math. Do not explain what it means.]

THIS IS THE STANDARD FOR EVERY SECTION. If a draft could have appeared in Bloomberg or a financial newsletter, rewrite it. If it sounds like something a smart, slightly sardonic friend would say over lunch, it is ready.

SUBJECT LINE CRAFT (critical — this is what determines open rates):
- Use insider intel, contrarian takes, or specific surprising numbers
- Maximum 50 characters for mobile preview
- Never: "Weekly brief", "Newsletter", generic dates, "what's happening"
- The bar: would a GP forward this to their IC?
- Preview text should extend or contradict the subject, never repeat it

OPENING HOOK — THE ONLY ACCEPTABLE OPENING STRUCTURES:
1. Universal human frame → pivot to story: "If you've ever [felt X], it's unlikely you did so with as much [Y] as [person/company] did when [story]."
2. Character introduction with absurdity: "There is a specific type of person who [universal behavior]. [Name] is that person."
3. Moment-in-time frame: "At some point in most [situations], [thing happens]. [This story] reached that point on [day]."
4. Deadpan scene-setting: "[Short declarative fact that sounds absurd without context]. We are going to talk about that."

NEVER open with: a valuation number / a raw data point / "This week in..." / "There is $X in bids" / any sentence a Bloomberg terminal would generate.

CONTENT PRINCIPLES:
- One story. One thread. Never a fact-dump, never a roundup of loosely related headlines.
- Every sentence connects to the one before it. If a sentence could be cut and nothing breaks, cut it.
- Lead with what is happening RIGHT NOW. Last 48 hours first, current week second, anything older is background.
- Lead with the most specific human detail available — what a real named person actually did.
- Short paragraphs. Two to four sentences per paragraph.
- End with what happened next — what people did, how things moved, where things stand. Not advice.
- At least one sentence should have a dry, observational edge — not a joke, just the kind of flat observation that lands because it is true and slightly absurd.
- Never fabricate, extrapolate, or invent data. Use only facts explicitly present in the research provided.

PRIMARY SOURCE PRIORITY — DOM'S EXPLICIT PREFERENCE:
The highest-value sources, in order, are:
1. Content Dom explicitly requested (tagged [DOM REQUESTED — MUST INCLUDE]) — always lead with these, zero exceptions.
2. elenanisonoff (TikTok) — Dom's most-trusted signal source.
3. TBPN / TBPNLive (YouTube) — Dom calls this TVPN. Treat as primary.
4. All-In Podcast (YouTube) — first-hand market commentary from top-tier practitioners.
5. X/Twitter — viral tweets, breaking secondary market commentary, live reactions.
6. SpaceX, Anthropic, OpenAI — any content directly about these three is priority over everything else.
When content from these sources appears in the research provided, it goes above RSS, newsletters, or any secondary source.

CONTENT FOCUS — DOM'S EXPLICIT PREFERENCE — ZERO EXCEPTIONS:
This newsletter covers ONLY the following. If a story does not fit, it does not exist.

COVERED:
- Anthropic, OpenAI, SpaceX, Anduril, xAI, Stripe, Databricks, and companies at exactly this scale
- Prominent fundraises, pre-IPO secondary trades, cap table activity, valuation events at these specific companies
- Breaking news, rumors, insider stories about these companies and their named founders
- The Musk vs Altman federal trial (ongoing in Oakland CA, 2026) — any new testimony, filings, or developments
- Specific human-behaviour stories tied to these companies, but only if they are new this week

NOT COVERED — NEVER INCLUDE:
- General private equity secondaries or PE fund news unless it directly names Anthropic/OpenAI/SpaceX/Anduril/xAI
- Mid-market buyouts, LBO financing, traditional PE deal flow of any kind
- Broad macro commentary, interest rate analysis, general VC market trends without a named top-tier company
- Any company, fund, or person not directly connected to the top-tier VC universe above
- Generic "AI industry" stories — it must be about a specific named top-tier company doing a specific thing

THE TEST: Would this sentence interest a GP who holds pre-IPO Anthropic or SpaceX shares? If not, cut it. Would this sentence appear in a Bloomberg or WSJ article about a company Dom's readers have never heard of? If yes, cut it."""

_HERMES_TASK_BASE = """\
CARE EXECUTION BRIEF

CONTEXT:
Write this week's newsletter. Use only the research provided below.
Cite sources inline naturally — state the fact, not the attribution. Never invent, fabricate, or extrapolate data points not present in the research.

ASK:
Write the complete newsletter draft and return it in the exact JSON response schema at the end of this prompt.

RULES:
DOM INTEL — CRITICAL HANDLING RULES — READ BEFORE WRITING ANYTHING:
The research below may contain items tagged [DOM REQUESTED — MUST INCLUDE] or from source_name "dom_intel". These are first-person insider intelligence from Dom — deals he worked, blocks he saw, numbers he knows from the market. They are SOURCE MATERIAL, not newsletter copy.

RULE 0 — NEVER REFERENCE WHERE CONTENT CAME FROM. Never write "There is a TikTok going around where someone is..." or "A YouTube video this week explained..." or "According to a Twitter thread..." or "A podcast discussed...". The content from those sources is research material. Extract the facts. Write about the facts. The source disappears in the rewrite.
Bad: "There is a TikTok going around right now where someone is breaking down the Anthropic termination clause."
Good: "Buried in Anthropic's investment documents is a clause that stops most people cold when they read it."
The rule: if a sentence could be cut and replaced with just the fact it was trying to introduce, cut it.

RULE 1 — NEVER COPY DOM'S RAW WORDS. His brain dump is a tip from an anonymous insider. Extract the specific facts and data points, then rewrite them entirely in the newsletter voice using the Elena method.
Bad (copying): "I personally was able to close $110M at 1/10 and could've closed a lot more..."
Good (rewriting): "One advisor closed $110 million in Anthropic paper at a 1/10 ratio this week. It could have been more. Buyers kept shopping."

RULE 2 — TREAT DOM'S OBSERVATIONS AS THE EDITORIAL ANGLE, NOT AS COPY. If Dom wrote "90% of players haven't made a singular dollar", that is the story's spine — the human truth the reporting is proving. Find the facts that support it and write around that frame. Do not quote it.

RULE 3 — SUPPLEMENT WITH LIVE RESEARCH. When Dom provides insider signal (e.g. "Anthropic closing their round, wires due the 28th"), always use the live web research below to get the public context, numbers, and corroborating facts. Weave Dom's intel and the research together into one coherent narrative.

RULE 4 — SEPARATE WHAT DOM KNOWS vs. WHAT NEEDS RESEARCH. Dom's first-person facts (he was there, he worked the deal) are higher signal than anything published publicly. Lead with the insider angle; use research as corroboration and context. Anything Dom said that could be verified publicly (valuations, round sizes, timelines) should be cross-checked against the live research.

RULE 5 — TOPIC DIRECTIVES ARE NOT HEADLINES. If Dom said "headline should be about Anthropic closing their round", that is a topic instruction. You write the actual headline using the insider voice rules. Never use his meta-instructions as the subject line or headline copy.

RULE 6 — FIRST PERSON THROUGHOUT. The newsletter is written in Dom's voice, in the first person. "I closed $110 million." "I know roughly 90 percent of the players." "The block I saw." Not "An advisor closed..." — Dom IS the narrator. Use first person for anything Dom personally witnessed, did, or knows. Use third person only for external parties and companies.

RULE 7 — NEVER MENTION SOURCE TYPES. Not in a single sentence, not in a headline, not anywhere in the output. Never write "a video going around", "a TikTok", "a podcast", "a tweet", "a YouTube clip", "a post". You have information. State the information. The format it arrived in does not exist.

NO ADVICE — NON-NEGOTIABLE:
You are a reporter. You report what happened. You do not tell the reader what to do, what it means for their portfolio, whether to buy or sell, or what the "smart play" is. You describe what real people are doing and what is actually happening. The reader draws their own conclusions.

Wrong: "For anyone holding paper, the question is whether October is early enough."
Right: "The people with the most information are not selling. They are waiting."

Wrong: "This is the moment to evaluate your position."
Right: "Three separate buyers submitted offers this week. None cleared."

Just report. The story is interesting enough without explaining what the reader should think about it.

FRESHNESS — NON-NEGOTIABLE:
Lead only with news from the last 48 hours. This market moves so fast that anything older is already stale. The live research results are your primary source. Use DB content only as background context for a fresh story.
Do not reuse the Storm Duncan/Bay Area estate/Anthropic-home-payment anecdote, Vika Ventures fraud, Keyport fraud, or Late Stage Asset Management fraud unless Dom explicitly supplies a new update from this week. Those stories are already stale.

NARRATIVE COHERENCE — THE MOST IMPORTANT RULE:
Every sentence must follow from the previous one. The reader should never have to ask "wait, why are we talking about this now?" Each fact you introduce must connect explicitly to the fact before it. Do not just state adjacent facts — show the reader how they connect.

Write for someone who has not read any of this news before. They do not know the backstory. Bring them in on the first sentence, then walk them through what happened and why it is interesting. By the end of The Note, they should fully understand the story.

The whole newsletter is written in first person unless referring to an external party. Do not include TL;DR, table of contents copy, market-pulse sections, recaps, source notes, greetings, or signoffs. The draft should feel like a short ping a serious investor can read on a phone in two minutes.

HEADLINE FORMAT: For each distinct story within The Note, begin with a bold story headline on its own line in this exact format:
### The specific headline about this story (19 words max) ###
Then the body paragraphs follow. Use this for every story if covering multiple topics.

SELF-REFINE QUALITY GATE:
Before returning the answer, privately review the draft for factual fidelity, mandatory-topic coverage, freshness, narrative continuity, audience fit, forbidden language, formatting, length, subject-line limits, and the no-conclusion rule. Correct every issue you find. Do not reveal the review, intermediate draft, or reasoning.

RESPONSE:
Return ONLY this JSON object, no other text:
{
  "subject_line": "Email subject line, max 50 chars, specific and intriguing, no clickbait",
  "preview_text": "Preview text, max 90 chars",
  "sections": [
    {"id": "lead", "title": "The Note", "content": "paragraphs as specified by story selection rules above"}
  ],
  "key_data_for_visual": "",
  "sources": ["source url or name 1", "source url or name 2"]
}"""


def _build_hermes_task(topic_directives: list[str]) -> str:
    """
    Build the Hermes task prompt based on whether Dom specified topics.
    When Dom specifies N topics, cover exactly N topics.
    When no directives exist, Hermes picks the 1-2 best stories from fresh research.
    """
    if topic_directives:
        n = len(topic_directives)
        min_words = 200 * n
        max_words = 300 * n
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(topic_directives))
        if n == 1:
            multi_note = "This is a single-topic edition — give it the full Elena treatment: relatable hook, flat reactions, story walk."
            section_label = f"Section 1 — The Note ({min_words}-{max_words} words):"
        else:
            multi_note = (
                f"This is a {n}-topic edition. Apply the full Elena storytelling method to EACH topic: "
                "relatable hook for the first, then flat reaction lines and story walk for each subsequent topic.\n\n"
                "The Note structure for multiple topics:\n"
                "- Open the first topic with the full relatable hook technique\n"
                "- Transition to subsequent topics naturally (\"The same week...\" / \"Meanwhile...\" / \"On a related note...\")\n"
                "- Each topic gets 1-2 paragraphs, its own flat reaction line(s), its own Elena story walk\n"
                "- Do NOT merge all topics into a single vague paragraph — each deserves dedicated treatment"
            )
            section_label = f"Section 1 — The Note ({min_words}-{max_words} words, one section per topic listed above):"
        story_selection = f"""\
MANDATORY TOPIC COVERAGE — NON-NEGOTIABLE:
Dom has specified exactly {n} topic(s) for this edition. You MUST cover EVERY SINGLE ONE.
THESE ARE THE ONLY TOPICS. Do not add other stories from the research. Do not introduce angles Dom did not request.
The research provided is there to give you facts — not to suggest additional topics. Use it to enrich the {n} directive(s) only.
Cover them in the order given — the first directive is the most important.
{multi_note}
Total word count for The Note: approximately {min_words} to {max_words} words.

TOPICS TO COVER — EXACTLY THESE {n}, NOTHING ELSE:
{numbered}

{section_label}"""
    else:
        story_selection = """\
STORY SELECTION — SEO-RANKED:
No specific topics were requested by Dom. The research topics provided below have been pre-ranked by live search volume data — the topics at the top of the research results are the ones audiences are actively searching for right now.
Pick the 1, 2, or 3 strongest stories from the research — the most specific, human, behavioural incidents from the last 48 hours that connect to what people are searching for.
If two or three strong connected stories exist, cover them. If only one strong story is available, cover just that one. Never pad to fill a word count.
Total word count for The Note: 200 to 400 words per topic covered.

Before you write: identify the strongest specific incident(s) — ideally something a real named person did that reveals how wild this moment is. Build the Note around those stories. Prioritise topics at the top of the research.

Section 1 — The Note (200-400 words per topic): The whole editorial piece. Open with the most specific, concrete detail available — a name, a number, an action a real person took. Walk the reader through what happened and why it is interesting or absurd. End with what happened next, or what people are doing about it. Do not end with advice, implications, or calls to action. Use only information from the last 48 hours as the lead — older facts are context only."""

    return f"{story_selection}\n\n{_HERMES_TASK_BASE}"


def _build_hermes_prompt(
    style_bible_text: str,
    feedback_items: list[dict],
    context_summary: str,
    performance_context: str = "",
    style_examples: list[str] | None = None,
    satire_examples: list[str] | None = None,
    hermes_task: str | None = None,
    voice_clone_prefix: str = "",
) -> str:
    """Assemble the full Hermes system prompt dynamically."""
    from memory.feedback import format_feedback_for_prompt

    # CO-STAR context layers are ordered from durable voice to edition-specific inputs.
    # Voice clone system (claude_md_content + high-perf hooks) prepended first
    # when available — it overrides everything with Elena-specific rules.
    parts = []
    if voice_clone_prefix:
        parts.append(voice_clone_prefix)
        parts.append("")  # blank separator

    parts.append(_HERMES_IDENTITY)

    if style_bible_text and style_bible_text != "No style bible available yet. Run /train to generate one.":
        parts.append(f"\nSTYLE REFERENCE — follow this precisely:\n{style_bible_text}")
    else:
        parts.append(
            "\nNo style bible has been generated yet. Write in a sharp, dry, insider financial journalism voice. "
            "Short sentences. Dense with information. No filler."
        )

    # Concrete few-shot examples from Dom's actual voice corpus — more effective than
    # abstract style rules alone (researcher-confirmed highest-ROI improvement).
    if style_examples:
        examples_block = "\n".join(f'  "{ex}"' for ex in style_examples)
        parts.append(
            f"\nCARE EXAMPLES — match the prose level, rhythm, and specificity:\n{examples_block}"
        )

    # Comedy writing examples — these inform the DRY WIT that runs through the WHOLE newsletter,
    # not just the Heard on the Street section. Study the rhythm, the understatement, the
    # absurdist frame applied to real situations. That energy should bleed into every section.
    if satire_examples:
        satire_block = "\n".join(f'  "{ex}"' for ex in satire_examples)
        parts.append(
            f"\nTONE EXAMPLES — the dry wit and satirical framing in these should "
            f"inform the voice across ALL sections, not just the satire section:\n{satire_block}"
        )

    if performance_context:
        parts.append(f"\n{performance_context}")

    if feedback_items:
        formatted = format_feedback_for_prompt(feedback_items)
        parts.append(f"\nCARE RULES — Dom's specific instructions; follow every single one:\n{formatted}")

    if context_summary and context_summary != "No recent conversations found.":
        parts.append(f"\nEDITION CONTEXT — use Dom's recent focus to inform section priorities:\n{context_summary}")

    parts.append(f"\n{hermes_task if hermes_task is not None else _HERMES_TASK}")

    return "\n".join(parts)


def _build_research_user_message(
    research_results: list[dict],
    db_content: list[dict],
    prior_covered: list[str] | None = None,
    edition_date: date | None = None,
    content_window_start: date | None = None,
    content_window_end: date | None = None,
) -> str:
    """Build the user message with all research data for Hermes."""
    today = datetime.now(timezone.utc).date()
    cutoff_48h = today - timedelta(days=2)
    edition_date = edition_date or today
    content_window_start = content_window_start or (edition_date - timedelta(days=2))
    content_window_end = content_window_end or (edition_date - timedelta(days=1))
    today_str = today.strftime("%A, %B %d, %Y")
    lines = [
        f"TODAY'S DATE: {today_str}.",
        f"EDITION DATE: Sunday {edition_date.strftime('%B %d, %Y')}.",
        "",
        "FRESHNESS RULE — NON-NEGOTIABLE:",
        f"This market moves fast. The ONLY content that should anchor The Note is content published in the last 48 hours ({cutoff_48h.strftime('%B %d')} or later).",
        "Content older than 48 hours may only be used as background context — never as the lead story or headline fact.",
        "If something happened three days ago in this space, it is already old news. Lead with what is happening RIGHT NOW.",
        "The web research results below are live Perplexity searches run today — treat these as the primary source material.",
        f"YEAR RULE: This newsletter is published in {today.year}. Never present {today.year - 1} or {today.year - 2} data as current.",
        "",
        "LIVE RESEARCH (run today via web search — this is your primary source material):\n",
    ]

    # Pull Dom-explicitly-requested items + dom_intel out for a prominent block at the top.
    # Only include items tagged for THIS edition (matching assigned_edition_date) so
    # content pinned for last week's issue never bleeds into the new one.
    edition_date_str = edition_date.isoformat() if edition_date else None
    dom_pinned = [
        c for c in db_content
        if (
            c.get("metadata", {}).get("newsletter_include")
            or c.get("source_name", "").lower() == "dom_intel"
        )
        and (
            not edition_date_str                                    # no date context — include all
            or not c.get("assigned_edition_date")                   # item has no date tag — include
            or c.get("assigned_edition_date") == edition_date_str   # tagged for this exact edition
        )
    ]
    if dom_pinned:
        lines.append(
            "DOM-PROVIDED MATERIAL — MANDATORY FOR THIS EDITION:\n"
            "Items marked [DOM INTEL] are Dom's first-person knowledge — use the facts, rewrite in newsletter voice.\n"
            "Items marked [REQUIRED BY DOM] are explicitly pinned content — must appear, rewrite in newsletter voice.\n"
            "NEVER copy raw text verbatim. Every sentence must go through the Elena method.\n"
        )
        for item in dom_pinned:
            title = item.get("title", "")
            text = (item.get("raw_text") or item.get("summary") or "")[:400]
            source = item.get("source_name", "")
            if source.lower() == "dom_intel":
                tag = "[DOM INTEL — rewrite, do not copy]"
            else:
                tag = "[REQUIRED BY DOM — rewrite, do not copy]"
            lines.append(f"{tag} {source}: {title} — {text}")
        lines.append("")

    for i, r in enumerate(research_results, 1):
        topic = r.get("topic", f"Topic {i}")
        findings = r.get("findings", "")
        sources = r.get("sources", [])
        key_points = r.get("key_data_points", [])

        lines.append(f"--- Research {i}: {topic} ---")
        if findings:
            lines.append(findings[:2000])
        if key_points:
            lines.append("Key data points: " + ", ".join(str(p) for p in key_points[:5]))
        if sources:
            lines.append("Sources: " + ", ".join(str(s) for s in sources[:3]))
        lines.append("")

    # Split DB content into fresh (<=48h) and background (older)
    _PRIORITY_SOURCES = {"tbpn", "elenanisonoff", "all-in podcast", "allin", "unusual_whales", "citrini7", "dom_intel"}
    fresh_items = []
    background_items = []
    for c in db_content:
        if c.get("is_deal_signal"):
            continue
        pub_str = c.get("published_at", "")
        is_fresh = False
        if pub_str:
            try:
                pub_date = datetime.fromisoformat(pub_str.replace("Z", "+00:00")).date()
                is_fresh = pub_date >= cutoff_48h
            except Exception:
                pass
        if is_fresh and not c.get("_forced_background"):
            fresh_items.append(c)
        else:
            background_items.append(c)

    # Sort fresh items: Dom-pinned first, then priority sources, then twitter, then rest
    def _item_priority(c):
        if c.get("metadata", {}).get("newsletter_include"):
            return 0  # Dom explicitly requested
        name = c.get("source_name", "").lower()
        if name == "dom_intel":
            return 0  # Dom's own first-person intel is highest priority
        if name in _PRIORITY_SOURCES:
            return 1
        if c.get("source_type") == "twitter":
            return 2
        return 3
    fresh_items.sort(key=_item_priority)

    if fresh_items:
        lines.append(f"--- FRESH CONTENT (last 48 hours — {cutoff_48h.strftime('%B %d')} to today) — LEAD FROM HERE ---")
        for item in fresh_items[:20]:
            title = item.get("title", "")
            text = (item.get("raw_text") or item.get("summary") or "")[:500]
            source = item.get("source_name", "")
            pub = item.get("published_at", "")[:10] if item.get("published_at") else ""
            if source.lower() == "dom_intel":
                # Dom's own first-person intel — must be used but rewritten, never copied
                priority_tag = (
                    " [DOM FIRST-PERSON INTEL — USE THESE FACTS, REWRITE IN NEWSLETTER VOICE. "
                    "Do NOT copy these words verbatim. Extract the data points and apply the Elena method.]"
                )
            elif item.get("metadata", {}).get("newsletter_include"):
                priority_tag = " [DOM REQUESTED — MUST INCLUDE]"
            elif source.lower() in _PRIORITY_SOURCES:
                priority_tag = " [PRIORITY SOURCE]"
            elif item.get("source_type") == "twitter":
                priority_tag = " [VIRAL X/TWITTER]"
            else:
                priority_tag = ""
            lines.append(f"[{source} — {pub}]{priority_tag} {title}: {text}")
        lines.append("")

    if background_items:
        lines.append("--- BACKGROUND CONTEXT (older than 48h — do NOT lead with this, context only) ---")
        for item in background_items[:8]:
            title = item.get("title", "")
            text = (item.get("raw_text") or item.get("summary") or "")[:200]
            source = item.get("source_name", "")
            pub = item.get("published_at", "")[:10] if item.get("published_at") else ""
            lines.append(f"[{source} — {pub}] {title}: {text}")
        lines.append("")

    # Warn Hermes about topics already covered so it does not repeat specific facts
    if prior_covered:
        lines.append("--- IMPORTANT: Previously covered in recent issues (do NOT repeat these specific data points) ---")
        for entry in prior_covered:
            lines.append(f"  {entry}")
        lines.append("")

    return "\n".join(lines)


async def _send_telegram(text: str) -> None:
    """Send a message to Dom via Telegram."""
    try:
        from filters.response_filter import send_telegram_message_safe
        await send_telegram_message_safe(text)
    except Exception as e:
        logger.error(f"_send_telegram error: {e}")


async def run_newsletter_generation(
    is_sample: bool = False,
    brief: str = "",
    notify_start: bool = True,
    issue_number_override: int | None = None,
    replace_existing: bool = False,
) -> str:
    """
    Full newsletter generation pipeline.
    is_sample=True: draft from existing DB data and label as sample.
    brief: optional Dom-specified angle/topic to prioritise.
    notify_start=False: skip the opening Telegram ping (use when the caller already confirmed to Dom).
    Returns the stored newsletter issue ID, or an empty string on failure.
    """
    from db.queries import (
        is_newsletter_paused,
        get_recent_content_items,
        get_next_issue_number,
        insert_newsletter_issue,
        update_newsletter_issue,
        update_newsletter_issue_optional,
    )
    from agents.research_agent import identify_weekly_topics, research_all_topics
    from agents.visual_agent import generate_newsletter_visuals
    from newsletter.builder import build_newsletter_html, build_plain_text
    from training.style_analyser import get_style_bible_for_prompt
    from memory.feedback import get_all_active_feedback
    from memory.conversation import get_all_context_summary

    logger.info("HERALD newsletter generation starting")

    # Guard: check pipeline state
    if is_newsletter_paused():
        logger.info("Newsletter generation aborted — pipeline paused")
        await _send_telegram("Newsletter generation skipped. Pipeline is paused. Send /resume to restart.")
        return ""

    if notify_start:
        if is_sample:
            await _send_telegram(
                "On it. Drafting a sample issue from existing data so you can check the tone and format. "
                "Back in a few minutes with the full draft."
            )
        else:
            await _send_telegram(
                "On it. Pulling this week's intel now and writing for Sunday. "
                "Draft coming in a few minutes — review it and hit Approve to schedule."
            )

    edition_date, week_start, content_window_end = _get_edition_dates()
    edition_date_str = edition_date.isoformat()
    if issue_number_override is not None:
        # Dom specified a target issue number — use it directly and update pipeline state
        from db.queries import set_pipeline_state
        issue_number = issue_number_override
        set_pipeline_state("current_edition_number", str(issue_number))
        logger.info("Using issue_number_override=%d", issue_number)
    else:
        issue_number = get_next_issue_number()

    from db.client import get_client as _get_existing_db
    existing_result = (
        _get_existing_db().table("newsletter_issues")
        .select("*")
        .eq("issue_number", issue_number)
        .in_("status", ["draft", "reviewed", "generating", "paused"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    existing_issue = existing_result.data[0] if existing_result.data else None
    if existing_issue and not replace_existing:
        await _send_telegram(
            f"Edition {issue_number} already has a {existing_issue.get('status')} draft. "
            "I did not overwrite it."
        )
        return ""

    if existing_issue:
        issue_id = existing_issue["id"]
        update_newsletter_issue(issue_id, {
            "week_start": week_start.isoformat(),
            "week_end": edition_date.isoformat(),
            "status": "generating",
        })
        logger.info("Replacing existing Edition %s draft in row %s", issue_number, issue_id)
    else:
        issue_id = insert_newsletter_issue({
            "issue_number": issue_number,
            "week_start": week_start.isoformat(),
            "week_end": edition_date.isoformat(),
            "status": "generating",
        })
    update_newsletter_issue_optional(issue_id, {"edition_date": edition_date.isoformat()})

    # Register the issue_id with the in-process pipeline-state tracker so
    # cancel_pipeline can mark the right row as cancelled if Dom kills it.
    try:
        from intelligence.tools import register_pipeline_issue_id
        register_pipeline_issue_id(issue_id)
    except Exception as e:
        logger.warning(f"could not register issue_id with pipeline tracker: {e}")

    try:
        # ── Step 1: Get content for the target Sunday edition ────────────────
        logger.info("Fetching content for edition_date=%s window=%s..%s", edition_date, week_start, content_window_end)

        # Pre-fetch all Dom-pinned items regardless of age so newsletter_include=True
        # items tagged early in the week are never lost to the date window filter.
        try:
            from db.client import get_client as _get_db_client
            _db = _get_db_client()
            _pinned_result = (
                _db.table("content_items")
                .select("id,source_type,source_name,source_url,title,raw_text,published_at,scraped_at,topics,is_deal_signal,metadata,assigned_edition_date")
                .eq("metadata->>newsletter_include", "true")
                .execute()
            )
            _pinned_items = [
                row for row in (_pinned_result.data or [])
                if not edition_date_str
                or not row.get("assigned_edition_date")
                or row.get("assigned_edition_date") == edition_date_str
            ]
            logger.info(f"Pre-fetched {len(_pinned_items)} dom-pinned items for edition {edition_date_str}")
        except Exception as _pe:
            logger.warning(f"Dom-pinned pre-fetch failed: {_pe}")
            _pinned_items = []

        # Fetch the current issue week so Friday drafts include TBPN, Elena,
        # All-In, and X/web material from the week.
        # Primary: last 5 days — ensures Monday-tagged items survive to Friday.
        # The _forced_background logic and week-window filter handle stale content.
        db_content = get_recent_content_items(days=5, limit=300, fresh_only=False)
        logger.info(f"Fetched {len(db_content)} content items (5-day window)")

        # Merge dom-pinned items — deduplicate by id so we don't double-count
        _existing_ids = {c.get("id") for c in db_content if c.get("id")}
        for _p in _pinned_items:
            if _p.get("id") not in _existing_ids:
                db_content.append(_p)
                _existing_ids.add(_p.get("id"))
        logger.info(f"After merging pinned items: {len(db_content)} total content items")

        # Fallback: if fewer than 10 items, expand to 7 days for background context
        if len(db_content) < 10:
            db_content_extended = get_recent_content_items(days=7, limit=200, fresh_only=False)
            # Mark extended items as background-only by flagging them so they sort to background
            two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            for item in db_content_extended:
                pub = item.get("published_at", "")
                if pub and pub < two_days_ago and item not in db_content:
                    # Flag as old so _build_research_user_message puts it in background
                    item["_forced_background"] = True
                    db_content.append(item)
            logger.info(f"Extended to 7-day fallback: {len(db_content)} total items")

        # Freshness filter — keep only content from the Mon-Fri issue window.
        # Anything published after Friday is next week's content.
        fresh_content = []
        stale_count = 0
        for item in db_content:
            pub = item.get("published_at")
            if pub:
                try:
                    pub_date = datetime.fromisoformat(pub.replace("Z", "+00:00")).date()
                    if pub_date < week_start or pub_date > content_window_end:
                        stale_count += 1
                        continue
                except Exception:
                    pass
            fresh_content.append(item)
        if stale_count:
            logger.info(
                f"Window filter: kept {len(fresh_content)} items from "
                f"{week_start} to {content_window_end}, dropped {stale_count} outside window"
            )
        db_content = fresh_content

        # Topic dedup — get subjects/facts from recent issues so Hermes avoids repeating
        from db.queries import get_recent_newsletter_topics
        prior_covered = get_recent_newsletter_topics(days=60)
        logger.info(f"Topic dedup: {len(prior_covered)} prior covered items loaded")

        # ── Step 2a: Fetch topic directives from edition_topics (GUARANTEED path) ──
        # Also fall back to the legacy memory.feedback path so nothing is lost.
        from tracking.topic_store import get_all_topics_for_edition as _get_edition_topics, mark_topics_used as _mark_topics_used
        _active_edition_num = issue_number

        edition_topic_rows = _get_edition_topics(_active_edition_num)
        edition_topic_directives = [t['topic'] for t in edition_topic_rows]
        logger.info(f"Edition topics from edition_topics table ({_active_edition_num}): {edition_topic_directives}")

        from memory.feedback import get_active_topic_directives, clear_topic_directives
        legacy_directives = await get_active_topic_directives(edition_date.isoformat())
        logger.info(f"Legacy topic directives from feedback table: {legacy_directives}")

        # Merge: edition_topics takes priority (guaranteed path)
        seen = set(edition_topic_directives)
        for d in legacy_directives:
            if d not in seen:
                edition_topic_directives.append(d)
                seen.add(d)
        topic_directives = edition_topic_directives
        if topic_directives:
            logger.info(f"Combined topic directives for Hermes: {topic_directives}")

        # ── Step 2b: Identify research topics ────────────────────────────────
        logger.info("Identifying research topics")
        import re as _re

        if topic_directives:
            # Dom specified topics — strip prefix and use exactly those for research.
            # Still run identify_weekly_topics for background research depth, but
            # directive topics go first and are the ONLY topics Hermes will write about.
            directive_topics = []
            for d in topic_directives:
                stripped = _re.sub(r"^\[Edition \d{4}-\d{2}-\d{2}\]\s*", "", d).strip()
                if stripped:
                    directive_topics.append(stripped)

            background_topics = await identify_weekly_topics(db_content)
            # Research directive topics + background for context; Hermes writes only directives
            topics = directive_topics + [t for t in background_topics if t not in directive_topics]
            logger.info(f"Directive topics (Hermes writes only these): {directive_topics}")
            logger.info(f"Background research topics (context only): {background_topics[:3]}")
        else:
            # No directives — SEO-ranked topics from Dom's preference universe.
            # identify_weekly_topics returns them sorted by live search volume (highest first).
            all_topics = await identify_weekly_topics(db_content)
            logger.info(f"SEO-ranked topics (no directives): {all_topics}")
            # Research all candidates; Hermes picks the top 1-3 to write about
            topics = all_topics

        # ── Steps 3 + context gathering: run in parallel ──────────────────────
        logger.info("Running parallel research and context gathering")
        from newsletter.performance import get_performance_context

        research_task = research_all_topics(topics)
        style_task = get_style_bible_for_prompt()
        feedback_task = get_all_active_feedback()
        context_task = get_all_context_summary(days=30)
        perf_task = get_performance_context()

        research_results, style_bible_text, feedback_items, context_summary, performance_context = await asyncio.gather(
            research_task, style_task, feedback_task, context_task, perf_task
        )
        logger.info(f"Research complete: {len(research_results)} topics. Feedback items: {len(feedback_items)}")

        # Retrieve concrete style examples from voice corpus keyed to this week's topics
        from training.style_analyser import get_concrete_style_examples
        style_examples = await asyncio.to_thread(get_concrete_style_examples, topics, 4)
        logger.info(f"Style examples: {len(style_examples)} concrete excerpts retrieved")

        # Retrieve satire examples for the 'Heard on the Street' section
        from training.style_analyser import get_satire_examples
        satire_examples = await asyncio.to_thread(get_satire_examples, 6)
        logger.info(f"Satire examples: {len(satire_examples)} tweets retrieved")

        # ── Step 3b: Pull voice clone data (non-blocking) ────────────────────
        logger.info("Pulling voice clone data")
        voice_clone_data = await asyncio.to_thread(pull_voice_clone_data)
        voice_clone_prefix = build_voice_clone_prompt_prefix(voice_clone_data)
        if voice_clone_prefix:
            logger.info("[voice_clone] Voice clone prefix loaded (%d chars)", len(voice_clone_prefix))
        else:
            logger.info("[voice_clone] No voice clone data available — using style bible only")

        # ── Step 4: Build Hermes prompt ───────────────────────────────────────
        logger.info("Building Hermes system prompt")

        hermes_prompt = _build_hermes_prompt(
            style_bible_text, feedback_items, context_summary, performance_context,
            style_examples, satire_examples, hermes_task=_build_hermes_task(topic_directives),
            voice_clone_prefix=voice_clone_prefix,
        )
        research_message = _build_research_user_message(
            research_results,
            db_content,
            prior_covered,
            edition_date=edition_date,
            content_window_start=week_start,
            content_window_end=content_window_end,
        )

        # Inject Dom's topic directives into the research message
        if topic_directives:
            numbered_directives = "\n".join(f"{i+1}. {d}" for i, d in enumerate(topic_directives))
            directives_block = (
                "\n\nMANDATORY — DO NOT SKIP ANY OF THESE:\n"
                "Dom has explicitly requested the following topics for this edition. "
                "EVERY ONE must appear as a dedicated section in The Note. "
                "Covering only some of them is a failure. "
                "Research is provided for each topic above — use it.\n\n"
                f"Topics you MUST cover (in this order):\n{numbered_directives}"
            )
            research_message += directives_block

        # Inject Dom's brief (angle/topic from the /newsletter conversation) if provided
        if brief and brief.strip():
            research_message += (
                f"\n\nDom's specific request for this issue: {brief.strip()}\n"
                "Prioritise this angle when selecting what to lead with."
            )
            if is_sample:
                research_message += (
                    "\n\nNOTE: This is a SAMPLE draft from existing data, not live research. "
                    "The purpose is to show tone and format — treat the content as illustrative."
                )

        # ── Step 5: Call claude-sonnet-4-5 to write the newsletter ────────────
        logger.info(f"Calling Hermes ({MODELS['writer']}) to write newsletter")
        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.getenv("OPENROUTER_API_KEY"))

        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["writer"],
            messages=[
                {"role": "system", "content": hermes_prompt},
                {"role": "user", "content": research_message},
            ],
            temperature=0.7,
            max_tokens=4500,
        )

        raw_output = response.choices[0].message.content or ""
        logger.info(f"Hermes response received: {len(raw_output)} chars")

        try:
            newsletter_data = _parse_model_json(raw_output)
            if not isinstance(newsletter_data, dict) or not newsletter_data.get("sections"):
                raise ValueError("Newsletter JSON did not contain sections")
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("Hermes returned invalid JSON after repair; retrying once")
            retry_response = await asyncio.to_thread(
                client.chat.completions.create,
                model=MODELS["writer"],
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return the complete newsletter as one valid JSON object only. "
                            "Do not use markdown fences or put properties outside the object."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Repair and complete this malformed newsletter response. "
                            "Preserve its facts and sections:\n\n" + raw_output
                        ),
                    },
                ],
                temperature=0.2,
                max_tokens=4500,
            )
            raw_output = retry_response.choices[0].message.content or ""
            newsletter_data = _parse_model_json(raw_output)
            if not isinstance(newsletter_data, dict) or not newsletter_data.get("sections"):
                raise ValueError("Newsletter retry did not contain sections")

        subject_line = newsletter_data.get("subject_line", f"Issue #{issue_number}")
        preview_text = newsletter_data.get("preview_text", "This week's VC secondaries intelligence brief.")
        sections = newsletter_data.get("sections", [])
        key_data = newsletter_data.get("key_data_for_visual", "VC secondaries market trends")
        sources = newsletter_data.get("sources", [])

        # ── Step 5b: Editorial review loop (Opus 4 reviewer) ─────────────────
        from agents.reviewer_agent import (
            review_newsletter, MAX_REVIEW_ITERATIONS, _build_revision_prompt
        )

        final_review = None
        # Track the best draft across all iterations so we use the highest-quality output
        best_score = -1
        best_draft = {
            "subject_line": subject_line,
            "preview_text": preview_text,
            "sections": sections,
            "sources": sources,
            "key_data": key_data,
            "raw_output": raw_output,
        }

        for review_iteration in range(1, MAX_REVIEW_ITERATIONS + 1):
            logger.info(f"Editorial review: iteration {review_iteration}/{MAX_REVIEW_ITERATIONS}")
            final_review = await review_newsletter(
                subject_line=subject_line,
                preview_text=preview_text,
                sections=sections,
                iteration=review_iteration,
                mandatory_topics=topic_directives if topic_directives else None,
            )
            logger.info(
                f"Review iteration {review_iteration}: "
                f"{'PASS' if final_review.passed else 'FAIL'} score={final_review.score}"
            )

            # Track best-scoring draft so far
            if final_review.score > best_score:
                best_score = final_review.score
                best_draft = {
                    "subject_line": subject_line,
                    "preview_text": preview_text,
                    "sections": sections,
                    "sources": sources,
                    "key_data": key_data,
                    "raw_output": raw_output,
                }

            if final_review.passed:
                break

            if review_iteration < MAX_REVIEW_ITERATIONS:
                # Send specific issues back to Hermes for revision
                logger.info(
                    f"Sending {len(final_review.issues)} reviewer notes back to Hermes "
                    f"(iteration {review_iteration})"
                )
                revision_message = _build_revision_prompt(
                    final_review.issues, research_message, iteration=review_iteration
                )
                revision_response = await asyncio.to_thread(
                    client.chat.completions.create,
                    model=MODELS["writer"],
                    messages=[
                        {"role": "system", "content": hermes_prompt},
                        {"role": "user", "content": research_message},
                        {"role": "assistant", "content": raw_output},
                        {"role": "user", "content": revision_message},
                    ],
                    temperature=0.5,  # Lower temp on revisions for more precise fixes
                    max_tokens=4500,
                )
                raw_output = revision_response.choices[0].message.content or ""
                clean_revised = raw_output.strip()
                if clean_revised.startswith("```"):
                    rev_lines = clean_revised.split("\n")
                    clean_revised = "\n".join(rev_lines[1:-1]) if len(rev_lines) > 2 else clean_revised
                try:
                    revised_data = _parse_model_json(clean_revised)
                    subject_line = revised_data.get("subject_line", subject_line)
                    preview_text = revised_data.get("preview_text", preview_text)
                    sections = revised_data.get("sections", sections)
                    sources = revised_data.get("sources", sources)
                    key_data = revised_data.get("key_data_for_visual", key_data)
                    logger.info(f"Hermes revision {review_iteration} parsed successfully")
                except json.JSONDecodeError as parse_err:
                    logger.error(f"Hermes revision {review_iteration} returned invalid JSON: {parse_err} — keeping previous")
                    break

        # If the loop never passed, roll back to the best-scoring draft
        if final_review and not final_review.passed:
            current_score = final_review.score
            if best_score > current_score:
                logger.info(
                    f"Final draft scored {current_score} but best was {best_score} "
                    f"— rolling back to best draft"
                )
                subject_line = best_draft["subject_line"]
                preview_text = best_draft["preview_text"]
                sections = best_draft["sections"]
                sources = best_draft["sources"]
                key_data = best_draft["key_data"]

        missing_topics = _missing_mandatory_topics(sections, topic_directives)
        if missing_topics:
            logger.warning("Mandatory topics missing after generation: %s", missing_topics)
            coverage_response = await asyncio.to_thread(
                client.chat.completions.create,
                model=MODELS["writer"],
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Edit the newsletter so every mandatory topic is covered. "
                            "Preserve factual accuracy and voice. Return valid JSON only "
                            "with a sections key containing the complete sections array."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Missing mandatory topics:\n"
                            + "\n".join(f"- {topic}" for topic in missing_topics)
                            + "\n\nCurrent sections:\n"
                            + json.dumps(sections)
                        ),
                    },
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            coverage_raw = (coverage_response.choices[0].message.content or "").strip()
            if coverage_raw.startswith("```"):
                coverage_raw = "\n".join(coverage_raw.splitlines()[1:-1])
            coverage_data = _parse_model_json(coverage_raw)
            if isinstance(coverage_data, list):
                sections = coverage_data
            elif isinstance(coverage_data.get("sections"), list):
                sections = coverage_data["sections"]
            still_missing = _missing_mandatory_topics(sections, topic_directives)
            if still_missing:
                raise RuntimeError(
                    "Mandatory topic verification failed after regeneration: "
                    + ", ".join(still_missing)
                )

        # Build honest review summary for Dom
        if final_review:
            if final_review.passed:
                review_summary = (
                    f"Reviewer: PASS score={final_review.score}/100 "
                    f"after {final_review.iteration} round(s)."
                )
            else:
                effective_score = max(best_score, final_review.score)
                remaining = final_review.issues[:5]
                issues_text = "\n".join(f"  {i+1}. {iss}" for i, iss in enumerate(remaining))
                review_summary = (
                    f"QUALITY FLAG: Best reviewer score {effective_score}/100 after {MAX_REVIEW_ITERATIONS} revisions. "
                    f"Remaining issues:\n{issues_text}\n"
                    f"You can still approve, or send /decline to restart with fresh content."
                )
        else:
            review_summary = "Reviewer: skipped"
        logger.info("review_summary: %s", review_summary[:200])

        # ── Step 5c: Humanizer post-processing — strip all remaining AI tells ──
        logger.info("Running humanizer pass on final sections")
        from processing.humanizer import humanize_sections
        sections = await humanize_sections(sections)
        logger.info("Humanizer pass complete")

        # Deterministic safety net — strip any em-dashes the LLM humanizer missed
        import re as _re_emdash
        for _sec in sections:
            if _sec.get("content"):
                _sec["content"] = _sec["content"].replace("—", ".").replace("–", ",")
                _sec["content"] = _re_emdash.sub(r"\s+\.\s+", ". ", _sec["content"])

        # ── Step 5d: Voice score check — regenerate weak sections ────────────
        logger.info("Running voice score checks on sections")
        scored_sections = []
        for sec in sections:
            sec_id = sec.get("id", "unknown")
            content = sec.get("content", "")
            if not content or len(content.strip()) < 80:
                scored_sections.append(sec)
                continue

            score = await check_voice_score(content)
            avg = score.get("avg", 7.0)
            logger.info("[voice_score] section=%s avg=%.1f (el=%s ins=%s dist=%s)",
                        sec_id, avg, score.get("elena_likeness"), score.get("insider_feel"), score.get("distinctiveness"))

            if avg >= 7.5:
                log_voice_drift(sec_id, score, "passed", issue_number)
                scored_sections.append(sec)
            elif avg >= 6.5:
                log_voice_drift(sec_id, score, "kept_with_flags", issue_number)
                scored_sections.append(sec)
            else:
                # avg < 6.5 — ask Hermes to rewrite with Elena technique enforcement
                log_voice_drift(sec_id, score, "regenerating", issue_number)
                flagged = score.get("flagged_sentences") or []
                flagged_note = ""
                if flagged:
                    flagged_note = (
                        "\n\nFlagged sentences to replace:\n"
                        + "\n".join(f"- {s}" for s in flagged[:5])
                    )
                regen_msg = (
                    f"The '{sec_id}' section scored {avg:.1f}/10 for Elena-likeness — too low. "
                    f"Rewrite ONLY the '{sec_id}' section. You MUST apply all three Elena techniques:\n"
                    f"1. RELATABLE HOOK: If this is the opening section, the first sentence must be a universal human frame, not a data point.\n"
                    f"2. FLAT REACTION LINES: Use one or two 3-7 word bone-dry reactions in the story, each as its own sentence and no more than one per paragraph. "
                    f"Examples: 'Dream big, I guess.' / 'This did not inspire confidence.' / 'Completely normal behaviour.' / 'And yet.'\n"
                    f"3. STORY WALK: Narrate events in sequence, reacting as you go. Do not front-load the conclusion.\n"
                    f"Return the full JSON object as before.{flagged_note}"
                )
                try:
                    regen_resp = await asyncio.to_thread(
                        client.chat.completions.create,
                        model=MODELS["writer"],
                        messages=[
                            {"role": "system", "content": hermes_prompt},
                            {"role": "user", "content": research_message},
                            {"role": "assistant", "content": raw_output},
                            {"role": "user", "content": regen_msg},
                        ],
                        temperature=0.6,
                        max_tokens=2000,
                    )
                    regen_raw = (regen_resp.choices[0].message.content or "").strip()
                    clean_regen = regen_raw
                    if clean_regen.startswith("```"):
                        regen_lines = clean_regen.split("\n")
                        clean_regen = "\n".join(regen_lines[1:-1]) if len(regen_lines) > 2 else clean_regen
                    regen_data = _parse_model_json(clean_regen)
                    new_sections = regen_data.get("sections") or []
                    regen_sec = next((s for s in new_sections if s.get("id") == sec_id), None)
                    if regen_sec:
                        regen_score = await check_voice_score(regen_sec.get("content", ""))
                        log_voice_drift(sec_id, regen_score, "regenerated", issue_number)
                        scored_sections.append(regen_sec)
                    else:
                        scored_sections.append(sec)
                except Exception as regen_err:
                    logger.warning("[voice_score] Regen failed for %s: %s", sec_id, regen_err)
                    scored_sections.append(sec)

        sections = scored_sections
        logger.info("Voice score checks complete")

        # ── Step 6: Generate visuals in parallel ──────────────────────────────
        logger.info("Generating newsletter visuals")
        newsletter_context = {
            "subject": subject_line,
            "key_data": key_data,
            "date_str": edition_date.strftime("%B %d, %Y"),
        }
        visuals = await generate_newsletter_visuals(newsletter_context)
        visual_count = sum(1 for v in visuals if v.get("url"))
        logger.info(f"Visuals generated: {visual_count}/3 succeeded")

        if visual_count < 3:
            logger.warning(f"Only {visual_count}/3 visuals generated — continuing with placeholders")

        # ── Step 7: Build HTML newsletter ─────────────────────────────────────
        logger.info("Building HTML newsletter")
        try:
            html_content = await build_newsletter_html(
                sections=sections,
                visuals=visuals,
                issue_number=issue_number,
                subject_line=subject_line,
                week_start=week_start,
            )
        except Exception as html_exc:
            logger.warning("HTML build attempt 1 failed: %s — retrying with empty visuals", html_exc)
            try:
                html_content = await build_newsletter_html(
                    sections=sections,
                    visuals=[],
                    issue_number=issue_number,
                    subject_line=subject_line,
                    week_start=week_start,
                )
            except Exception as html_exc2:
                logger.error("HTML build attempt 2 failed: %s", html_exc2)
                html_content = f"<html><body><p>Draft ready — HTML render failed. Sections: {len(sections)}. Edit and rebuild via voice note.</p></body></html>"
        plain_text = build_plain_text(sections)

        # ── Step 7b: Restore Dom's preserved lead content (soft restart) ────────
        # If Dom triggered a soft restart, his locked lead is held in pipeline_state.
        # Slot it back in before we build the final HTML and store the draft.
        try:
            from db.queries import get_pipeline_state, set_pipeline_state
            import json as _json
            _preserved_raw = get_pipeline_state("preserved_lead_content")
            if _preserved_raw:
                _preserved = _json.loads(_preserved_raw)
                for _sec in sections:
                    if _sec.get("id") == "lead":
                        _sec["content"] = _preserved.get("content", _sec["content"])
                        _sec["locked"] = True
                        break
                # Rebuild HTML and plain text with Dom's lead restored
                html_content = await build_newsletter_html(
                    sections=sections,
                    visuals=visuals,
                    issue_number=issue_number,
                    subject_line=subject_line,
                    week_start=week_start,
                )
                plain_text = build_plain_text(sections)
                set_pipeline_state("preserved_lead_content", "")
                logger.info("Preserved lead content restored for issue #%s", issue_number)
        except Exception as _pe:
            logger.warning("Could not restore preserved lead: %s", _pe)

        # ── Step 8: Store in Supabase as draft ────────────────────────────────
        logger.info("Storing newsletter draft in Supabase")
        update_newsletter_issue(issue_id, {
            "subject_line": subject_line,
            "preview_text": preview_text,
            "html_content": html_content,
            "plain_text": plain_text,
            "sections": sections,
            "visuals": visuals,
            "sources_used": sources,
            "style_bible_version": _get_style_version_sync(),
            "feedback_items_applied": len(feedback_items),
            "status": "draft",
        })
        try:
            from tracking.edition_tracker import mark_included_in_draft, track_content as _track_draft
            _active_edition_number = issue_number
            mark_included_in_draft(_active_edition_number)
            _track_draft(
                content_type='draft_edit',
                title=f"Draft generated — Edition {_active_edition_number}",
                body=f"Draft generated with {len(sections)} sections.",
                added_by='system',
                edition_number=_active_edition_number,
            )
        except Exception:
            pass
        update_newsletter_issue_optional(issue_id, {
            "edition_date": edition_date.isoformat(),
            "research_topics": topics,
            "review_summary": {"text": review_summary},
        })

        # ── Step 9: Send the stored draft to Dom via Telegram ─────────────────
        logger.info("Sending newsletter draft to Dom via Telegram")
        await _deliver_to_dom(
            issue_number=issue_number,
            subject_line=subject_line,
            preview_text=preview_text,
            plain_text=plain_text,
            html_content=html_content,
            visual_count=visual_count,
            sources=sources,
            research_topics=topics,
            review_summary=review_summary,
            is_sample=is_sample,
        )

        # Mark edition_topics as used and clear legacy directives
        if topic_directives:
            try:
                _mark_topics_used(_active_edition_num)
                logger.info(f"Marked edition_topics as used for edition {_active_edition_num}")
            except Exception as _mtu_e:
                logger.warning(f"Could not mark topics used: {_mtu_e}")
            await clear_topic_directives(edition_date.isoformat())
            logger.info("Topic directives cleared after successful generation")

        logger.info("Newsletter generation complete. Issue #%s, issue_id=%s", issue_number, issue_id)
        return issue_id

    except json.JSONDecodeError as e:
        error_msg = f"Newsletter writer returned invalid JSON: {e}. Raw output:\n{raw_output[:500]}"
        logger.error(error_msg)
        update_newsletter_issue(issue_id, {"status": "draft", "dom_feedback": f"Generation error: {e}"})
        await _send_telegram(f"Newsletter generation hit a formatting issue. The AI writer returned malformed output. Try /newsletter again.")
        return ""

    except Exception as e:
        logger.error(f"run_newsletter_generation error: {e}", exc_info=True)
        try:
            update_newsletter_issue(issue_id, {"status": "draft", "dom_feedback": f"Generation error: {str(e)}"})
        except Exception:
            pass
        await _send_telegram(f"Newsletter generation failed: {str(e)[:200]}")
        return ""


async def _deliver_to_dom(
    issue_number: int,
    subject_line: str,
    preview_text: str,
    plain_text: str,
    html_content: str,
    visual_count: int,
    sources: list = None,
    research_topics: list = None,
    review_summary: str = "",
    is_sample: bool = False,
) -> None:
    """Send the draft newsletter to Dom via Telegram with screenshot and inline buttons."""
    try:
        from telegram_bot.bot import HeraldExtBot
        from telegram_bot.newsletter_flow import send_newsletter_draft_preview

        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
        if not token or not chat_id:
            logger.warning("Telegram credentials not set — skipping delivery")
            return

        bot = HeraldExtBot(token=token)

        # Prepend sample label and tag Dom for feedback
        if is_sample:
            await bot.send_message(
                chat_id=chat_id,
                text="@DP9992 — sample draft below. This is from existing data, not live research. Take a look at the tone and format and let me know what you think.",
            )

        await send_newsletter_draft_preview(
            bot=bot,
            chat_id=chat_id,
            issue_number=issue_number,
            subject_line=subject_line,
            preview_text=preview_text,
            plain_text=plain_text,
            html_content=html_content,
            visual_count=visual_count,
            sources=sources or [],
            research_topics=research_topics or [],
            review_summary=review_summary,
        )

        # Prompt Dom about deals after every non-sample delivery
        if not is_sample:
            try:
                from db.queries import get_newsletter_edition_deals
                edition_deals = get_newsletter_edition_deals()
                supply = edition_deals.get("supply") or []
                demand = edition_deals.get("demand") or []
                if supply or demand:
                    deals_msg = (
                        f"Deal section is loaded with {len(supply)} supply and "
                        f"{len(demand)} demand entries from last time. "
                        "Reply with new deals to replace them, or just approve to keep them."
                    )
                else:
                    deals_msg = (
                        "No deals in the deal section yet. "
                        "Reply with Supply: / Demand: to add them before approving."
                    )
                await bot.send_message(chat_id=chat_id, text=deals_msg)
            except Exception as deals_exc:
                logger.warning("_deliver_to_dom: deals prompt failed: %s", deals_exc)

        if is_sample:
            await bot.send_message(
                chat_id=chat_id,
                text="Does this feel right, @DP9992? Reply with any feedback and I'll regenerate. When you're happy with the tone and format, just say 'real thing' and I'll run the live issue with fresh intel.",
            )
    except Exception as exc:
        logger.error("_deliver_to_dom error: %s", exc, exc_info=True)
        # Fallback to plain text if rich delivery fails
        label = "SAMPLE draft" if is_sample else "Draft"
        await _send_telegram(
            f"{label} ready — Issue #{issue_number}.\nSubject: {subject_line}\n"
            "Open HERALD Newsletter Studio to review, edit, download, or approve it."
        )


async def run_newsletter_from_conversation_draft(
    draft_text: str,
    issue_number: int,
    subject_line: str = "",
    preview_text: str = "",
) -> str:
    """
    Build and deliver a newsletter issue from a pre-approved conversation draft.

    This function is used when Dom and the bot collaboratively draft an issue
    via Telegram conversation and Dom approves the final text. It bypasses ALL
    automated generation (no web research, no Hermes LLM call, no voice score
    check) and directly builds HTML from the provided text.

    Args:
        draft_text:   The full approved plain-text content. Use "---" lines to
                      separate distinct story sections — each becomes its own
                      rendered story block inside the single "lead" section.
        issue_number: The target issue number.
        subject_line: Email subject line (optional — defaults to a generic title).
        preview_text: Email preview text (optional).

    Returns:
        Stored newsletter issue ID on success, empty string on failure.
    """
    from db.queries import (
        insert_newsletter_issue,
        update_newsletter_issue,
        update_newsletter_issue_optional,
        set_pipeline_state,
    )
    from newsletter.builder import build_newsletter_html, build_plain_text

    logger.info(
        "run_newsletter_from_conversation_draft: issue=%d subject=%r",
        issue_number,
        subject_line,
    )

    # Normalise subject / preview defaults
    subject_line = (subject_line or f"Issue #{issue_number}").strip()
    preview_text = (preview_text or "This week's VC secondaries intelligence brief.").strip()

    # Record the issue number in pipeline state so get_next_issue_number() never
    # reuses it even if Dom re-runs the automated pipeline afterward.
    set_pipeline_state("current_issue_number", str(issue_number))

    # Create a tracking row immediately so cancel_pipeline can find it.
    issue_id = insert_newsletter_issue({
        "issue_number": issue_number,
        "status": "generating",
        "subject_line": subject_line,
        "preview_text": preview_text,
    })

    try:
        # Parse the draft into story blocks split on "---" separator lines.
        # Each block becomes a paragraph group inside the single "lead" section.
        raw_blocks = [b.strip() for b in draft_text.split("\n---\n") if b.strip()]
        if not raw_blocks:
            raw_blocks = [draft_text.strip()]

        # Convert plain-text paragraphs to simple HTML paragraphs.
        def _to_html(block: str) -> str:
            paragraphs = [p.strip() for p in block.split("\n\n") if p.strip()]
            return "\n".join(f"<p>{p.replace(chr(10), ' ')}</p>" for p in paragraphs)

        # Join all story blocks, separated by a thin horizontal rule for readability.
        combined_html = "\n<hr style='border:none;border-top:1px solid #e0d9ce;margin:24px 0;'/>\n".join(
            _to_html(b) for b in raw_blocks
        )

        sections = [
            {
                "id": "lead",
                "title": "The Note",
                "content": combined_html,
            }
        ]

        # Build HTML and plain text
        html_content = await build_newsletter_html(
            sections=sections,
            visuals=[],
            issue_number=issue_number,
            subject_line=subject_line,
            week_start=None,
        )
        plain_text = build_plain_text(sections)

        # Store in Supabase as draft
        update_newsletter_issue(issue_id, {
            "subject_line": subject_line,
            "preview_text": preview_text,
            "html_content": html_content,
            "plain_text": plain_text,
            "sections": sections,
            "visuals": [],
            "sources_used": [],
            "status": "draft",
        })

        # Deliver to Dom via Telegram with approval buttons
        await _deliver_to_dom(
            issue_number=issue_number,
            subject_line=subject_line,
            preview_text=preview_text,
            plain_text=plain_text,
            html_content=html_content,
            visual_count=0,
            sources=[],
            research_topics=[],
            review_summary="Conversation draft — approved by Dom before pipeline submission.",
            is_sample=False,
        )

        logger.info(
            "run_newsletter_from_conversation_draft: complete. issue=%d issue_id=%s",
            issue_number,
            issue_id,
        )
        return issue_id

    except Exception as exc:
        logger.error(
            "run_newsletter_from_conversation_draft error: %s", exc, exc_info=True
        )
        try:
            update_newsletter_issue(issue_id, {
                "status": "draft",
                "dom_feedback": f"Conversation draft pipeline error: {str(exc)}",
            })
        except Exception:
            pass
        await _send_telegram(
            f"Conversation draft pipeline failed: {str(exc)[:200]}"
        )
        return ""


def _get_style_version_sync() -> int:
    """Safely get the current style bible version number (sync, no coroutine)."""
    try:
        from db.client import get_client
        client = get_client()
        result = (
            client.table("style_bible")
            .select("version")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows:
            return int(rows[0].get("version") or 1)
        return 0
    except Exception:
        return 0

"""
Newsletter performance analysis and self-improvement intelligence.

Fetches Beehiiv analytics and generates data-driven recommendations
that are injected into the Hermes newsletter generation prompt.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Industry benchmarks for VC/finance newsletters
_BENCHMARKS = {
    "open_rate_good": 0.35,      # 35%+ is strong for B2B finance
    "open_rate_average": 0.25,   # 25-35% is average
    "click_rate_good": 0.05,     # 5%+ click rate
    "click_rate_average": 0.02,  # 2-5% is average
    "unsub_rate_danger": 0.01,   # >1% unsubscribe is a warning sign
}

# Newsletter growth expert knowledge injected into every generation prompt
NEWSLETTER_EXPERT_KNOWLEDGE = """
NEWSLETTER PERFORMANCE INTELLIGENCE
You are also an expert in B2B newsletter growth, email copywriting, and audience development for finance/VC audiences. Apply this knowledge every time you write.

SUBJECT LINE FORMULAS THAT DRIVE OPENS (ranked by effectiveness for finance audiences):
1. Insider intel: "The deal Goldman didn't want you to know about" / "What the LP letter buried on page 14"
2. Contrarian take: "Everyone's wrong about secondaries right now" / "Why the carry rush will backfire"
3. Specific numbers: "3 funds repriced 40% below NAV this week" / "The $2.3bn denominator effect no one is tracking"
4. Scarcity/urgency: "Window closing on this pricing anomaly" / "Before the Q3 marks come out"
5. Social proof: "What every GP at SuperReturn was actually worried about" / "The question Blackstone's IR team keeps dodging"
6. Question with implied tension: "Is the secondaries bid-ask finally closing?" / "Who's really buying at these levels?"

SUBJECT LINE RULES:
- Maximum 50 characters for mobile preview (most opens are mobile)
- Avoid: "Weekly brief", "Newsletter", dates in subject, generic "what's happening"
- Always test: would a GP forward this to their IC? That's the bar.
- Preview text should contradict or extend the subject, not repeat it

OPENING HOOK PATTERNS (first 2 sentences determine whether they read on):
- Open on a scene, not a summary: "A GP called me Tuesday. His LP wanted out."
- Open on a number that surprises: "Sixteen funds. That's how many had redemption requests this week."
- Open on a contrarian assertion: "The secondary market is not as liquid as everyone claims."
- Never open with: "This week in..." / "In today's issue..." / "Welcome to..."

CONTENT STRUCTURE THAT DRIVES CLICK-THROUGH:
- Lead with the signal, not the context. Readers know the context.
- One insight per section, not a news summary
- Use short paragraphs (2-3 sentences max) — finance readers skim
- Bold the key claim at the start of each section
- End sections with an open question or forward-looking implication
- Tables > bullet lists for data-heavy content (higher engagement)

VISUAL STRATEGY:
- Header image sets tone — dark/authoritative for finance audiences
- Charts only if they show something counter-intuitive
- Deal tables outperform narrative for click-through (readers want to reference data)
- One strong visual beats three weak ones

GROWTH TACTICS TO EMBED IN EVERY ISSUE:
- "Forward to your IC" language in the deal sections
- Referral-worthy insight in the lead (the kind LPs forward to their advisors)
- Web version link for sharing on LinkedIn
- Clear CTA at the end linking to one specific resource or action
"""


async def get_performance_context() -> str:
    """
    Fetch recent newsletter performance and return a formatted
    context string for injection into the Hermes generation prompt.
    """
    try:
        from newsletter.beehiiv import get_recent_posts_performance, get_publication_overview
        overview = await get_publication_overview()

        if not overview.get("success"):
            return ""

        recent = overview.get("recent_posts", [])
        if not recent:
            return ""

        lines = ["\nNEWSLETTER PERFORMANCE DATA (use this to improve this week's issue):"]

        # Recent performance trend
        avg_open = overview.get("recent_avg_open_rate", 0.0)
        avg_click = overview.get("recent_avg_click_rate", 0.0)

        open_pct = avg_open * 100
        click_pct = avg_click * 100
        bench_open = _BENCHMARKS["open_rate_good"] * 100
        bench_click = _BENCHMARKS["click_rate_good"] * 100

        if avg_open > 0:
            open_vs_bench = "above benchmark" if avg_open >= _BENCHMARKS["open_rate_good"] else "below benchmark"
            click_vs_bench = "above benchmark" if avg_click >= _BENCHMARKS["click_rate_good"] else "below benchmark"
            lines.append(
                f"Recent average: {open_pct:.1f}% open rate ({open_vs_bench}, "
                f"benchmark {bench_open:.0f}%+), "
                f"{click_pct:.1f}% CTR ({click_vs_bench}, benchmark {bench_click:.0f}%+)"
            )

        # Best and worst performing subject lines
        if len(recent) >= 2:
            sorted_by_open = sorted(recent, key=lambda x: x["open_rate"], reverse=True)
            best = sorted_by_open[0]
            worst = sorted_by_open[-1]

            if best.get("subject_line"):
                lines.append(f"Best performing subject ({best['open_rate']*100:.1f}% open): \"{best['subject_line']}\"")
            if worst.get("subject_line") and worst["subject_line"] != best.get("subject_line"):
                lines.append(f"Worst performing subject ({worst['open_rate']*100:.1f}% open): \"{worst['subject_line']}\"")

        # Unsubscribe warnings
        total_unsubs = sum(p.get("unsubscribes", 0) for p in recent)
        if total_unsubs > 0 and recent:
            avg_recipients = sum(p.get("recipients", 1) for p in recent) / len(recent)
            unsub_rate = total_unsubs / (len(recent) * max(avg_recipients, 1))
            if unsub_rate > _BENCHMARKS["unsub_rate_danger"]:
                lines.append(
                    f"WARNING: Elevated unsubscribe rate ({unsub_rate*100:.2f}%). "
                    "Review content for relevance and frequency."
                )

        # Actionable directive
        if avg_open < _BENCHMARKS["open_rate_average"]:
            lines.append(
                "PRIORITY: Open rate is underperforming. "
                "Use a high-curiosity or contrarian subject line this week (see formulas above). "
                "Lead with the most surprising data point you have."
            )
        elif avg_open >= _BENCHMARKS["open_rate_good"]:
            lines.append(
                "Open rate is strong. Focus this week on improving click-through — "
                "add a clear data table or one specific deal breakdown that readers will screenshot."
            )

        if avg_click < _BENCHMARKS["click_rate_average"] and avg_open > 0:
            lines.append(
                "CTR is low. Add a direct CTA at the end of the lead section. "
                "Include at least one deal table — structured data drives clicks."
            )

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("get_performance_context: could not fetch analytics: %s", exc)
        return ""


def format_performance_for_telegram(overview: dict) -> str:
    """Format publication overview as a clean Telegram-ready summary."""
    if not overview.get("success"):
        return f"Could not fetch analytics: {overview.get('error', 'unknown error')}"

    recent = overview.get("recent_posts", [])
    agg = overview.get("aggregate", {})

    lines = ["Newsletter analytics:"]

    avg_open = overview.get("recent_avg_open_rate", 0.0)
    avg_click = overview.get("recent_avg_click_rate", 0.0)

    if avg_open > 0:
        lines.append(f"Recent open rate: {avg_open*100:.1f}%")
        lines.append(f"Recent click rate: {avg_click*100:.1f}%")

    if agg.get("total_recipients"):
        lines.append(f"Total emails sent: {agg['total_recipients']:,}")

    if recent:
        lines.append(f"\nLast {len(recent)} issues:")
        for i, p in enumerate(recent[:5], 1):
            subj = p.get("subject_line", p.get("title", "Untitled"))[:50]
            open_pct = p.get("open_rate", 0) * 100
            click_pct = p.get("click_rate", 0) * 100
            lines.append(f"{i}. {subj}")
            lines.append(f"   {open_pct:.1f}% open / {click_pct:.1f}% CTR")

        # Industry context
        lines.append("\nIndustry benchmark (B2B finance): 35%+ open, 5%+ CTR")
        if avg_open >= 0.35:
            lines.append("Open rate: above benchmark.")
        elif avg_open >= 0.25:
            lines.append("Open rate: average. Stronger subject lines can push this higher.")
        else:
            lines.append("Open rate: below average. Subject line and hook quality is the lever.")

    return "\n".join(lines)

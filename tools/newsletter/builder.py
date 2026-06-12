"""
Newsletter builder — assembles the full HTML newsletter from Hermes JSON output.

Sections are rendered in a fixed editorial order:
  tldr -> lead -> market_pulse -> angle

A standalone Deals block (supply/demand) is always appended at the bottom.
"""

import logging
import re
from datetime import date, datetime, timezone, timedelta

from newsletter.sections import (
    render_deal_table,
    render_supply_demand_block,
    render_footer,
    render_header,
    render_image_block,
    render_satire_section,
    render_section,
    render_tldr_section,
    render_contents,
)

logger = logging.getLogger(__name__)

# Fixed editorial section order.  Each entry maps an id that Hermes must emit
# to a display title and an alternating background colour.
_SECTION_ORDER = [
    ("tldr", "TL;DR", "#1a1a2e"),          # dark navy — stands out at top
    ("lead", "The Lead", "#ffffff"),
    ("market_pulse", "Market Pulse", "#f8f6f0"),
    ("angle", "The Angle", "#ffffff"),
]

# Regex used in the plain-text builder to strip HTML tags.
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities from a string."""
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&copy;", "(c)")
    )
    return text


def _find_visual(visuals: list[dict], placement: str) -> dict | None:
    """Return the first visual whose placement matches, or None."""
    for v in visuals:
        if v.get("placement") == placement:
            return v
    return None


def _build_week_str(week_start: date | None) -> str:
    """Format week_start as a human-readable range string.

    Example: April 21–27, 2026
    """
    if week_start is None:
        return datetime.now(timezone.utc).strftime("%B %Y")

    week_end = week_start + timedelta(days=6)
    try:
        if week_start.month == week_end.month:
            return f"{week_start.strftime('%B')} {week_start.day}–{week_end.day}, {week_start.year}"
        else:
            return (
                f"{week_start.strftime('%B')} {week_start.day} – "
                f"{week_end.strftime('%B')} {week_end.day}, {week_start.year}"
            )
    except Exception:
        return week_start.strftime("%B %Y")


def _wrap_document(body_html: str) -> str:
    """Wrap assembled body HTML in a full email-safe HTML document."""
    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="UTF-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge" />'
        "</head>"
        '<body style="margin:0;padding:0;background:#F5F0E8;'
        'font-family:Georgia,\'Times New Roman\',serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" style="background:#F5F0E8;padding:0;">'
        "<tr><td align=\"center\">"
        + body_html
        + "</td></tr></table>"
        "</body>"
        "</html>"
    )


async def build_newsletter_html(
    sections: list[dict],
    visuals: list[dict],
    issue_number: int,
    subject_line: str,
    week_start: date | None = None,
) -> str:
    """Assemble the complete HTML newsletter from sections and visuals.

    Args:
        sections: List of dicts. Each must contain:
                    - id (str): one of tldr, lead, market_pulse, angle
                    - title (str): display title (used as fallback if order title missing)
                    - content (str): body HTML or plain text
        visuals: List of dicts. Each must contain:
                    - placement (str): 'top', 'after_section_2', or 'after_section_4'
                    - url (str): absolute image URL
                    - alt (str, optional): alt text
                    - caption (str, optional): caption text
        issue_number: Sequential issue number.
        subject_line: The newsletter subject / headline for the masthead.
        week_start: Optional Monday date for the issue week.

    Returns:
        Complete standalone newsletter HTML string.
    """
    logger.info(
        "build_newsletter_html: issue=%d sections=%d visuals=%d",
        issue_number,
        len(sections),
        len(visuals),
    )

    week_str = _build_week_str(week_start)

    # Index incoming sections by id for quick lookup.
    sections_by_id: dict[str, dict] = {}
    for s in sections:
        sid = s.get("id", "")
        if not sid:
            logger.warning("Section missing 'id' field — skipping: %s", s)
            continue
        sections_by_id[sid] = s

    # Build a placement index: placement_key -> visual dict.
    # Only index visuals with a real URL — skip failed generation placeholders.
    # Supported placements:
    #   "top"              -> header banner image
    #   "after_<section>"  -> image injected immediately after that section
    #   "before_deals"     -> image injected before the Deals block
    #   "bottom"           -> image injected after Deals, before footer
    visuals_by_placement: dict[str, dict] = {}
    for v in visuals:
        key = v.get("placement", "")
        if key and v.get("url", "").strip():
            visuals_by_placement[key] = v

    header_image_url: str | None = (
        visuals_by_placement["top"].get("url") if "top" in visuals_by_placement else None
    )

    # Build the table of contents from the fixed order, including only
    # sections that have data.
    toc_sections: list[dict] = []
    for sid, default_title, _ in _SECTION_ORDER:
        if sid in sections_by_id:
            s = sections_by_id[sid]
            toc_sections.append(
                {"id": sid, "title": s.get("title") or default_title}
            )

    # --------------------------------------------------------------------------
    # Assemble body parts
    # --------------------------------------------------------------------------
    parts: list[str] = []

    # 1. Header
    parts.append(
        render_header(
            issue_number=issue_number,
            week_str=week_str,
            subject=subject_line,
            header_image_url=header_image_url,
        )
    )

    # 2. Table of contents. Short ping-style editions do not need a TOC.
    if len(toc_sections) > 3:
        parts.append(render_contents(toc_sections))

    # 3. Sections in fixed editorial order.  After each section, inject any
    #    visual whose placement is "after_<section_id>".
    for sid, default_title, bg_color in _SECTION_ORDER:
        section_data = sections_by_id.get(sid)
        if not section_data:
            logger.debug("Section '%s' not found in input — skipping.", sid)
            continue

        title = section_data.get("title") or default_title
        content = section_data.get("content", "")

        if not content:
            logger.warning("Section '%s' has empty content — rendering empty body.", sid)

        if sid == "tldr":
            parts.append(render_tldr_section(content))
        else:
            parts.append(render_section(sid, title, content, bg_color))

        # Inject visual placed after this section, if any.
        after_key = f"after_{sid}"
        if after_key in visuals_by_placement:
            v = visuals_by_placement[after_key]
            parts.append(
                render_image_block(
                    image_url=v.get("url", ""),
                    caption=v.get("caption", ""),
                    alt=v.get("alt", ""),
                )
            )

    # 4. Optional image before the Deals block.
    if "before_deals" in visuals_by_placement:
        v = visuals_by_placement["before_deals"]
        parts.append(
            render_image_block(
                image_url=v.get("url", ""),
                caption=v.get("caption", ""),
                alt=v.get("alt", ""),
            )
        )

    # 5. Standalone Deals section — ALWAYS rendered at bottom, separate from editorial content.
    from db.queries import get_newsletter_edition_deals
    edition_deals = get_newsletter_edition_deals()
    sd_html = render_supply_demand_block(
        supply=edition_deals.get("supply") or [],
        demand=edition_deals.get("demand") or [],
    )
    parts.append(render_section("deals_block", "Deals", sd_html, "#f8f6f0"))

    # 6. Optional image after the Deals block (bottom placement).
    if "bottom" in visuals_by_placement:
        v = visuals_by_placement["bottom"]
        parts.append(
            render_image_block(
                image_url=v.get("url", ""),
                caption=v.get("caption", ""),
                alt=v.get("alt", ""),
            )
        )

    # 7. Footer — use custom sign-off from a 'footer' section if present
    footer_section = sections_by_id.get("footer")
    footer_note = footer_section.get("content") if footer_section else None
    parts.append(render_footer(footer_note=footer_note))

    body_html = "".join(parts)
    full_html = _wrap_document(body_html)

    logger.info(
        "build_newsletter_html: complete. HTML length=%d chars", len(full_html)
    )
    return full_html


def build_plain_text(sections: list[dict]) -> str:
    """Build a plain-text version of the newsletter for Telegram preview.

    Produces clean readable text with section titles and content,
    with no HTML tags.

    Args:
        sections: Same format as build_newsletter_html.

    Returns:
        Plain-text string.
    """
    # Index by id.
    sections_by_id: dict[str, dict] = {
        s["id"]: s for s in sections if s.get("id")
    }

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("The Secondaries Intelligence Report")
    lines.append("=" * 60)
    lines.append("")

    for sid, default_title, _ in _SECTION_ORDER:
        section_data = sections_by_id.get(sid)
        if not section_data:
            continue

        title = section_data.get("title") or default_title
        content = section_data.get("content", "")
        deals = section_data.get("deals_table", [])

        lines.append(f"[ {title.upper()} ]")
        lines.append("-" * 40)

        if content:
            clean = _strip_html(content).strip()
            # Normalise whitespace runs.
            clean = re.sub(r" {2,}", " ", clean)
            clean = re.sub(r"\n{3,}", "\n\n", clean)
            lines.append(clean)

        if deals:
            lines.append("")
            lines.append("DEALS THIS WEEK:")
            for deal in deals:
                company = deal.get("company", "—")
                stage = deal.get("stage", "—")
                deal_type = deal.get("deal_type", "—")
                size = deal.get("reported_size", "—")
                signal = deal.get("signal", "—")
                lines.append(
                    f"  {company} | {stage} | {deal_type} | {size}"
                )
                if signal and signal != "—":
                    lines.append(f"    Signal: {signal}")

        lines.append("")

    # Always render the standalone deals block in plain text — separate from editorial content.
    from db.queries import get_newsletter_edition_deals
    edition_deals = get_newsletter_edition_deals()
    supply = edition_deals.get("supply") or []
    demand = edition_deals.get("demand") or []
    lines.append("[ DEALS ]")
    lines.append("-" * 40)
    lines.append("Supply:")
    if supply:
        for item in supply:
            lines.append(f"  - {item}")
    else:
        lines.append("  (none listed)")
    lines.append("")
    lines.append("Demand:")
    if demand:
        for item in demand:
            lines.append(f"  - {item}")
    else:
        lines.append("  (none listed)")
    lines.append("")

    lines.append("=" * 60)
    lines.append("The Secondaries Intelligence Report")
    lines.append("=" * 60)

    return "\n".join(lines)

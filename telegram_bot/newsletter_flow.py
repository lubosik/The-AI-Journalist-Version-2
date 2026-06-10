"""Rich newsletter draft delivery to Dom via Telegram."""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


async def send_newsletter_draft_preview(
    bot,
    chat_id: str | int,
    issue_number: int,
    subject_line: str,
    preview_text: str,
    plain_text: str,
    html_content: str,
    visual_count: int,
    beehiiv_post_id: str,
    beehiiv_url: str,
    sources: list | None = None,
    research_topics: list | None = None,
    review_summary: str = "",
) -> None:
    """Send a structured newsletter draft preview to Dom."""
    sources = sources or []
    research_topics = research_topics or []

    # --- Header message ---
    header_lines = [
        f"HERALD Issue #{issue_number} — Draft Ready",
        f"Subject: {subject_line}",
        f"Preview: {preview_text}" if preview_text else "",
    ]
    if sources:
        header_lines.append(f"Sources used: {', '.join(str(s) for s in sources[:5])}")
    if research_topics:
        header_lines.append(f"Research topics: {', '.join(str(t) for t in research_topics[:3])}")
    if review_summary:
        header_lines.append(f"\nEditor notes: {review_summary[:300]}")

    header = "\n".join(l for l in header_lines if l)
    await bot.send_message(chat_id=chat_id, text=header)

    # --- Content preview (first ~1500 chars of plain text) ---
    if plain_text:
        snippet = plain_text.strip()[:1500]
        if len(plain_text.strip()) > 1500:
            snippet += "\n\n[...continued — full draft on Beehiiv]"
        await bot.send_message(chat_id=chat_id, text=snippet)

    # --- Plain text file attachment ---
    try:
        txt_bytes = plain_text.encode("utf-8")
        buf = io.BytesIO(txt_bytes)
        buf.name = f"herald_issue_{issue_number}_draft.txt"
        await bot.send_document(
            chat_id=chat_id,
            document=buf,
            caption=f"Full plain-text draft — Issue #{issue_number}",
        )
    except Exception as e:
        logger.warning("Could not attach draft file: %s", e)

    # --- Footer with Beehiiv link ---
    if beehiiv_url:
        footer = f"Beehiiv draft: {beehiiv_url}\n\nReply with any feedback, or send /approve to publish."
    else:
        footer = "No Beehiiv draft yet (403 plan limit). Review the text above and reply with feedback."
    await bot.send_message(chat_id=chat_id, text=footer)

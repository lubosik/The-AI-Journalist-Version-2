"""
Section rendering utilities for the HERALD newsletter.

Each function takes structured content and returns an inline-styled HTML string
suitable for Beehiiv and general email client delivery.

Design system:
  Background:     #F5F0E8
  Primary text:   #1A1A1A
  Accent:         #D0C8B8
  Muted accent:   #B0A888
  Font:           Georgia, 'Times New Roman', serif
  Max width:      680px
  Dividers:       1px solid #c9a84c
  Deal table:     dark navy header (#1a1a2e), white rows, alternating #f8f6f0
"""

import html
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
_BG_WHITE = "#F5F0E8"
_COLOR_PRIMARY = "#1A1A1A"
_COLOR_ACCENT = "#D0C8B8"
_COLOR_NAVY = "#2A2A2A"
_COLOR_MUTED = "#666666"
_COLOR_ALT_ROW = "#EDE8DC"
_COLOR_TABLE_HEADER_BG = "#1A1A1A"
_COLOR_TABLE_HEADER_TEXT = "#F5F0E8"
_FONT_SERIF = "Georgia, 'Times New Roman', serif"
_FONT_SANS = "Arial, Helvetica, sans-serif"
_MAX_WIDTH = "600px"
_DIVIDER_STYLE = f"1px solid {_COLOR_ACCENT}"


def _container_open(max_width: str = _MAX_WIDTH, bg: str = _BG_WHITE) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{bg};">'
        f'<tr><td align="center" style="padding:0;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="{max_width}" style="max-width:{max_width};width:100%;background:{bg};">'
        f'<tr><td style="padding:0 24px;">'
    )


def _container_close() -> str:
    return "</td></tr></table></td></tr></table>"


# ---------------------------------------------------------------------------
# Public rendering functions
# ---------------------------------------------------------------------------

def render_header(
    issue_number: int,
    week_str: str,
    subject: str,
    header_image_url: str | None = None,
) -> str:
    """Render the newsletter masthead/header.

    Args:
        issue_number: Sequential issue number (e.g. 12).
        week_str: Human-readable week string (e.g. "April 21–27, 2026").
        subject: The issue subject line / headline.
        header_image_url: Optional URL to a header banner image.

    Returns:
        Inline-styled HTML string.
    """
    parts: list[str] = []

    # Outer wrapper
    parts.append(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{_BG_WHITE};">'
        '<tr><td align="center" style="padding:0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="{_MAX_WIDTH}" style="max-width:{_MAX_WIDTH};width:100%;background:{_BG_WHITE};">'
        '<tr><td style="padding:40px 24px 24px 24px;text-align:center;">'
    )

    # Optional header image
    if header_image_url:
        safe_url = html.escape(header_image_url, quote=True)
        parts.append(
            f'<img src="{safe_url}" alt="Newsletter header" '
            f'style="display:block;margin:0 auto 20px auto;max-width:100%;height:auto;" />'
        )

    # Masthead wordmark — no internal system names in subscriber-facing output
    parts.append(
        f'<h1 style="margin:0;border-bottom:3px solid {_COLOR_PRIMARY};padding-bottom:16px;'
        f'font-family:{_FONT_SERIF};font-size:36px;font-weight:900;color:{_COLOR_PRIMARY};letter-spacing:0;">'
        "ROFR'd"
        "</h1>"
        f'<p style="margin:10px 0 24px 0;font-family:{_FONT_SANS};font-size:11px;'
        f'letter-spacing:2px;text-transform:uppercase;color:{_COLOR_MUTED};">'
        f"Pre-IPO Secondaries &bull; {html.escape(week_str)} &bull; 3 min read"
        "</p>"
    )

    # Subject / headline
    safe_subject = html.escape(subject)
    parts.append(
        f'<p style="margin:0;font-family:{_FONT_SERIF};font-size:18px;'
        f'font-weight:700;color:{_COLOR_PRIMARY};line-height:1.45;">'
        f"{safe_subject}"
        "</p>"
    )

    parts.append("</td></tr></table></td></tr></table>")
    return "".join(parts)


def render_contents(sections: list[dict]) -> str:
    """Render a linked table of contents.

    Args:
        sections: List of dicts with keys 'id' (anchor target) and 'title'.

    Returns:
        Inline-styled HTML string.
    """
    if not sections:
        logger.warning("render_contents called with empty sections list")
        return ""

    parts: list[str] = []
    parts.append(_container_open())
    parts.append(
        f'<div style="padding:24px 0 16px 0;">'
        f'<p style="margin:0 0 12px 0;font-family:{_FONT_SANS};font-size:10px;'
        f'letter-spacing:3px;text-transform:uppercase;color:{_COLOR_ACCENT};">'
        "IN THIS ISSUE"
        "</p>"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
    )

    for idx, section in enumerate(sections, start=1):
        section_id = html.escape(str(section.get("id", "")), quote=True)
        title = html.escape(str(section.get("title", "Untitled")))
        bg = _BG_WHITE if idx % 2 != 0 else _COLOR_ALT_ROW
        parts.append(
            f'<tr style="background:{bg};">'
            f'<td style="padding:8px 12px;font-family:{_FONT_SERIF};font-size:14px;color:{_COLOR_MUTED};">'
            f"{idx:02d}."
            "</td>"
            f'<td style="padding:8px 4px 8px 0;font-family:{_FONT_SERIF};font-size:14px;">'
            f'<a href="#{section_id}" '
            f'style="color:{_COLOR_PRIMARY};text-decoration:none;border-bottom:1px solid {_COLOR_ACCENT};">'
            f"{title}"
            "</a>"
            "</td>"
            "</tr>"
        )

    parts.append("</table>")

    # Bottom divider
    parts.append(
        f'<div style="height:1px;background:{_COLOR_ACCENT};margin:16px 0 0 0;"></div>'
        "</div>"
    )
    parts.append(_container_close())
    return "".join(parts)


def render_section(
    section_id: str,
    title: str,
    content: str,
    bg_color: str = _BG_WHITE,
) -> str:
    """Render a standard text section with an HTML anchor.

    Args:
        section_id: Used as the anchor id for in-page linking.
        title: Section heading.
        content: Body HTML or plain text. If plain text, it will be
                 wrapped in a paragraph tag.
        bg_color: Background colour for the section cell.

    Returns:
        Inline-styled HTML string.
    """
    safe_id = html.escape(section_id, quote=True)
    safe_title = html.escape(title)

    # Pre-process story headlines — Hermes outputs "### Headline ###" for per-story headlines
    # Convert these to 19px/700 Georgia headline paragraphs matching the target HTML template
    import re as _re

    def _render_headlines(text: str) -> str:
        def _replace_headline(m):
            headline_text = html.escape(m.group(1).strip())
            return (
                f'<p style="font-family:{_FONT_SERIF};font-size:19px;font-weight:700;'
                f'line-height:1.35;color:{_COLOR_PRIMARY};padding-bottom:12px;'
                f'padding-top:16px;margin:0 0 12px 0;">'
                f'{headline_text}</p>'
            )
        return _re.sub(r'###\s*(.+?)\s*###', _replace_headline, text)

    if not content.strip().startswith("<"):
        content = _render_headlines(content)

    # Detect whether content looks like HTML (contains any tag-like pattern).
    # If not, wrap it in a paragraph with line-break handling.
    if content.strip().startswith("<"):
        body_html = content
    else:
        # Escape and convert newlines to <br>
        body_html = (
            f'<p style="margin:0 0 16px 0;font-family:{_FONT_SERIF};font-size:15px;'
            f'line-height:1.75;color:#2A2A2A;">'
            + html.escape(content).replace("\n\n", "</p>"
              f'<p style="margin:0 0 16px 0;font-family:{_FONT_SERIF};font-size:15px;'
              f'line-height:1.75;color:#2A2A2A;">')
            .replace("\n", "<br>")
            + "</p>"
        )

    parts: list[str] = []
    parts.append(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{bg_color};">'
        '<tr><td align="center" style="padding:0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="{_MAX_WIDTH}" style="max-width:{_MAX_WIDTH};width:100%;background:{bg_color};">'
        '<tr><td style="padding:32px 24px;">'
    )

    # Anchor target
    parts.append(f'<a name="{safe_id}" id="{safe_id}" style="display:block;"></a>')

    # Section label
    parts.append(
        f'<p style="margin:0 0 12px 0;font-family:{_FONT_SANS};font-size:10px;'
        f'font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#999999;'
        f'border-top:1px solid {_COLOR_ACCENT};padding-top:24px;">'
        f"{safe_title}"
        "</p>"
    )

    # Body
    parts.append(body_html)

    parts.append("</td></tr></table></td></tr></table>")
    return "".join(parts)


def render_deal_table(deals: list[dict]) -> str:
    """Render a structured deal table.

    Args:
        deals: List of dicts. Each must contain:
               - company (str)
               - stage (str)
               - deal_type (str)
               - reported_size (str)
               - signal (str)

    Returns:
        Inline-styled HTML string.
    """
    if not deals:
        logger.warning("render_deal_table called with empty deals list")
        return (
            f'<p style="font-family:{_FONT_SERIF};font-size:14px;color:{_COLOR_MUTED};'
            'font-style:italic;">No deal data available for this week.</p>'
        )

    columns = [
        ("Company", "company", "left", "22%"),
        ("Stage", "stage", "center", "14%"),
        ("Type", "deal_type", "center", "18%"),
        ("Size", "reported_size", "center", "16%"),
        ("Signal", "signal", "left", "30%"),
    ]

    parts: list[str] = []
    parts.append(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="border-collapse:collapse;font-family:{_FONT_SANS};font-size:13px;">'
    )

    # Header row
    parts.append("<thead><tr>")
    for label, _, align, width in columns:
        parts.append(
            f'<th style="background:{_COLOR_TABLE_HEADER_BG};color:{_COLOR_TABLE_HEADER_TEXT};'
            f'padding:10px 12px;text-align:{align};font-size:11px;letter-spacing:1px;'
            f'text-transform:uppercase;width:{width};white-space:nowrap;">'
            f"{html.escape(label)}"
            "</th>"
        )
    parts.append("</tr></thead><tbody>")

    for idx, deal in enumerate(deals):
        row_bg = _BG_WHITE if idx % 2 == 0 else _COLOR_ALT_ROW
        parts.append(f'<tr style="background:{row_bg};">')
        for _, key, align, _ in columns:
            raw_val = deal.get(key, "—")
            val = html.escape(str(raw_val)) if raw_val else "—"

            # Bold company name
            if key == "company":
                cell_content = (
                    f'<span style="font-family:{_FONT_SERIF};font-weight:bold;'
                    f'color:{_COLOR_PRIMARY};">{val}</span>'
                )
            elif key == "signal":
                cell_content = (
                    f'<span style="font-family:{_FONT_SERIF};font-size:13px;'
                    f'color:{_COLOR_PRIMARY};font-style:italic;">{val}</span>'
                )
            elif key == "reported_size":
                cell_content = (
                    f'<span style="color:{_COLOR_NAVY};font-weight:bold;">{val}</span>'
                )
            else:
                cell_content = f'<span style="color:{_COLOR_PRIMARY};">{val}</span>'

            parts.append(
                f'<td style="padding:10px 12px;text-align:{align};'
                f'border-bottom:1px solid #e8e4dc;vertical-align:top;">'
                f"{cell_content}</td>"
            )
        parts.append("</tr>")

    parts.append("</tbody></table>")
    return "".join(parts)


def render_supply_demand_block(
    supply: list[str] | None = None,
    demand: list[str] | None = None,
) -> str:
    """Render the Supply / Demand block for the Deals section.

    If supply/demand lists are provided, renders each item as a formatted
    entry.  When empty, renders the headers as unfilled placeholders so Dom
    can see the section is ready for deals.
    """
    _GOLD = "#c9a84c"

    def _render_items(items: list[str]) -> str:
        if not items:
            return (
                f'<p style="font-family:{_FONT_SERIF};font-size:14px;'
                f'color:{_COLOR_MUTED};font-style:italic;margin:0 0 20px 0;">'
                "No deals listed yet."
                "</p>"
            )
        rows = []
        for item in items:
            rows.append(
                f'<p style="font-family:{_FONT_SERIF};font-size:15px;'
                f'line-height:1.65;color:{_COLOR_PRIMARY};'
                f'margin:0 0 10px 0;padding-left:14px;'
                f'border-left:2px solid {_GOLD};">'
                + html.escape(item.strip())
                + "</p>"
            )
        return "".join(rows)

    supply_html = _render_items(supply or [])
    demand_html = _render_items(demand or [])

    return (
        f'<div style="margin-top:28px;border-top:1px solid {_COLOR_ACCENT};padding-top:24px;">'
        # Supply header
        f'<p style="font-family:{_FONT_SANS};font-size:10px;font-weight:700;'
        f'letter-spacing:3px;text-transform:uppercase;color:#999999;margin:0 0 14px 0;">'
        "SUPPLY"
        "</p>"
        + supply_html
        # Demand header
        + f'<p style="font-family:{_FONT_SANS};font-size:10px;font-weight:700;'
        f'letter-spacing:3px;text-transform:uppercase;color:#999999;margin:20px 0 14px 0;">'
        "DEMAND"
        "</p>"
        + demand_html
        + "</div>"
    )


def render_image_block(
    image_url: str,
    caption: str = "",
    alt: str = "",
) -> str:
    """Render a centred image block with optional caption.

    Args:
        image_url: Absolute URL to the image.
        caption: Optional caption displayed below the image.
        alt: Alt text for the img tag.

    Returns:
        Inline-styled HTML string.
    """
    safe_url = html.escape(image_url, quote=True)
    safe_alt = html.escape(alt)

    parts: list[str] = []
    parts.append(_container_open())
    parts.append(
        '<div style="padding:24px 0;text-align:center;">'
        f'<img src="{safe_url}" alt="{safe_alt}" '
        'style="display:block;max-width:100%;height:auto;margin:0 auto;'
        f'border:1px solid #e8e4dc;" />'
    )

    if caption:
        safe_caption = html.escape(caption)
        parts.append(
            f'<p style="margin:10px 0 0 0;font-family:{_FONT_SANS};font-size:11px;'
            f'color:{_COLOR_MUTED};font-style:italic;text-align:center;">'
            f"{safe_caption}"
            "</p>"
        )

    parts.append("</div>")
    parts.append(_container_close())
    return "".join(parts)


def render_tldr_section(content: str) -> str:
    """Render the TL;DR section — dark navy background, white text, gold bullets.

    Sits at the very top of the newsletter body (after the header) so readers
    get the key takeaways before scrolling into the full sections.
    """
    _BG = "#EDE8DC"
    _TEXT = _COLOR_PRIMARY

    parts: list[str] = []
    parts.append(
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{_BG};">'
        f'<tr><td align="center" style="padding:0;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="{_MAX_WIDTH}" style="max-width:{_MAX_WIDTH};width:100%;background:{_BG};">'
        f'<tr><td style="padding:20px 24px 20px 24px;border-left:4px solid {_COLOR_PRIMARY};">'
    )

    # Anchor
    parts.append('<a name="tldr" id="tldr" style="display:block;"></a>')

    # Label + gold underline
    parts.append(
        f'<p style="margin:0 0 14px 0;font-family:{_FONT_SANS};font-size:10px;'
        f'font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#999999;">'
        "TL;DR"
        "</p>"
    )

    # Body — if already HTML pass through; otherwise render bullet lines
    if content.strip().startswith("<"):
        body = content
    else:
        lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
        items = "".join(
            f'<p style="margin:0 0 8px 0;font-family:{_FONT_SERIF};font-size:14px;'
            f'line-height:1.7;color:{_TEXT};">'
            f'&#8212; {html.escape(line.lstrip("-—*• "))}</p>'
            for line in lines
        )
        body = items

    parts.append(body)
    parts.append("</td></tr></table></td></tr></table>")
    return "".join(parts)


def render_satire_section(content: str) -> str:
    """Render the 'Heard on the Street' satirical section.

    Uses a warm cream background to visually distinguish it from the analytical sections.
    Content is lightly styled with a slightly playful italic treatment.
    """
    _BG = "#faf7f0"
    _LABEL_COLOR = "#8b6914"  # warm gold-brown

    parts: list[str] = []
    parts.append(
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{_BG};">'
        f'<tr><td align="center" style="padding:0;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="{_MAX_WIDTH}" style="max-width:{_MAX_WIDTH};width:100%;background:{_BG};">'
        f'<tr><td style="padding:32px 24px;">'
    )

    parts.append('<a name="heard_on_the_street" id="heard_on_the_street" style="display:block;"></a>')

    parts.append(
        f'<p style="margin:0 0 4px 0;font-family:{_FONT_SANS};font-size:10px;'
        f'letter-spacing:3px;text-transform:uppercase;color:{_LABEL_COLOR};">'
        "HEARD ON THE STREET"
        "</p>"
        f'<div style="height:1px;background:{_COLOR_ACCENT};margin:0 0 20px 0;"></div>'
    )

    if content.strip().startswith("<"):
        body_html = content
    else:
        body_html = (
            f'<p style="margin:0 0 16px 0;font-family:{_FONT_SERIF};font-size:16px;'
            f'line-height:1.8;color:{_COLOR_PRIMARY};font-style:italic;">'
            + html.escape(content).replace(
                "\n\n",
                f'</p><p style="margin:0 0 16px 0;font-family:{_FONT_SERIF};font-size:16px;'
                f'line-height:1.8;color:{_COLOR_PRIMARY};font-style:italic;">',
            )
            .replace("\n", "<br>")
            + "</p>"
        )

    parts.append(body_html)
    parts.append("</td></tr></table></td></tr></table>")
    return "".join(parts)


_DEFAULT_FOOTER_NOTE = "You know a name I should? Hit reply.\n\n— D"


def render_footer(footer_note: str | None = None) -> str:
    """Render newsletter footer with unsubscribe placeholder and legal text.

    Args:
        footer_note: Optional sign-off text. Defaults to Dom's standard sign-off.

    Returns:
        Inline-styled HTML string.
    """
    current_year = datetime.utcnow().year
    note = (footer_note or _DEFAULT_FOOTER_NOTE).strip()

    parts: list[str] = []
    parts.append(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{_BG_WHITE};margin-top:16px;">'
        '<tr><td align="center" style="padding:0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="{_MAX_WIDTH}" style="max-width:{_MAX_WIDTH};width:100%;background:{_BG_WHITE};">'
        '<tr><td style="padding:24px 24px 40px 24px;text-align:left;'
        f'border-top:1px solid {_COLOR_ACCENT};">'
    )

    # Footer sign-off — convert plain newlines to <br> for HTML rendering
    note_html = html.escape(note).replace("\n\n", "<br><br>").replace("\n", "<br>")
    parts.append(
        f'<p style="margin:0 0 20px 0;font-family:{_FONT_SERIF};font-size:15px;'
        f'line-height:1.75;color:{_COLOR_NAVY};">'
        f"{note_html}"
        "</p>"
    )

    # Unsubscribe — Beehiiv will replace {{unsubscribe_url}} server-side
    parts.append(
        f'<p style="margin:0 0 12px 0;font-family:{_FONT_SANS};font-size:11px;'
        f'color:{_COLOR_MUTED};">'
        '<a href="{{unsubscribe_url}}" '
        f'style="color:{_COLOR_MUTED};text-decoration:underline;">Unsubscribe</a>'
        " &nbsp;|&nbsp; "
        '<a href="{{manage_preferences_url}}" '
        f'style="color:{_COLOR_MUTED};text-decoration:underline;">Manage preferences</a>'
        "</p>"
    )

    # Legal
    parts.append(
        f'<p style="margin:0;font-family:{_FONT_SANS};font-size:10px;'
        f'color:{_COLOR_MUTED};line-height:1.6;">'
        f"&copy; {current_year} The Secondaries Intelligence Report. All rights reserved.<br>"
        "For informational purposes only. Not investment advice.<br>"
        "Strictly private circulation — VC secondaries market intelligence."
        "</p>"
    )

    parts.append("</td></tr></table></td></tr></table>")
    return "".join(parts)

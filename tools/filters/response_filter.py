import asyncio
import os
import re
from typing import Any

AI_SLOP_PHRASES = [
    "Certainly!", "Certainly,", "Absolutely!", "Absolutely,",
    "Of course!", "Of course,", "Great question!", "Great question,",
    "I'd be happy to", "I'd be delighted to", "I'm happy to",
    "As an AI", "I should note that", "It's worth noting that",
    "It is worth noting", "In conclusion", "To summarize", "To summarise",
    "I hope this helps", "Feel free to", "Don't hesitate to",
    "Please let me know", "I understand that", "I appreciate that",
    "Thank you for sharing", "That's a great", "Excellent!",
    "I want to clarify", "I need to clarify", "Moving forward",
    "Going forward", "At the end of the day", "The bottom line is",
    "Without a doubt", "Needless to say", "It goes without saying",
    "In today's fast-paced", "In the ever-evolving",
    "I'm here to help", "How can I assist", "Is there anything else",
    "I'd love to", "I would love to", "Let me know if you need",
    "Happy to help", "Let me know if", "Feel free to reach out",
]

_AI_PHRASE_PATTERNS = [re.compile(re.escape(p), re.IGNORECASE) for p in AI_SLOP_PHRASES]


def filter_response(text: str) -> str:
    """Strip all markdown formatting and AI tells from text before sending via Telegram."""
    if not text:
        return text

    # Remove AI slop phrases
    for pattern in _AI_PHRASE_PATTERNS:
        text = pattern.sub("", text)

    # Remove bold markdown: **text** -> text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)

    # Remove italic markdown: *text* -> text
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', text)

    # Remove markdown headers
    text = re.sub(r'#{1,6}\s+', '', text, flags=re.MULTILINE)

    # Remove inline code backticks
    text = re.sub(r'`+([^`]*)`+', r'\1', text)

    # Remove hashtags
    text = re.sub(r'(?<!\w)#(\w+)', r'\1', text)

    # Remove bullet markdown
    text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)

    # Remove numbered list markers
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)

    # Em dashes -> period or comma
    text = text.replace('—', '.').replace('–', '-')
    text = re.sub(r'\s+[.]\s+', '. ', text)

    # Limit exclamation marks to max 1 per response
    exclamations = text.count('!')
    if exclamations > 1:
        first_found = False
        result = []
        for char in text:
            if char == '!' and not first_found:
                result.append('!')
                first_found = True
            elif char == '!':
                result.append('.')
            else:
                result.append(char)
        text = ''.join(result)

    # Collapse triple+ line breaks
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Clean up extra spaces
    text = re.sub(r'  +', ' ', text)

    text = text.strip()
    return text


def split_telegram_message(text: str, max_len: int = 4000) -> list[str]:
    """Split filtered Telegram text on paragraph boundaries where possible."""
    clean = filter_response(text)
    if not clean:
        return []
    if len(clean) <= 4096:
        return [clean]

    chunks: list[str] = []
    current = ""
    for paragraph in clean.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > max_len:
            split_at = paragraph.rfind("\n", 0, max_len)
            if split_at < max_len // 2:
                split_at = paragraph.rfind(" ", 0, max_len)
            if split_at < max_len // 2:
                split_at = max_len
            chunks.append(paragraph[:split_at].strip())
            paragraph = paragraph[split_at:].strip()
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


async def send_telegram_message_safe(
    text: str,
    chat_id: str | int | None = None,
    bot: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Filter and chunk every outbound Telegram message."""
    target = chat_id or os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
    if not target:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_ID is not configured")
    if bot is None:
        from telegram import Bot
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
        bot = Bot(token=token)

    chunks = split_telegram_message(text)
    result = None
    for index, chunk in enumerate(chunks):
        if index:
            await asyncio.sleep(0.5)
        result = await bot.send_message(chat_id=target, text=chunk, **kwargs)
    return result

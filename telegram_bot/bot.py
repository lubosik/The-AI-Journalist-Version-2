"""Thin wrapper around python-telegram-bot for HERALD delivery."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup


class HeraldExtBot:
    """Minimal bot wrapper used by the orchestrator delivery path."""

    def __init__(self, token: str | None = None):
        self._token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._bot = Bot(token=self._token)

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
        **kwargs,
    ) -> Any:
        from filters.response_filter import split_telegram_message
        chunks = split_telegram_message(text)
        result = None
        for i, chunk in enumerate(chunks):
            if i:
                await asyncio.sleep(0.4)
            result = await self._bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=reply_markup if i == len(chunks) - 1 else None,
                parse_mode=parse_mode,
                **kwargs,
            )
        return result

    async def send_document(self, chat_id: str | int, document: Any, caption: str = "", **kwargs) -> Any:
        return await self._bot.send_document(chat_id=chat_id, document=document, caption=caption, **kwargs)

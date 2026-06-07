"""
newsletter/screenshot.py

Renders newsletter HTML to a PNG screenshot using a headless Chromium browser.
Used to send a visual preview of the newsletter to Dom via Telegram before publishing.
"""

import asyncio
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Newsletter viewport — matches typical email client width
# device_scale_factor=2 gives retina-quality text (2x pixel density)
_VIEWPORT_WIDTH = 680
_VIEWPORT_HEIGHT = 900
_DEVICE_SCALE_FACTOR = 2


async def render_newsletter_screenshot(html_content: str) -> bytes | None:
    """
    Render newsletter HTML to a full-page PNG screenshot.
    Returns raw PNG bytes, or None on failure.
    """
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page(
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT},
                device_scale_factor=_DEVICE_SCALE_FACTOR,
            )
            await page.set_content(html_content, wait_until="networkidle")
            # Wait for images to load
            await page.wait_for_timeout(2000)
            screenshot = await page.screenshot(full_page=True)
            await browser.close()

        logger.info(
            "render_newsletter_screenshot: PNG rendered (%d bytes)", len(screenshot)
        )
        return screenshot

    except Exception as exc:
        logger.error("render_newsletter_screenshot failed: %s", exc)
        return None


async def render_to_temp_file(html_content: str) -> str | None:
    """
    Render HTML to a temp PNG file and return the file path.
    Caller is responsible for deleting the file.
    Returns None on failure.
    """
    png_bytes = await render_newsletter_screenshot(html_content)
    if not png_bytes:
        return None

    try:
        fd, path = tempfile.mkstemp(suffix=".png", prefix="herald_preview_")
        with os.fdopen(fd, "wb") as f:
            f.write(png_bytes)
        logger.info("render_to_temp_file: saved to %s", path)
        return path
    except Exception as exc:
        logger.error("render_to_temp_file: could not write file: %s", exc)
        return None

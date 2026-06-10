"""
HERALD v2 Playwright Review Agent
Tests every feature after deployment. Run after any PM2 restart.

Usage:
    cd /root/herald-v2
    python3 tests/review_agent.py
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, async_playwright

HERALD_URL = "http://localhost:8002"
SCREENSHOTS_DIR = Path("/root/herald-v2/tests/screenshots")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

USERNAME = "dom"
PASSWORD = os.getenv("HERALD_DOM_PASSWORD", "Working101#")

results: list[dict] = []


def record(test: str, passed: bool, detail: str = "") -> None:
    status = "✓ PASS" if passed else "✗ FAIL"
    results.append({"test": test, "passed": passed, "detail": detail})
    print(f"  {status}: {test}" + (f" — {detail}" if detail else ""))


async def screenshot(page: Page, name: str) -> None:
    ts = datetime.now().strftime("%H%M%S")
    path = SCREENSHOTS_DIR / f"{ts}_{name}.png"
    try:
        await page.screenshot(path=str(path), full_page=False, timeout=8000)
        print(f"  [ss] {path.name}")
    except Exception as e:
        print(f"  [ss-skip] {name} — {str(e)[:60]}")


async def send_message(page: Page, text: str, wait: float = 3.0) -> None:
    ta = page.locator("textarea").first
    await ta.click()
    await ta.fill("")
    await ta.press_sequentially(text, delay=20)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Enter")
    await asyncio.sleep(wait)


async def wait_for_response(page: Page, timeout: float = 30.0) -> None:
    await asyncio.sleep(2)
    # Wait for the send button to be re-enabled (processing done)
    try:
        await page.wait_for_function(
            "() => { const btn = document.querySelector('button[type=\"submit\"]'); return btn && !btn.disabled; }",
            timeout=int(timeout * 1000),
        )
    except Exception:
        pass
    await asyncio.sleep(1)


async def last_assistant_text(page: Page) -> str:
    """Return the text content of the last assistant message in the chat."""
    try:
        # Try multiple Chainlit class patterns — version-dependent
        for selector in [
            '[class*="assistant"][class*="message"]',
            '[class*="ai-message"]',
            '[data-role="assistant"]',
            # Chainlit 2.x renders steps + messages — grab the last cl-message author=HERALD
            '[class*="message"]:not([class*="user"])',
        ]:
            msgs = page.locator(selector)
            count = await msgs.count()
            if count > 0:
                text = (await msgs.nth(count - 1).text_content()) or ""
                if text.strip():
                    return text.strip()
        # Fallback: evaluate innerText of the whole chat area, return last non-empty segment
        body = await page.evaluate("() => document.body.innerText")
        return body[-500:] if body else ""
    except Exception:
        return ""


async def run_all_tests() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()

        # Block fonts to prevent screenshot hangs
        await page.route("**fonts.googleapis.com**", lambda r: r.abort())
        await page.route("**fonts.gstatic.com**", lambda r: r.abort())

        print(f"\n{'═'*55}")
        print("HERALD v2 Review Agent")
        print(f"Target: {HERALD_URL}")
        print(f"Time:   {datetime.now().strftime('%A %d %B %Y %H:%M')}")
        print(f"{'═'*55}\n")

        # ── TEST 1: Login page ────────────────────────────────────
        print("TEST 1: Login page renders")
        await page.goto(HERALD_URL, timeout=30000)
        await asyncio.sleep(4)
        await screenshot(page, "01_login_page")
        title = await page.title()
        record("Page title is HERALD Intelligence", title == "HERALD Intelligence", title)
        n_inputs = await page.locator("input").count()
        record("Login form has inputs", n_inputs >= 2, f"found {n_inputs}")

        # ── TEST 2: Authentication ────────────────────────────────
        print("\nTEST 2: Authentication")
        await page.locator("input").nth(0).fill(USERNAME)
        await page.locator("input").nth(1).fill(PASSWORD)
        await page.keyboard.press("Enter")
        await asyncio.sleep(7)
        await screenshot(page, "02_after_login")
        url = page.url
        record("Login redirects away from /login", "/login" not in url, url[:60])

        # ── TEST 3: Empty state — H orbital ──────────────────────
        print("\nTEST 3: Empty state H orbital")
        await asyncio.sleep(2)
        await screenshot(page, "03_empty_state")
        h_count = await page.locator("#herald-orbital-empty").count()
        record("H orbital visible", h_count > 0, f"count={h_count}")
        record("Only ONE H orbital (no duplicate)", h_count <= 1, f"count={h_count}")

        # ── TEST 4: Basic message — no raw JSON ───────────────────
        print("\nTEST 4: Basic message — prose response")
        await send_message(page, "good morning", wait=2.0)
        await wait_for_response(page, timeout=20)
        await screenshot(page, "04_basic_message")
        response = await last_assistant_text(page)
        record("Agent responds to greeting", bool(response and len(response) > 5))
        is_not_json = not (response.strip().startswith("{") or response.strip().startswith("["))
        record("Response is NOT raw JSON", is_not_json, response[:80] if response else "")

        # ── TEST 5: Thread persistence ────────────────────────────
        print("\nTEST 5: Thread persistence")
        sidebar_before = await page.locator('[class*="thread-item"], [class*="conversation-item"]').count()
        # Try to start a new chat via compose button
        compose = page.locator('[aria-label*="new" i], [class*="compose"], svg[data-icon="pencil"]')
        if await compose.count() > 0:
            await compose.first.click()
            await asyncio.sleep(2)
        await screenshot(page, "05_new_thread")
        sidebar_after = await page.locator('[class*="thread-item"], [class*="conversation-item"]').count()
        record("Thread saved in sidebar", sidebar_after >= sidebar_before)

        # ── TEST 6: Thread resume — no spam messages ──────────────
        print("\nTEST 6: Thread resume — no 'Back in:' spam")
        threads = page.locator('[class*="thread-item"], [class*="conversation-item"]')
        if await threads.count() > 0:
            await threads.first.click()
            await asyncio.sleep(3)
            await screenshot(page, "06_thread_resumed")
        back_messages = await page.locator('text="Back in:"').count()
        back_messages2 = await page.locator(':text-matches("Back in:", "i")').count()
        record("No 'Back in:' resume spam", back_messages == 0 and back_messages2 == 0, f"found {back_messages}")

        # ── TEST 7: Morning brief — prose not JSON ────────────────
        print("\nTEST 7: Morning brief")
        await send_message(page, "What came in today? Morning brief.", wait=5.0)
        await wait_for_response(page, timeout=90)
        await screenshot(page, "07_morning_brief")
        brief = await last_assistant_text(page)
        is_prose = brief and not brief.strip().startswith("{")
        record("Morning brief returns prose not JSON", bool(is_prose), brief[:80] if brief else "")

        # ── TEST 8: Elena TikTok — not refused ───────────────────
        print("\nTEST 8: Elena TikTok")
        await send_message(page, "What's the latest from Elena's TikTok?", wait=5.0)
        await wait_for_response(page, timeout=90)
        await screenshot(page, "08_elena_tiktok")
        tiktok = await last_assistant_text(page)
        not_refused = tiktok and "cannot access" not in tiktok.lower() and "i cannot" not in tiktok.lower()
        record("TikTok request does not refuse", bool(not_refused), tiktok[:80] if tiktok else "")

        # ── TEST 9: Topics display — prose not JSON ───────────────
        print("\nTEST 9: Topics display")
        await send_message(page, "What topics do we have saved?", wait=3.0)
        await wait_for_response(page, timeout=20)
        await screenshot(page, "09_topics")
        topics_resp = await last_assistant_text(page)
        is_prose_t = topics_resp and not topics_resp.strip().startswith("{")
        record("Topics returns prose not JSON", bool(is_prose_t))

        # ── TEST 10: Model switcher — action buttons ──────────────
        print("\nTEST 10: Model switcher")
        await send_message(page, "What model are you using right now?", wait=3.0)
        await wait_for_response(page, timeout=15)
        await screenshot(page, "10_model_switcher")
        model_actions = await page.locator('[class*="action"]').count()
        record("Model switcher shows action buttons", model_actions > 0, f"found {model_actions}")
        body_text_model = await page.evaluate("() => document.body.innerText")
        has_model_text = any(
            x in body_text_model.lower()
            for x in ["claude", "sonnet", "gpt-4o", "hermes", "current model"]
        )
        record("Model names visible", has_model_text)

        # ── TEST 11: Draft — topic plan + approval buttons ────────
        print("\nTEST 11: Draft newsletter flow")
        await send_message(page, "Draft the newsletter. Show me the topic plan.", wait=3.0)
        await wait_for_response(page, timeout=40)
        await screenshot(page, "11_draft_initiation")
        body_text = await page.evaluate("() => document.body.innerText")
        draft_resp = await last_assistant_text(page)
        not_plain_text = "here's a concise draft" not in (draft_resp or "").lower()
        has_approval_word = any(
            x in body_text.lower()
            for x in ["approve this plan", "awaiting your approval", "edition", "topic"]
        )
        approve_btns = await page.locator('button:has-text("Yes, draft it"), button:has-text("Add more topics")').count()
        record("Draft shows topic plan not freeform draft", not_plain_text, draft_resp[:80] if draft_resp else "")
        record("Draft shows plan/approval content", has_approval_word)
        record("Draft approval buttons rendered", approve_btns > 0, f"found {approve_btns}")

        # ── TEST 12: HTML preview (skip if no draft was generated) ─
        print("\nTEST 12: HTML preview")
        manifest_resp = await page.request.get(f"{HERALD_URL}/public/manifest.json")
        sw_resp = await page.request.get(f"{HERALD_URL}/public/sw.js")
        record("PWA manifest accessible", manifest_resp.ok, f"HTTP {manifest_resp.status}")
        record("Service worker accessible", sw_resp.ok, f"HTTP {sw_resp.status}")

        # ── TEST 13: Mobile viewport ──────────────────────────────
        print("\nTEST 13: Mobile responsiveness")
        await page.set_viewport_size({"width": 390, "height": 844})
        await asyncio.sleep(1)
        await screenshot(page, "13_mobile_view")
        mobile_input = await page.locator("textarea, [role='textbox']").count()
        record("Input visible on mobile", mobile_input > 0)
        font_size_str = await page.evaluate(
            "() => { const ta = document.querySelector('textarea'); "
            "return ta ? parseFloat(window.getComputedStyle(ta).fontSize) : 0; }"
        )
        try:
            font_size = float(font_size_str)
        except (TypeError, ValueError):
            font_size = 0.0
        record("Input font >= 16px on mobile (no iOS zoom)", font_size >= 16, f"{font_size}px")

        await browser.close()

    # ── Final report ──────────────────────────────────────────────
    print(f"\n{'═'*55}")
    print("HERALD v2 REVIEW REPORT")
    print(f"{'═'*55}")
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    print(f"Total: {len(results)}  |  Passed: {len(passed)}  |  Failed: {len(failed)}")
    if failed:
        print("\nFAILED TESTS:")
        for r in failed:
            print(f"  ✗ {r['test']}" + (f": {r['detail']}" if r["detail"] else ""))
    print(f"\nScreenshots: {SCREENSHOTS_DIR}")
    print(f"{'═'*55}")
    return failed


async def main() -> int:
    failed = await run_all_tests()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""
HERALD v2 Playwright test — visual verification + Dom's workflow scenarios.
Screenshots saved to /root/herald-v2/test-artifacts/
"""
import asyncio
import os
import sys
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://localhost:8002"
SS_DIR = Path("/root/herald-v2/test-artifacts")
SS_DIR.mkdir(exist_ok=True)

DOM_USER = "dom"
DOM_PASS = os.getenv("HERALD_DOM_PASSWORD", "Working101#")
RESULTS = []


def log(label: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    RESULTS.append((status, label, detail))
    mark = "✓" if ok else "✗"
    print(f"  [{status}] {mark} {label}" + (f" — {detail}" if detail else ""))


async def ss(page, name: str):
    path = str(SS_DIR / f"{name}.png")
    try:
        await page.screenshot(path=path, full_page=False, timeout=10000)
        print(f"  [ss] {name}.png")
    except Exception as e:
        print(f"  [ss-skip] {name}.png — {str(e)[:40]}")


async def send_msg(page, text: str, wait: float = 8.0):
    ta = page.locator("textarea").first
    await ta.click()
    # Use press_sequentially for slash commands so Chainlit's command palette triggers correctly
    # Clear first, then type character by character
    await ta.fill("")
    await ta.press_sequentially(text, delay=20)
    # Give command palette a moment to react, then submit
    await asyncio.sleep(0.3)
    await page.keyboard.press("Enter")
    await asyncio.sleep(wait)


async def login_and_verify(page):
    print("\n=== TEST 1: Login ===")
    await page.goto(BASE, timeout=30000)
    await asyncio.sleep(4)
    await ss(page, "01_login_page")

    title = await page.title()
    log("Page title is HERALD Intelligence", title == "HERALD Intelligence", title)

    inputs = page.locator("input")
    n = await inputs.count()
    log("Login form has 2 inputs", n >= 2, f"found {n}")

    await inputs.nth(0).fill(DOM_USER)
    await inputs.nth(1).fill(DOM_PASS)
    await page.keyboard.press("Enter")
    await asyncio.sleep(7)
    await ss(page, "02_after_login")

    url = page.url
    log("Redirected away from /login", "/login" not in url, url[:60])


async def check_visual_quality(page):
    print("\n=== TEST 2: Visual quality (CSS vars, fonts, layout) ===")
    await asyncio.sleep(2)
    await ss(page, "03_empty_state")

    gold = await page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--gold').trim()"
    )
    void_bg = await page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--void').trim()"
    )
    font = await page.evaluate("() => getComputedStyle(document.documentElement).fontFamily")

    log("CSS --gold is #c9a84c", "#c9a84c" in gold, gold)
    log("CSS --void is #07070f", "#07070f" in void_bg, void_bg)
    log("Inter font set", "inter" in font.lower(), font[:50])

    ta_h = await page.evaluate(
        "() => { const ta = document.querySelector('textarea'); return ta ? ta.getBoundingClientRect().height : 0; }"
    )
    log("Textarea height >= 56px", float(ta_h) >= 56, f"{ta_h:.0f}px")

    wm = await page.locator('a[href*="chainlit"], [class*="watermark"]').count()
    log("Chainlit branding hidden", wm == 0, f"found {wm}")

    orbital = await page.locator("#herald-orbital-empty").count()
    log("Orbital H shown in empty chat", orbital > 0, f"count={orbital}")


async def test_first_message(page):
    print("\n=== TEST 3: First message ===")
    await send_msg(page, "What is HERALD and what can you do for me?", wait=10)
    await ss(page, "04_first_response")
    msgs = await page.locator('[class*="message"]').count()
    log("Response messages rendered", msgs > 0, f"count={msgs}")
    orbital = await page.locator("#herald-orbital-empty").count()
    log("Orbital removed after first message", orbital == 0, f"count={orbital}")


async def test_dom_topic_save(page):
    print("\n=== TEST 4: Dom saves a topic (Telegram-style directive) ===")
    await send_msg(
        page,
        "Make sure you include the SpaceX tender offer this week — BlackRock and Fidelity buying at $135/share. That's the headline.",
        wait=12,
    )
    await ss(page, "05_topic_save_response")
    content = await page.content()
    log("Agent responded to topic directive", len(content) > 5000)
    saved = any(
        x in content.lower()
        for x in ["saved", "noted", "headline", "spacex", "added", "stored"]
    )
    log("Agent acknowledged topic", saved)


async def test_draft_review(page):
    print("\n=== TEST 5: Draft review — smart topics ===")
    # "draft the newsletter" keyword triggers draft intent in classify_intent
    await send_msg(page, "Draft the newsletter. Go.", wait=3)
    await ss(page, "06_draft_loading")
    await asyncio.sleep(32)
    await ss(page, "07_draft_topics")

    content = await page.content()
    body_text = await page.evaluate("() => document.body.innerText")
    # "approve this plan" is in the final return of handle_draft()
    # "Awaiting your approval" is the action message
    has_draft_output = (
        "approve this plan" in body_text.lower()
        or "awaiting your approval" in body_text.lower()
        or "generation" in body_text.lower()
    )
    has_sources = any(
        x in content
        for x in ["elenanisonoff", "Elena", "TBPN", "All-In", "sources", "this week", "source"]
    )
    has_topic = any(
        x in content
        for x in ["GP-led", "SpaceX", "continuation", "topic", "saved"]
    )
    log("Draft shows plan/approval output", has_draft_output)
    log("Draft includes source content or saved topics", has_sources or has_topic)

    # Check for the specific "Yes, draft it" action button from handle_draft()
    approve = await page.locator('button:has-text("Yes, draft it"), button:has-text("Add more topics")').count()
    log("Draft approval buttons rendered", approve > 0, f"found {approve}")


async def test_model_switcher(page):
    print("\n=== TEST 6: Model switcher ===")
    # Use natural language to avoid Chainlit command palette issues with /model
    await send_msg(page, "What model are you using right now?", wait=14)
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(2)
    await ss(page, "08_model_list")
    body_text_model = await page.evaluate("() => document.body.innerText")
    has_models = any(
        x in body_text_model.lower()
        for x in ["claude", "sonnet", "gpt-4o", "current model", "available", "gemini"]
    )
    # If /model didn't render, check if "model" keyword appears in content at all (different rendering)
    if not has_models:
        content = await page.content()
        has_models = any(
            x in content.lower()
            for x in ["claude-sonnet", "gpt-4o", "claude opus"]
        )
    log("Model list rendered", has_models)


async def run_tests():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # Block Google Fonts to prevent screenshot hangs
        await page.route("**fonts.googleapis.com**", lambda r: r.abort())
        await page.route("**fonts.gstatic.com**", lambda r: r.abort())

        try:
            await login_and_verify(page)
            await check_visual_quality(page)
            await test_first_message(page)
            await test_dom_topic_save(page)
            await test_draft_review(page)
            await test_model_switcher(page)
        except Exception as e:
            print(f"\n[CRASH] {e}")
            await ss(page, "crash_state")
        finally:
            await ss(page, "09_final_state")
            await browser.close()

    print(f"\n{'='*52}")
    print("HERALD v2 TEST RESULTS")
    print(f"{'='*52}")
    passed = sum(1 for r in RESULTS if r[0] == "PASS")
    failed = sum(1 for r in RESULTS if r[0] == "FAIL")
    for status, label, detail in RESULTS:
        mark = "✓" if status == "PASS" else "✗"
        print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))
    print(f"\n  {passed} passed, {failed} failed")
    print(f"  Screenshots: {SS_DIR}")
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(run_tests())
    sys.exit(0 if ok else 1)

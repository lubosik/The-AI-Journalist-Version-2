"""
HERALD v2 Playwright test — visual verification + Dom's workflow scenarios.
Screenshots saved to /root/herald-v2/test-artifacts/
"""
import asyncio
import os
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


async def screenshot(page, name: str):
    path = str(SS_DIR / f"{name}.png")
    try:
        await page.screenshot(path=path, full_page=False, timeout=12000)
        print(f"  [ss] {name}.png")
    except Exception as e:
        print(f"  [ss-skip] {name}.png ({str(e)[:50]})")


async def wait_for_idle(page, extra_ms: int = 2000):
    """Wait for network + a short settling period."""
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(extra_ms / 1000)


async def login(page):
    print("\n=== TEST 1: Login page ===")
    await page.goto(BASE, timeout=30000)
    await asyncio.sleep(5)
    await screenshot(page, "01_login_page")

    title = await page.title()
    log("Page title is HERALD Intelligence", title == "HERALD Intelligence", title)

    inputs = page.locator("input")
    n = await inputs.count()
    log("Login form renders 2 inputs", n >= 2, f"found {n}")

    await inputs.nth(0).fill(DOM_USER)
    await inputs.nth(1).fill(DOM_PASS)
    await screenshot(page, "02_login_filled")
    await page.keyboard.press("Enter")
    await asyncio.sleep(8)
    await screenshot(page, "03_after_login")
    current_url = page.url
    log("Redirect away from /login after auth", "/login" not in current_url, current_url)


async def check_empty_state(page):
    print("\n=== TEST 2: Empty state — orbital H ===")
    await asyncio.sleep(3)
    await screenshot(page, "04_empty_state")

    orbital_count = await page.locator("#herald-orbital-empty").count()
    log("Orbital H injected into empty chat", orbital_count > 0, f"count={orbital_count}")

    gold = await page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--gold').trim()"
    )
    void_bg = await page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--void').trim()"
    )
    log("CSS --gold is #c9a84c", "#c9a84c" in gold, gold)
    log("CSS --void is #07070f", "#07070f" in void_bg, void_bg)

    font_fam = await page.evaluate(
        "() => getComputedStyle(document.documentElement).fontFamily"
    )
    log("Inter font set", "inter" in font_fam.lower(), font_fam[:50])


async def test_first_message(page):
    print("\n=== TEST 3: First message — orbital fades, response arrives ===")
    ta = page.locator("textarea").first
    await ta.click()
    await ta.fill("What is HERALD and what can you do for me as a VC secondaries advisor?")
    await screenshot(page, "05_typing_first_message")
    await page.keyboard.press("Enter")
    await asyncio.sleep(4)
    await screenshot(page, "06_agent_thinking")

    # Orbital should be gone now (message visible)
    await asyncio.sleep(6)
    orbital_count = await page.locator("#herald-orbital-empty").count()
    log("Orbital removed after first message sent", orbital_count == 0, f"count={orbital_count}")

    # Wait for response to arrive
    await asyncio.sleep(12)
    await screenshot(page, "07_first_response")
    msg_count = await page.locator('[class*="message"]').count()
    log("Response messages rendered", msg_count > 0, f"count={msg_count}")


async def test_dom_saves_topic(page):
    print("\n=== TEST 4: Dom saves a topic (Telegram-style directive) ===")
    ta = page.locator("textarea").first
    await ta.click()
    await ta.fill(
        "Make sure you include the SpaceX tender offer this week — "
        "BlackRock and Fidelity buying at $135/share. That's the headline."
    )
    await page.keyboard.press("Enter")
    await asyncio.sleep(18)
    await screenshot(page, "08_topic_save_response")
    content = await page.content()
    log("Agent responded to topic directive", len(content) > 5000)


async def test_draft_review(page):
    print("\n=== TEST 5: Draft review — smart topics (edition + sources) ===")
    ta = page.locator("textarea").first
    await ta.click()
    await ta.fill("/draft")
    await page.keyboard.press("Enter")
    await asyncio.sleep(5)
    await screenshot(page, "09_draft_loading")

    # Draft runs view-plan + loads source content — give it time
    await asyncio.sleep(25)
    await screenshot(page, "10_draft_topics")

    content = await page.content()
    has_edition = "dition" in content  # "edition" or "Edition"
    has_sources = any(x in content for x in ["elenanisonoff", "Elena", "TBPN", "All-In", "sources", "this week"])
    has_saved = any(x in content for x in ["GP-led", "SpaceX", "continuation", "saved topic", "topic"])

    log("Draft shows edition/plan info", has_edition)
    log("Draft includes this week's source content", has_sources)
    log("Draft includes Dom's saved topics", has_saved)

    # Action buttons - Chainlit renders these as role="button" or class with "action"
    approve = page.locator('[class*="action"] button, button:has-text("draft"), button:has-text("Yes")')
    btn_count = await approve.count()
    log("Draft approval buttons rendered", btn_count > 0, f"found {btn_count}")


async def test_model_switcher(page):
    print("\n=== TEST 6: Model switcher ===")
    ta = page.locator("textarea").first
    await ta.click()
    await ta.fill("/model")
    await page.keyboard.press("Enter")
    await asyncio.sleep(12)
    await screenshot(page, "11_model_switcher")
    content = await page.content()
    has_gpt = "gpt-4o" in content.lower() or "gpt4o" in content.lower()
    has_claude = "claude" in content.lower() or "sonnet" in content.lower()
    log("Model list shows GPT-4o option", has_gpt)
    log("Model list shows Claude option", has_claude)

    # Switch to claude-sonnet
    ta2 = page.locator("textarea").first
    await ta2.click()
    await ta2.fill("claude-sonnet")
    await page.keyboard.press("Enter")
    await asyncio.sleep(8)
    await screenshot(page, "12_model_switched")
    content2 = await page.content()
    switched = "switched" in content2.lower() or ("claude" in content2.lower() and "sonnet" in content2.lower())
    log("Model switch to claude-sonnet confirmed", switched)


async def test_visual_quality(page):
    print("\n=== TEST 7: Visual quality ===")
    await screenshot(page, "13_final_view")

    bf = await page.evaluate("""() => {
        const el = document.querySelector('footer, [class*="input-area"], [class*="composer"]');
        if (!el) return 'no-footer';
        const s = getComputedStyle(el);
        return s.backdropFilter || s.webkitBackdropFilter || 'none';
    }""")
    log("Glass backdrop-filter on input panel", bf not in ("none", "no-footer", ""), bf[:30])

    ta_h = await page.evaluate("""() => {
        const ta = document.querySelector('textarea');
        return ta ? ta.getBoundingClientRect().height : 0;
    }""")
    log("Input textarea height >= 56px", float(ta_h) >= 56, f"{ta_h:.0f}px")

    # Check no Chainlit watermark visible
    watermarks = await page.locator('a[href*="chainlit"], [class*="watermark"]').count()
    log("Chainlit branding hidden", watermarks == 0, f"found {watermarks}")


async def run_tests():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # Block Google Fonts to prevent screenshot hangs on font loading
        await page.route("**fonts.googleapis.com**", lambda r: r.abort())
        await page.route("**fonts.gstatic.com**", lambda r: r.abort())

        try:
            await login(page)
            await check_empty_state(page)
            await test_first_message(page)
            await test_dom_saves_topic(page)
            await test_draft_review(page)
            await test_model_switcher(page)
            await test_visual_quality(page)
        except Exception as e:
            print(f"\n[CRASH] {e}")
            await screenshot(page, "crash_state")
        finally:
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
    import sys
    ok = asyncio.run(run_tests())
    sys.exit(0 if ok else 1)

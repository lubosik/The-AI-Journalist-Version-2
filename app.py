from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import chainlit as cl
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/root/herald-v2/tools")
sys.path.insert(0, "/root/herald-v2")

# Chainlit persistence enables thread history, new chat, and resume.
_db_uri = os.getenv("SUPABASE_DB_URI_ASYNC") or os.getenv("SUPABASE_DB_URI", "")
if _db_uri and "+asyncpg" not in _db_uri:
    _db_uri = _db_uri.replace("postgresql://", "postgresql+asyncpg://", 1)


class HeraldSQLAlchemyDataLayer(SQLAlchemyDataLayer):
    """Bridge Chainlit 2.11 with the existing Supabase Chainlit schema."""

    async def execute_sql(self, query: str, parameters: dict):
        query = query.replace(
            's."metadata" LIKE :favorite_pattern',
            's."metadata"::text LIKE :favorite_pattern',
        )
        return await super().execute_sql(query, parameters)

    async def create_step(self, step_dict):
        step_dict.pop("autoCollapse", None)
        await super().create_step(step_dict)

    async def get_user(self, identifier: str):
        """Get user, auto-creating on first login so the thread endpoint never 404s."""
        from chainlit.user import User
        result = await super().get_user(identifier)
        if result is not None:
            return result
        # First login — upsert a minimal user record so Chainlit can attach threads.
        return await self.create_user(User(identifier=identifier, metadata={}))


@cl.data_layer
def get_data_layer():
    if not _db_uri:
        return None
    return HeraldSQLAlchemyDataLayer(conninfo=_db_uri)


AUTHOR = "HERALD"
URL_RE = re.compile(r"https?://[^\s<>()]+")
HERMES_TIMEOUT = int(os.getenv("HERMES_TIMEOUT_SECONDS", "900"))

COMMANDS = [
    {"id": "research", "icon": "search", "description": "Research a live topic", "button": True},
    {"id": "ingest", "icon": "link", "description": "Ingest and analyse a URL", "button": False},
    {"id": "topics", "icon": "list", "description": "View the current edition plan", "button": True},
    {"id": "brief", "icon": "sunrise", "description": "Run the morning source brief", "button": True},
    {"id": "draft", "icon": "file-text", "description": "Review the plan before drafting", "button": True},
    {"id": "status", "icon": "activity", "description": "Check system and database status", "button": False},
    {"id": "transcript", "icon": "captions", "description": "Find a quote or transcript segment", "button": False},
    {"id": "linkedin", "icon": "share-2", "description": "Create a LinkedIn post", "button": False},
    {"id": "model", "icon": "cpu", "description": "Switch AI model", "button": True},
]

INTENTS = {
    "url_ingest": ("Ingesting content", "link", "Pull the source, read it, and identify the editorial angle."),
    "research": ("Planning research", "search", "Search live sources and return specific evidence and implications."),
    "transcript": ("Locating transcript", "captions", "Search stored transcripts first, then recent channel episodes."),
    "save_topic": ("Saving editorial direction", "bookmark", "Add this instruction to the active newsletter edition."),
    "view_plan": ("Reading edition plan", "list", "Load the active edition and its saved topics."),
    "draft": ("Preparing draft decision", "file-text", "Review the topic plan and wait for explicit approval."),
    "status": ("Checking system health", "activity", "Inspect the active edition and database health."),
    "morning_brief": ("Planning source sweep", "sunrise", "Check Elena, TBPN, and All-In for new material."),
    "linkedin": ("Planning LinkedIn post", "share-2", "Turn the supplied idea into a concise LinkedIn draft."),
    "tiktok_check": ("Checking Elena TikTok", "video", "Pull recent posts from elenanisonoff and summarise."),
    "source_latest": ("Checking source", "rss", "Pull recent content from the requested source and summarise."),
    "conversation": ("Thinking", "sparkles", "Use the conversation context and form a direct editorial response."),
}

AVAILABLE_MODELS = {
    "hermes": {"id": "openai/gpt-4o", "label": "Hermes (Default)", "description": "Your default model — recommended"},
    "gpt-4o": {"id": "openai/gpt-4o", "label": "GPT-4o", "description": "Fast and powerful"},
    "claude-sonnet": {"id": "anthropic/claude-sonnet-4-5", "label": "Claude Sonnet", "description": "Best for writing"},
    "claude-opus": {"id": "anthropic/claude-opus-4-6", "label": "Claude Opus", "description": "Most capable"},
    "gemini-flash": {"id": "google/gemini-flash-1.5", "label": "Gemini Flash", "description": "Fastest"},
    "perplexity": {"id": "perplexity/sonar-pro", "label": "Perplexity Sonar", "description": "Live web search"},
}

OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")

HTML_PREVIEW_DIR = ROOT / "public" / "previews"
HTML_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def get_herald_system() -> str:
    """Build system prompt with current date injected — called per-turn so date stays fresh."""
    today = datetime.now().strftime("%A, %d %B %Y")
    current_time = datetime.now().strftime("%H:%M")
    return f"""You are HERALD — Dom Pandolfo's AI research partner and newsletter journalist.

TODAY: {today} at {current_time}. You are always aware of the current date.
When assessing content recency, use today's date as your reference.

WHO DOM IS:
Dom runs a pre-IPO VC secondaries advisory practice in the UK.
He is a sophisticated institutional investor and deal flow specialist.
His clients are family offices, RIAs, and institutional allocators.
He publishes a weekly newsletter to this audience.
You are his 24/7 research partner and the author of that newsletter.

YOUR DOMAIN — THINK LIKE A 15-YEAR VC SECONDARIES SPECIALIST:
You know the players: Apollo, Lexington, Hamilton Lane, Coller Capital,
Ardian, AlpInvest, HarbourVest, Pantheon, Pomona, Partners Group.
You track: GP-led continuation vehicles, LP secondary interest,
fund stake sales, pre-IPO secondaries, NAV discount pricing,
tender offers, cap table liquidity, DPI vs TVPI dynamics,
J-curve mitigation strategies, and family office LP appetite.
You read every TBPN episode, every All-In Podcast, and Elena Nisonoff's TikTok.
You are opinionated. You have a view. You share it.

THREE CONTENT SOURCES (automated daily):
- Elena Nisonoff (@elenanisonoff) — TikTok — finance and VC commentary
- TBPN Podcast (YouTube @tbpn) — VC and PE market commentary
- All-In Podcast (YouTube @allinpodcast) — macro + tech + PE crossover

YOUR TONE:
Short sentences. Direct. Opinionated when you have a view.
You do not hedge everything. You do not sound like a chatbot.
You sound like a sharp colleague who has been in the room.
You are HERALD. You have a voice. Use it.

HARD RULES:
1. NEVER respond in raw JSON. Always natural language prose.
2. NEVER say "As an AI" or "I should note" or "I cannot access TikTok/YouTube".
   If you cannot do something, say why and offer an alternative.
3. NEVER ask what a URL is. Process it immediately.
4. NEVER say only "stored". Return the insight after storing.
5. NEVER use asterisks, em dashes, hashtags, or bullet points in casual chat.
6. When asked about TikTok/YouTube/podcast: check the database first,
   scrape if needed, summarise with today's date as context.
7. Keep casual replies under 150 words.
8. Sound opinionated. Not like you summarised something.

TOOLS:
/research — live web search for current deal activity
/topics — view this week's edition plan
/brief — morning ingestion from the three sources
/draft — review topics and initiate newsletter draft
/status — system health check
/linkedin — generate a LinkedIn post
/model — switch AI model
Type any URL to ingest and analyse it."""


# Module-level fallback for build_prompt (overridden per-turn in on_message)
SYSTEM_PROMPT = get_herald_system()


# ── JSON → prose sanitiser ────────────────────────────────────────────────────

def json_to_natural_language(data: Any) -> str:
    """Convert a parsed JSON value to readable prose. Never return raw JSON."""
    if isinstance(data, list):
        if not data:
            return "Nothing to report."
        return "\n".join(f"- {item}" for item in data if item)

    if isinstance(data, dict):
        # Morning brief format: {"sources": {"Elena": N, ...}, "new_items": N}
        if "sources" in data and "new_items" in data:
            total = data.get("new_items", 0)
            sources = data.get("sources", {})
            if total == 0:
                return "Nothing new from sources today."
            lines = [f"New this week: {total} item{'s' if total != 1 else ''} across sources."]
            for source, count in sources.items():
                if count and count > 0:
                    lines.append(f"{source}: {count} new item{'s' if count != 1 else ''}")
            return "\n".join(lines)

        # Edition/topics format
        if "topics" in data or "edition_number" in data or "active_edition" in data:
            edition = data.get("edition_number") or data.get("active_edition") or data.get("edition", {}).get("active_edition", "current")
            topics = data.get("topics", [])
            if not topics:
                return f"Edition {edition} has no saved topics yet. Drop links or tell me what must be covered."
            lines = [f"Edition {edition}. {len(topics)} saved topic{'s' if len(topics) != 1 else ''}:"]
            for t in topics[:20]:
                label = t.get("topic") or t.get("title") or str(t) if isinstance(t, dict) else str(t)
                lines.append(f"- {label}")
            return "\n".join(lines)

        # Error dict
        if "error" in data and len(data) <= 3:
            return f"That action encountered an issue: {data['error']}"

        # Generic fallback
        lines = []
        for key, value in data.items():
            key_label = key.replace("_", " ").title()
            if isinstance(value, dict):
                lines.append(f"{key_label}:")
                for k2, v2 in value.items():
                    lines.append(f"  {k2}: {v2}")
            elif isinstance(value, list):
                lines.append(f"{key_label}: {', '.join(str(v) for v in value)}")
            else:
                lines.append(f"{key_label}: {value}")
        return "\n".join(lines) if lines else str(data)

    return str(data)


def sanitise_response(text: str) -> str:
    """
    HARD RULE: Dom must never see raw JSON as a response.
    Convert any JSON/code-block responses to natural language prose.
    """
    if not text:
        return text

    stripped = text.strip()

    # Detect if the entire response is JSON
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
            return json_to_natural_language(data)
        except (json.JSONDecodeError, ValueError):
            pass

    # Detect JSON inside a fenced code block
    json_block = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", stripped, re.DOTALL)
    if json_block:
        try:
            data = json.loads(json_block.group(1))
            prose = json_to_natural_language(data)
            return re.sub(
                r"```(?:json)?\s*(?:\{.*?\}|\[.*?\])\s*```",
                prose,
                stripped,
                flags=re.DOTALL,
            )
        except (json.JSONDecodeError, ValueError):
            pass

    return text


# ── HTML preview ─────────────────────────────────────────────────────────────

async def show_html_preview(html_content: str, title: str = "Newsletter Preview") -> None:
    """Save HTML to public dir and show as an inline iframe with action buttons."""
    html_hash = hashlib.md5(html_content.encode()).hexdigest()[:8]
    filename = f"preview_{html_hash}.html"
    filepath = HTML_PREVIEW_DIR / filename
    preview_url = f"/public/previews/{filename}"
    tmp_path = f"/tmp/herald_newsletter_{html_hash}.html"

    filepath.write_text(html_content, encoding="utf-8")
    Path(tmp_path).write_text(html_content, encoding="utf-8")

    preview_card = (
        f'<div style="border:1px solid rgba(201,168,76,0.3);border-radius:12px;'
        f'overflow:hidden;margin:8px 0;">'
        f'<div style="background:rgba(201,168,76,0.08);padding:12px 16px;'
        f'border-bottom:1px solid rgba(201,168,76,0.2);">'
        f'<span style="font-family:Inter,sans-serif;font-size:13px;font-weight:600;'
        f'color:#c9a84c;">{title}</span></div>'
        f'<iframe src="{preview_url}" style="width:100%;height:540px;border:none;'
        f'background:white;" sandbox="allow-same-origin allow-scripts"></iframe>'
        f"</div>"
    )

    actions = [
        cl.Action(name="export_html", payload={"html_path": tmp_path, "filename": f"herald_{html_hash}.html"}, label="Download HTML", icon="download"),
        cl.Action(name="edit_newsletter_content", payload={"html_path": tmp_path}, label="Edit content", icon="pencil"),
        cl.Action(name="approve_newsletter", payload={"issue_id": "latest"}, label="Approve and publish", icon="check"),
    ]
    await cl.Message(content=preview_card, actions=actions, author=AUTHOR).send()


# ── Auth & starters ───────────────────────────────────────────────────────────

@cl.password_auth_callback
def auth_callback(username: str, password: str):
    dom_email = os.getenv("HERALD_DOM_EMAIL", "dom@herald.local").lower()
    admin_email = os.getenv("HERALD_ADMIN_EMAIL", "lubosi@herald.local").lower()
    credentials = {
        "dom": ("dom", os.getenv("HERALD_DOM_PASSWORD", ""), "client"),
        dom_email: ("dom", os.getenv("HERALD_DOM_PASSWORD", ""), "client"),
        "lubosi": ("lubosi", os.getenv("HERALD_ADMIN_PASSWORD", ""), "admin"),
        admin_email: ("lubosi", os.getenv("HERALD_ADMIN_PASSWORD", ""), "admin"),
    }
    credential = credentials.get(username.strip().lower())
    if not credential:
        return None
    identifier, expected, role = credential
    if not expected or not hmac.compare_digest(password, expected):
        return None
    return cl.User(identifier=identifier, metadata={"role": role, "provider": "credentials"})


@cl.set_starters
async def set_starters():
    return [
        cl.Starter(
            label="Morning brief",
            message="Run the morning brief. What came in from Elena, TBPN, and All-In today?",
            icon="/public/icons/brief.svg",
        ),
        cl.Starter(
            label="Edition plan",
            message="What topics do we have saved for this week's newsletter?",
            icon="/public/icons/plan.svg",
        ),
        cl.Starter(
            label="System status",
            message="Full system status. Database, edition state, and pipeline health.",
            icon="/public/icons/status.svg",
        ),
        cl.Starter(
            label="Draft newsletter",
            message="Draft the newsletter. Show me the topic plan.",
            icon="/public/icons/draft.svg",
        ),
    ]


async def register_commands() -> None:
    await cl.context.emitter.set_commands(COMMANDS)


# ── Session lifecycle ─────────────────────────────────────────────────────────

@cl.on_chat_start
async def on_start():
    # Guard against duplicate WebSocket dispatch
    if cl.user_session.get("_initialized"):
        return
    cl.user_session.set("_initialized", True)
    cl.user_session.set("_loaded_thread_id", None)
    cl.user_session.set("history", [{"role": "system", "content": get_herald_system()}])
    cl.user_session.set("awaiting_draft_approval", False)
    cl.user_session.set("cross_thread_loaded", False)
    cl.user_session.set("selected_model", "hermes")
    await register_commands()


@cl.on_chat_resume
async def on_resume(thread):
    """
    Restore a persisted conversation selected from the thread sidebar.
    CRITICAL: Guard against WebSocket reconnect re-firing this handler.
    Never send a message — silently restore context only.
    Dom sees the existing conversation exactly as he left it.
    """
    thread_id = thread.get("id", "")

    # If this thread is already loaded in this session, do nothing
    if cl.user_session.get("_loaded_thread_id") == thread_id:
        return

    cl.user_session.set("_loaded_thread_id", thread_id)
    cl.user_session.set("_initialized", True)

    history = [{"role": "system", "content": get_herald_system()}]
    for step in thread.get("steps", []):
        step_type = step.get("type", "")
        output = step.get("output", "")
        if not output:
            continue
        if step_type == "user_message":
            history.append({"role": "user", "content": output})
        elif step_type == "assistant_message":
            history.append({"role": "assistant", "content": output})

    cl.user_session.set("history", history[-30:])
    cl.user_session.set("awaiting_draft_approval", False)
    cl.user_session.set("cross_thread_loaded", True)
    cl.user_session.set("selected_model", cl.user_session.get("selected_model") or "hermes")
    await register_commands()
    # DO NOT send any message here.


# ── Cross-thread context ──────────────────────────────────────────────────────

async def get_cross_thread_context(current_message: str, user_identifier: str = "") -> str:
    """Return relevant assistant context from prior sessions for this user only, or fail silently."""
    try:
        from db.client import get_client

        stop_words = {
            "about", "would", "could", "should", "there", "their",
            "these", "those", "what", "when", "where", "which", "that",
            "this", "with", "have", "from", "they", "been", "will",
        }
        keywords = [
            word.lower()
            for word in re.findall(r"[A-Za-z0-9-]+", current_message)
            if len(word) > 4 and word.lower() not in stop_words
        ][:4]
        if not keywords:
            return ""

        supabase = get_client()

        # Scope to this user's threads only
        user_thread_ids: list[str] = []
        if user_identifier:
            threads_resp = (
                supabase.table("threads")
                .select("id")
                .eq("userId", user_identifier)
                .execute()
            )
            user_thread_ids = [t["id"] for t in (threads_resp.data or [])]

        steps_query = (
            supabase.table("steps")
            .select("output,createdAt")
            .eq("type", "assistant_message")
            .order("createdAt", desc=True)
            .limit(80)
        )
        if user_thread_ids:
            steps_query = steps_query.in_("threadId", user_thread_ids)
        persisted = steps_query.execute()

        legacy = (
            supabase.table("conversation_memory")
            .select("content,created_at")
            .eq("role", "assistant")
            .order("created_at", desc=True)
            .limit(80)
            .execute()
        )
        candidates = [
            {"content": message.get("output") or ""}
            for message in persisted.data or []
        ] + list(legacy.data or [])
        relevant = []
        for message in candidates:
            content = message.get("content") or ""
            if any(keyword in content.lower() for keyword in keywords):
                relevant.append(content[:180].replace("\n", " "))
                if len(relevant) >= 2:
                    break
        if relevant:
            return "Context from past sessions: " + " | ".join(relevant)
    except Exception:
        pass
    return ""


# ── Intent classification ─────────────────────────────────────────────────────

def classify_intent(text: str, command: str | None = None) -> str:
    lower = text.lower().strip()
    command_map = {
        "research": "research",
        "ingest": "url_ingest",
        "topics": "view_plan",
        "brief": "morning_brief",
        "draft": "draft",
        "status": "status",
        "transcript": "transcript",
        "linkedin": "linkedin",
        "model": "model",
    }
    if command and command.lower() in command_map:
        return command_map[command.lower()]
    # Handle slash-prefixed commands typed directly in the textarea
    if lower.startswith("/"):
        cmd = lower[1:].split()[0]
        if cmd in command_map:
            return command_map[cmd]
    if URL_RE.search(text):
        return "url_ingest"
    # Source-specific checks — before generic research
    if any(x in lower for x in ("elena", "elenanisonoff", "tiktok")):
        return "tiktok_check"
    if any(x in lower for x in ("tbpn", "all-in podcast", "all in podcast", "allin podcast")):
        return "source_latest"
    if any(x in lower for x in ("find the transcript", "find where", "part where", "quote from", "said on", "transcript segment")):
        return "transcript"
    if any(x in lower for x in ("research", "find out", "look into", "what's happening", "tell me about")):
        return "research"
    if any(x in lower for x in ("include this", "add this", "save this", "make sure you cover", "put this in", "make sure you include")):
        return "save_topic"
    # Draft check must come before view_plan
    if any(x in lower for x in (
        "draft the newsletter", "draft newsletter", "generate the newsletter",
        "create the edition", "ready to draft", "show me the topic plan",
        "let's draft", "lets draft", "ready to generate", "start drafting",
    )):
        return "draft"
    if any(x in lower for x in ("what topics", "edition plan", "what do we have saved", "what's planned")):
        return "view_plan"
    if any(x in lower for x in ("system status", "status check", "database status", "how is herald")):
        return "status"
    if any(x in lower for x in ("morning brief", "what came in", "what is new today", "what's new today")):
        return "morning_brief"
    if any(x in lower for x in ("linkedin", "repurpose this")):
        return "linkedin"
    return "conversation"


# ── Utilities ─────────────────────────────────────────────────────────────────

def platform_name(url: str) -> str:
    lower = url.lower()
    if "spotify.com" in lower:
        return "Spotify episode"
    if "youtu" in lower:
        return "YouTube video"
    if "tiktok.com" in lower:
        return "TikTok"
    if "twitter.com" in lower or "x.com" in lower:
        return "X post"
    if "instagram.com" in lower:
        return "Instagram post"
    if "linkedin.com" in lower:
        return "LinkedIn post"
    return "web article"


def intent_detail(intent_key: str, text: str, history: list[dict]) -> str:
    display, _, action = INTENTS[intent_key]
    lines = [f"UNDERSTOOD  {display.upper()}", f"NEXT        {action}"]
    urls = URL_RE.findall(text)
    if urls:
        lines.append(f"SOURCE      {platform_name(urls[0])}")
    if len(history) > 1:
        lines.append(f"CONTEXT     {len(history) - 1} prior messages available")
    return "\n".join(lines)


async def run_cli(*args: str, timeout: int = 900) -> dict:
    _env = {
        **os.environ,
        "PYTHONPATH": f"{ROOT / 'tools'}:{ROOT}:/root/herald",
    }
    proc = await asyncio.create_subprocess_exec(
        os.getenv("PYTHON", "python3"),
        str(ROOT / "herald_cli.py"),
        *args,
        cwd=str(ROOT),
        env=_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip() or output)
    if not output:
        return {}
    return json.loads(output.splitlines()[-1])


def _get_selected_model() -> str:
    try:
        key = cl.user_session.get("selected_model") or "hermes"
        model = AVAILABLE_MODELS.get(key)
        if model:
            return model["id"]
        # Fallback: treat value as a raw model ID
        return key
    except Exception:
        return os.getenv("HERALD_CHAT_MODEL", "openai/gpt-4o")


async def run_hermes(prompt: str) -> str:
    api_key = OPENROUTER_KEY or os.getenv("OPENAI_API_KEY")
    if api_key:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=45,
        )
        response = await client.chat.completions.create(
            model=_get_selected_model(),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=900,
        )
        content = response.choices[0].message.content
        if content:
            return content.strip()

    proc = await asyncio.create_subprocess_exec(
        os.getenv("HERMES_COMMAND", "hermes"),
        "-z",
        prompt,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=min(HERMES_TIMEOUT, 20))
    response = stdout.decode(errors="replace").strip()
    if proc.returncode != 0 or not response:
        raise RuntimeError(stderr.decode(errors="replace").strip() or "Reasoning model returned no response")
    return response


def build_prompt(message: str, history: list[dict], tool_context: Any = None) -> str:
    system_prompt = get_herald_system()
    if history and history[0].get("role") == "system":
        system_prompt = history[0].get("content") or system_prompt
    recent = "\n".join(
        f"{item['role'].upper()}: {item['content'][:1800]}"
        for item in history[-20:]
        if item.get("role") != "system"
    )
    context = ""
    if tool_context is not None:
        context = (
            "\n\nA HERALD tool completed. Its result is authoritative. Analyse it rather "
            "than merely saying it was stored:\n"
            + json.dumps(tool_context, ensure_ascii=True, default=str)[:30000]
        )
    return f"{system_prompt}\nRecent conversation:\n{recent or '(new conversation)'}\n\nCurrent request:\n{message}{context}"


def compact_output(value: Any, limit: int = 900) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit].rstrip() + "\n..."


async def stream_response(text: str) -> None:
    text = sanitise_response(text)
    msg = cl.Message(content="", author=AUTHOR)
    await msg.send()
    chunks = re.findall(r"\S+\s*", text)
    for index in range(0, len(chunks), 7):
        await msg.stream_token("".join(chunks[index:index + 7]))
        await asyncio.sleep(0.006)
    await msg.update()


async def analyse_with_hermes(message: str, history: list[dict], context: Any = None) -> str:
    async with cl.Step(name="HERALD is formulating", type="llm", icon="sparkles", default_open=False) as step:
        step.input = message[:180]
        try:
            response = await run_hermes(build_prompt(message, history, context))
            step.output = "Editorial response formed from the available context."
            return sanitise_response(response)
        except Exception as exc:
            step.output = f"Model unavailable: {str(exc)[:180]}"
            if context is not None:
                return sanitise_response(format_tool_fallback(context))
            return "I could not reach the reasoning model. The visible tool steps above show what completed."


def format_tool_fallback(data: Any) -> str:
    if isinstance(data, str):
        return sanitise_response(data)
    if isinstance(data, dict):
        if data.get("findings"):
            return str(data["findings"])
        if data.get("note"):
            return str(data["note"])
        if data.get("message"):
            return str(data["message"])
    return json_to_natural_language(data)


# ── Intent handlers ───────────────────────────────────────────────────────────

async def handle_url(text: str, history: list[dict]) -> str:
    results = []
    for raw_url in URL_RE.findall(text)[:3]:
        url = raw_url.rstrip(".,)")
        async with cl.Step(
            name=f"Reading {platform_name(url)}",
            type="tool",
            icon="download",
            show_input=True,
            default_open=False,
        ) as step:
            step.input = url
            try:
                result = await run_cli("ingest-url", url, timeout=300)
                step.output = compact_output(result)
                results.append(result)
            except Exception as exc:
                step.output = f"Failed: {str(exc)[:300]}"
                results.append({"url": url, "error": str(exc)})
    return await analyse_with_hermes(text, history, results)


async def handle_research(text: str, history: list[dict]) -> str:
    query = re.sub(r"(?i)^\s*(research|find out about|look into|tell me about)\s*", "", text).strip()
    if not query:
        answer = await cl.AskUserMessage(
            content="What company, fund, deal, or market signal should I research?",
            timeout=120,
        ).send()
        query = (answer or {}).get("output", "").strip()
        if not query:
            return "Research paused. Send the topic when you are ready."
    async with cl.Step(name="Searching live sources", type="tool", icon="search", show_input=True, default_open=True) as step:
        step.input = query
        try:
            result = await run_cli("research", query, timeout=300)
            step.output = compact_output(result, 1400)
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:300]}"
    return await analyse_with_hermes(text or query, history, result)


async def handle_transcript(text: str, history: list[dict]) -> str:
    query = re.sub(r"(?i)^\s*(find|pull|search)\s+(the\s+)?(transcript|part|quote)?\s*", "", text).strip()
    if not query:
        answer = await cl.AskUserMessage(
            content="What quote, person, show, or topic should I locate in the transcripts?",
            timeout=120,
        ).send()
        query = (answer or {}).get("output", "").strip()
        if not query:
            return "Transcript search paused. Send the quote or topic when you have it."
    async with cl.Step(name="Searching stored transcripts", type="tool", icon="captions", show_input=True, default_open=True) as step:
        step.input = query
        try:
            result = await run_cli("find-transcript", query, timeout=360)
            step.output = compact_output(result, 1600)
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:300]}"
    return await analyse_with_hermes(text or query, history, result)


async def handle_save_topic(text: str) -> str:
    async with cl.Step(name="Saving to active edition", type="tool", icon="bookmark", show_input=True) as step:
        step.input = text
        try:
            result = await run_cli("save-topic", text)
            step.output = compact_output(result)
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:200]}"
    return format_tool_fallback(result)


async def handle_view_plan() -> str:
    async with cl.Step(name="Reading edition plan", type="tool", icon="list", default_open=True) as step:
        try:
            result = await run_cli("view-plan")
            step.output = compact_output(result, 2400)
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:200]}"
    return sanitise_response(format_plan(result))


def format_plan(result: dict) -> str:
    edition = result.get("edition") or {}
    topics = result.get("topics") or []
    number = edition.get("active_edition") or edition.get("edition_number") or "current"
    if not topics:
        return f"Edition {number} has no saved topics yet. Drop links or tell me what must be covered."
    lines = [f"Edition {number}. {len(topics)} saved topic{'s' if len(topics) != 1 else ''}:"]
    for topic in topics[:20]:
        if isinstance(topic, dict):
            label = topic.get("topic") or topic.get("title") or topic.get("content") or str(topic)
        else:
            label = str(topic)
        lines.append(f"- {label}")
    return "\n".join(lines)


async def get_smart_draft_topics() -> str:
    """Pull topics from edition_topics + recent content_items for draft review."""
    try:
        from db.client import get_client

        plan = {}
        try:
            plan = await run_cli("view-plan")
        except Exception:
            pass
        edition_data = plan.get("edition", {})
        edition = edition_data.get("active_edition", "current")
        dom_topics = plan.get("topics") or []
        dom_topics = [t for t in dom_topics if not t.get("used")]

        supabase = get_client()

        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        recent = (
            supabase.table("content_items")
            .select("title, source_name, raw_text, scraped_at")
            .in_("source_name", ["elenanisonoff", "TBPN", "All-In Podcast"])
            .gte("scraped_at", week_ago)
            .order("scraped_at", desc=True)
            .limit(6)
            .execute()
        )
        recent_items = recent.data or []

        today = datetime.now().strftime("%A %d %B %Y")
        lines = [f"Edition {edition} — draft review", f"Today: {today}\n"]

        if dom_topics:
            lines.append(f"Your saved topics ({len(dom_topics)}):")
            for t in dom_topics:
                if isinstance(t, dict):
                    ttype = t.get("topic_type", "topic")
                    label = f"[{ttype.upper()}] " if ttype and ttype != "topic" else ""
                    lines.append(f"  {label}{t.get('topic', str(t))}")
                else:
                    lines.append(f"  {t}")
        else:
            lines.append("No topics saved by you yet for this edition.")

        if recent_items:
            lines.append(f"\nThis week from sources ({len(recent_items)} items):")
            for item in recent_items:
                title = item.get("title") or (item.get("raw_text") or "")[:80]
                source = item.get("source_name", "")
                scraped = (item.get("scraped_at") or "")[:10]
                lines.append(f"  [{source} {scraped}] {title}")
        else:
            lines.append("\nNo new source content this week yet.")

        return "\n".join(lines)

    except Exception as exc:
        return f"Could not load topics: {str(exc)[:120]}"


async def handle_draft() -> str:
    """
    Draft initiation — show topic plan and wait for explicit approval.
    NEVER generates content directly. That is on_confirm_draft's job.
    """
    async with cl.Step(name="Loading topic plan", type="tool", icon="list", default_open=True) as step:
        topics_text = await get_smart_draft_topics()
        step.output = topics_text[:600]

    actions = [
        cl.Action(name="confirm_draft", payload={}, label="Yes, draft it", icon="check"),
        cl.Action(name="continue_editing", payload={}, label="Add more topics first", icon="plus"),
    ]
    await cl.Message(content="Awaiting your approval.", actions=actions, author=AUTHOR).send()
    cl.user_session.set("awaiting_draft_approval", True)
    return f"{topics_text}\n\nI will not start generation until you approve this plan."


async def handle_status() -> str:
    async with cl.Step(name="Checking system status", type="tool", icon="activity", default_open=True) as step:
        try:
            result = await run_cli("status")
            step.output = compact_output(result, 2200)
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:200]}"
    return sanitise_response(format_tool_fallback(result))


async def handle_brief() -> str:
    async with cl.Step(name="Checking Elena TikTok", type="tool", icon="video", default_open=False) as step:
        step.output = "Source queued."
    async with cl.Step(name="Checking TBPN and All-In", type="tool", icon="youtube", default_open=False) as step:
        try:
            result = await run_cli("morning-brief", timeout=900)
            step.output = compact_output(result, 1800)
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:300]}"
    return sanitise_response(format_tool_fallback(result))


async def handle_linkedin(text: str, history: list[dict]) -> str:
    topic = re.sub(r"(?i)^\s*(linkedin|repurpose this|make this a linkedin post)\s*", "", text).strip()
    if not topic:
        answer = await cl.AskUserMessage(content="What should the LinkedIn post be about?", timeout=120).send()
        topic = (answer or {}).get("output", "").strip()
    if not topic:
        return "LinkedIn drafting paused. Send the source or angle when ready."
    async with cl.Step(name="Drafting LinkedIn post", type="llm", icon="share-2", show_input=True) as step:
        step.input = topic
        try:
            result = await run_cli("linkedin", topic, timeout=300)
            step.output = "LinkedIn draft generated."
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:300]}"
    return sanitise_response(format_tool_fallback(result))


async def handle_source_check(message: str, history: list[dict]) -> str:
    """
    When Dom asks about a specific source, query the DB and summarise.
    Never refuse with "I cannot access TikTok/YouTube".
    """
    msg_lower = message.lower()

    if any(x in msg_lower for x in ("elena", "elenanisonoff", "tiktok")):
        source_name = "Elena TikTok"
        source_id = "elenanisonoff"
    elif "tbpn" in msg_lower:
        source_name = "TBPN Podcast"
        source_id = "TBPN"
    else:
        source_name = "All-In Podcast"
        source_id = "All-In Podcast"

    today = datetime.now().strftime("%A %d %B %Y")

    async with cl.Step(
        name=f"Checking {source_name}",
        type="tool",
        icon="video",
        show_input=True,
        default_open=True,
    ) as step:
        step.input = f"Latest from {source_name} — {today}"
        try:
            from db.client import get_client

            supabase = get_client()
            week_ago = (datetime.now() - timedelta(days=7)).isoformat()
            recent = (
                supabase.table("content_items")
                .select("title, raw_text, published_at, scraped_at")
                .eq("source_name", source_id)
                .gte("scraped_at", week_ago)
                .order("scraped_at", desc=True)
                .limit(5)
                .execute()
            )
            items = recent.data or []

            if not items:
                step.output = "Nothing in DB for past 7 days. Triggering fresh scrape..."
                try:
                    await run_cli("morning-brief", timeout=900)
                    recent = (
                        supabase.table("content_items")
                        .select("title, raw_text, published_at, scraped_at")
                        .eq("source_name", source_id)
                        .gte("scraped_at", week_ago)
                        .order("scraped_at", desc=True)
                        .limit(5)
                        .execute()
                    )
                    items = recent.data or []
                except Exception:
                    pass

            if not items:
                step.output = "No recent items found even after scrape."
                return f"Nothing new from {source_name} in the past 7 days."

            step.output = f"Found {len(items)} recent item(s) from {source_name}."
            content_preview = "\n\n---\n\n".join(
                f"Date: {item.get('scraped_at','')[:10]}\n{(item.get('raw_text') or '')[:400]}"
                for item in items
            )
            summary = await run_hermes(
                f"Today is {today}. Summarise the most recent and relevant content from "
                f"{source_name} for a VC secondaries newsletter editor. "
                f"Be specific about dates and topics covered. "
                f"Identify the 2-3 most interesting angles for Dom's newsletter. "
                f"Content:\n{content_preview}"
            )
            return sanitise_response(summary)

        except Exception as exc:
            step.output = f"Error: {str(exc)[:100]}"
            return f"Could not check {source_name}: {str(exc)[:100]}"


async def handle_model_switcher(text: str, command: str | None) -> None:
    """Show the model selector with clickable action buttons."""
    current_key = cl.user_session.get("selected_model", "hermes")
    current = AVAILABLE_MODELS.get(current_key, AVAILABLE_MODELS["hermes"])

    actions = []
    for key, model in AVAILABLE_MODELS.items():
        is_current = key == current_key
        actions.append(
            cl.Action(
                name="switch_model",
                payload={"model_key": key},
                label=f"{'✓ ' if is_current else ''}{model['label']}",
                tooltip=model["description"],
            )
        )

    await cl.Message(
        content=f"Current model: **{current['label']}**\n\nSelect a model:",
        actions=actions,
        author=AUTHOR,
    ).send()


async def handle_file_uploads(message: cl.Message) -> None:
    for element in message.elements or []:
        path = getattr(element, "path", None)
        name = getattr(element, "name", "attachment")
        if not path:
            continue
        async with cl.Step(name=f"Reading file: {name}", type="tool", icon="file", default_open=False) as step:
            step.input = name
            step.output = f"Attachment received. {Path(path).stat().st_size:,} bytes ready for analysis."


# ── Main message handler ──────────────────────────────────────────────────────

@cl.on_message
async def on_message(message: cl.Message):
    history = cl.user_session.get("history") or [{"role": "system", "content": get_herald_system()}]
    # Refresh system prompt with current date on every turn
    if history and history[0].get("role") == "system":
        history[0]["content"] = get_herald_system()

    command = (message.command or "").lower() or None
    text = message.content.strip()

    # Model switcher — early return, no history entry needed
    if (
        command == "model"
        or text.lower().startswith("/model")
        or any(x in text.lower() for x in ("what model", "switch model", "change model", "which model", "what model are you"))
    ):
        await handle_model_switcher(text, command)
        return

    if not cl.user_session.get("cross_thread_loaded"):
        _current_user = cl.context.session.user
        _uid = _current_user.identifier if _current_user else ""
        cross_context = await get_cross_thread_context(text, _uid)
        if cross_context and history and history[0].get("role") == "system":
            history[0]["content"] = f"{get_herald_system()}\n\nPAST CONTEXT:\n{cross_context}"
            cl.user_session.set("history", history)
        cl.user_session.set("cross_thread_loaded", True)

    intent_key = classify_intent(text, command)
    display, icon, _ = INTENTS[intent_key]

    async with cl.Step(
        name=f"HERALD · {display}",
        type="tool",
        icon=icon,
        show_input=True,
        default_open=False,
    ) as step:
        step.input = text[:240] or f"/{command or intent_key}"
        step.output = intent_detail(intent_key, text, history)

    if message.elements:
        await handle_file_uploads(message)
        if not text:
            response = "I have the file. Tell me the claim, section, or question you want me to focus on."
            await stream_response(response)
            return

    try:
        if intent_key == "url_ingest":
            if not URL_RE.search(text):
                answer = await cl.AskUserMessage(content="Drop the URL you want me to ingest.", timeout=120).send()
                text = (answer or {}).get("output", "").strip()
            response = await handle_url(text, history) if URL_RE.search(text) else "Ingestion paused. Send the URL when ready."
        elif intent_key == "research":
            response = await handle_research(text, history)
        elif intent_key == "transcript":
            response = await handle_transcript(text, history)
        elif intent_key == "save_topic":
            response = await handle_save_topic(text)
        elif intent_key == "view_plan":
            response = await handle_view_plan()
        elif intent_key == "draft":
            response = await handle_draft()
        elif intent_key == "status":
            response = await handle_status()
        elif intent_key == "morning_brief":
            response = await handle_brief()
        elif intent_key == "linkedin":
            response = await handle_linkedin(text, history)
        elif intent_key in ("tiktok_check", "source_latest"):
            response = await handle_source_check(text, history)
        else:
            response = await analyse_with_hermes(text, history)
    except Exception as exc:
        response = f"That action failed before completion: {str(exc)[:260]}"

    await stream_response(response)
    history.extend([
        {"role": "user", "content": text or f"/{command or intent_key}"},
        {"role": "assistant", "content": response},
    ])
    cl.user_session.set("history", history[-30:])


# ── Action callbacks ──────────────────────────────────────────────────────────

@cl.action_callback("switch_model")
async def on_switch_model(action):
    model_key = action.payload.get("model_key", "hermes")
    model = AVAILABLE_MODELS.get(model_key)
    if not model:
        await cl.Message(content="Unknown model.", author=AUTHOR).send()
        return

    cl.user_session.set("selected_model", model_key)
    await cl.Message(
        content=f"Switched to {model['label']}. {model['description']}.",
        author=AUTHOR,
    ).send()
    await action.remove()


@cl.action_callback("confirm_draft")
async def on_confirm_draft(action):
    import sys as _sys
    for _p in [str(ROOT / "tools"), str(ROOT), "/root/herald-v2/tools", "/root/herald-v2", "/root/herald"]:
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    async with cl.Step(name="Starting approved newsletter pipeline", type="tool", icon="play", default_open=True) as step:
        step.input = "Topic plan approved by Dom"
        try:
            from intelligence.tools import draft_full_weekly_newsletter

            result = await draft_full_weekly_newsletter("Dom approved the Chainlit edition plan")
            step.output = compact_output(result)
            html_content = result.get("html") or result.get("html_content") or ""
            response = result.get("note") or "Newsletter generation complete."
        except Exception as exc:
            step.output = f"Failed: {str(exc)[:300]}"
            result = {}
            html_content = ""
            response = f"The draft pipeline could not start: {str(exc)[:220]}"

    await stream_response(response)
    cl.user_session.set("awaiting_draft_approval", False)
    await action.remove()

    if html_content:
        await show_html_preview(html_content, "Newsletter Draft")


@cl.action_callback("continue_editing")
async def on_continue_editing(action):
    await cl.Message(
        content="Add the missing links or topics. I will update the plan, then ask again before drafting.",
        author=AUTHOR,
    ).send()
    cl.user_session.set("awaiting_draft_approval", False)
    await action.remove()


@cl.action_callback("export_html")
async def on_export_html(action):
    path = action.payload.get("html_path", "")
    filename = action.payload.get("filename", "herald_newsletter.html")
    if path and Path(path).exists():
        await cl.Message(
            content="HTML file ready:",
            elements=[cl.File(name=filename, path=path, display="inline")],
            author=AUTHOR,
        ).send()
    else:
        await cl.Message(content="HTML file not found — the preview may have expired.", author=AUTHOR).send()
    await action.remove()


@cl.action_callback("edit_newsletter_content")
async def on_edit_newsletter(action):
    await cl.Message(
        content=(
            "What would you like to change? Tell me in plain text.\n\n"
            "Examples: 'Change the headline to...' / 'Remove the section about...' / 'Make the tone more concise'"
        ),
        author=AUTHOR,
    ).send()
    await action.remove()


@cl.action_callback("download_html")
async def on_download(action):
    async with cl.Step(name="Preparing HTML file", type="tool", icon="download") as step:
        try:
            result = await run_cli("download-html")
            step.output = compact_output(result)
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:200]}"
    if result.get("found") and Path(result.get("filename", "")).exists():
        await cl.Message(
            content=f"HTML ready: {result.get('subject', '')}",
            elements=[cl.File(name=Path(result["filename"]).name, path=result["filename"], display="inline")],
            author=AUTHOR,
        ).send()
    else:
        await cl.Message(content=result.get("reason", "No HTML draft found."), author=AUTHOR).send()
    await action.remove()


@cl.action_callback("approve_newsletter")
async def on_approve(action):
    async with cl.Step(name="Publishing to Beehiiv", type="tool", icon="send", default_open=True) as step:
        try:
            result = await run_cli("publish-latest")
            step.output = compact_output(result)
        except Exception as exc:
            result = {"error": str(exc)}
            step.output = f"Failed: {str(exc)[:200]}"
    text = "Published to Beehiiv." if result.get("success") else f"Publish failed: {result.get('error') or result.get('note')}"
    await cl.Message(content=sanitise_response(text), author=AUTHOR).send()
    await action.remove()


@cl.action_callback("request_edits")
async def on_edits(action):
    await cl.Message(content="What needs changing? I will keep the rest intact.", author=AUTHOR).send()
    await action.remove()


@cl.action_callback("decline_newsletter")
async def on_decline(action):
    await cl.Message(content="Draft declined. Tell me what needs to change.", author=AUTHOR).send()
    await action.remove()

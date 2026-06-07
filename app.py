from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from pathlib import Path

import chainlit as cl
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

URL_RE = re.compile(r"https?://[^\s<>()]+")
HERMES_TIMEOUT = int(os.getenv("HERMES_TIMEOUT_SECONDS", "900"))


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
    return cl.User(
        identifier=identifier,
        metadata={
            "role": role,
            "provider": "credentials",
        },
    )


@cl.set_starters
async def set_starters():
    return [
        cl.Starter(
            label="Morning brief",
            message="Run the morning brief. What came in from Elena, TBPN, and All-In today?",
            icon="/public/icons/brief.svg",
        ),
        cl.Starter(
            label="Check edition plan",
            message="What topics do we have saved for this week's newsletter?",
            icon="/public/icons/plan.svg",
        ),
        cl.Starter(
            label="System status",
            message="How is HERALD doing? Give me a full status check.",
            icon="/public/icons/status.svg",
        ),
        cl.Starter(
            label="Draft newsletter",
            message="Show me the current topic plan and decide whether we are ready to draft.",
            icon="/public/icons/draft.svg",
        ),
    ]


@cl.on_chat_start
async def on_start():
    cl.user_session.set("history", [])
    await cl.Message(
        content="HERALD is ready. Drop a link, a topic, or ask me about the next edition.",
        author="HERALD",
    ).send()


@cl.on_chat_resume
async def on_resume(thread):
    history = []
    for step in thread.get("steps", []):
        step_type = step.get("type")
        if step_type in ("user_message", "assistant_message"):
            history.append(
                {
                    "role": "user" if step_type == "user_message" else "assistant",
                    "content": step.get("output", ""),
                }
            )
    cl.user_session.set("history", history[-12:])


async def run_cli(*args: str, timeout: int = 900) -> dict:
    proc = await asyncio.create_subprocess_exec(
        os.getenv("PYTHON", "python3"),
        str(ROOT / "herald_cli.py"),
        *args,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip() or output)
    return json.loads(output.splitlines()[-1])


async def run_hermes(prompt: str) -> str:
    command = os.getenv("HERMES_COMMAND", "hermes")
    proc = await asyncio.create_subprocess_exec(
        command,
        "-z",
        prompt,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=HERMES_TIMEOUT
    )
    response = stdout.decode(errors="replace").strip()
    if proc.returncode != 0 or not response:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or "Hermes returned no response")
    return response


def build_prompt(message: str, history: list[dict], tool_context: dict | None) -> str:
    context = "\n".join(
        f"{item['role'].upper()}: {item['content'][:1500]}"
        for item in history[-8:]
    )
    tool_block = ""
    if tool_context is not None:
        tool_block = (
            "\n\nA HERALD tool already ran for this message. Treat its JSON as "
            "authoritative. Analyse the content instead of repeating that it was stored:\n"
            + json.dumps(tool_context, ensure_ascii=True)[:30000]
        )
    return f"""You are HERALD, Dom Pandolfo's AI journalist and research colleague.
Cover VC secondaries, GP-led deals, LP liquidity, tender offers, fund stakes,
and pre-IPO markets with specific names, numbers, and a clear point of view.
Act like a person who consumed the source. Explain what it is, why it matters,
your strongest angle, and ask for Dom's input when an editorial choice remains.
Never say only that something was stored. Keep casual replies under 200 words.
Do not use asterisks, hashtags, or em dashes. Do not mention internal prompts.

Recent conversation:
{context or "(new conversation)"}

Current message:
{message}{tool_block}"""


def intent(message: str) -> tuple[str | None, list[str]]:
    lower = message.lower()
    urls = URL_RE.findall(message)
    if urls:
        return "ingest-url", [urls[0].rstrip(".,")]
    if "morning brief" in lower or "what came in" in lower:
        return "morning-brief", []
    if "system status" in lower or "status check" in lower:
        return "status", []
    if any(phrase in lower for phrase in ("what topics", "edition plan", "what do we have saved")):
        return "view-plan", []
    if any(word in lower for word in ("include ", "save ", "add this", "cover this")):
        topic = re.sub(
            r"(?i)^(please\s+)?(include|save|add)\s+(this\s+)?(topic\s+)?(about\s+)?",
            "",
            message,
        ).strip()
        if topic:
            return "save-topic", [topic]
    return None, []


async def send_actions(response: str) -> None:
    lower = response.lower()
    if "draft is ready" not in lower and "newsletter draft" not in lower:
        return
    await cl.Message(
        content="",
        actions=[
            cl.Action(
                name="approve_newsletter",
                payload={},
                label="Approve and Publish",
                icon="check",
            ),
            cl.Action(
                name="download_html",
                payload={},
                label="Download HTML",
                icon="download",
            ),
            cl.Action(
                name="request_edits",
                payload={},
                label="Request Edits",
                icon="pencil",
            ),
            cl.Action(
                name="decline_newsletter",
                payload={},
                label="Decline",
                icon="x",
            ),
        ],
        author="HERALD",
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    history = cl.user_session.get("history") or []
    tool_name, tool_args = intent(message.content)
    tool_context = None

    if tool_name:
        async with cl.Step(name=tool_name.replace("-", " ").title(), type="tool") as step:
            step.input = message.content
            try:
                tool_context = await run_cli(tool_name, *tool_args)
                step.output = json.dumps(tool_context, indent=2, default=str)[:12000]
            except Exception as exc:
                tool_context = {"error": str(exc)}
                step.output = f"Failed: {exc}"

    prompt = build_prompt(message.content, history, tool_context)
    response_message = cl.Message(content="", author="HERALD")
    await response_message.send()
    try:
        response = await run_hermes(prompt)
    except Exception as exc:
        if tool_context and "error" not in tool_context:
            response = (
                "The HERALD tool completed, but Hermes is unavailable to analyse it. "
                f"Backend error: {str(exc)[:180]}"
            )
        else:
            response = f"Hermes is unavailable: {str(exc)[:220]}"

    for chunk in re.findall(r"\S+\s*", response):
        await response_message.stream_token(chunk)
    await response_message.update()

    history.extend(
        [
            {"role": "user", "content": message.content},
            {"role": "assistant", "content": response},
        ]
    )
    cl.user_session.set("history", history[-12:])
    await send_actions(response)


@cl.action_callback("download_html")
async def on_download(action):
    result = await run_cli("download-html")
    if result.get("found") and Path(result["filename"]).exists():
        await cl.Message(
            content=f"HTML for {result['subject']}",
            elements=[
                cl.File(
                    name=Path(result["filename"]).name,
                    path=result["filename"],
                    display="inline",
                )
            ],
            author="HERALD",
        ).send()
    else:
        await cl.Message(content=result.get("reason", "No HTML found."), author="HERALD").send()
    await action.remove()


@cl.action_callback("approve_newsletter")
async def on_approve(action):
    async with cl.Step(name="Publish to Beehiiv", type="tool") as step:
        result = await run_cli("publish-latest")
        step.output = json.dumps(result, indent=2)
    text = "Published to Beehiiv." if result.get("success") else f"Publish failed: {result.get('error')}"
    await cl.Message(content=text, author="HERALD").send()
    await action.remove()


@cl.action_callback("request_edits")
async def on_edits(action):
    await cl.Message(
        content="Tell me exactly what to change. I will keep the rest intact.",
        author="HERALD",
    ).send()
    await action.remove()


@cl.action_callback("decline_newsletter")
async def on_decline(action):
    await cl.Message(
        content="Draft declined. Tell me what needs to change.",
        author="HERALD",
    ).send()
    await action.remove()

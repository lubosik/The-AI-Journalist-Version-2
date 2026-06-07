from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
load_dotenv(ROOT / ".env")
os.environ.setdefault("HERALD_USE_LEGACY_AI", "false")


def emit(data) -> None:
    print(json.dumps(data, ensure_ascii=True, default=str))


def platform_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "spotify.com" in host:
        return "spotify"
    if "youtu.be" in host or "youtube.com" in host:
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    if "twitter.com" in host or host.endswith("x.com"):
        return "twitter"
    if "instagram.com" in host:
        return "instagram"
    return "web"


def latest_content(url: str) -> dict:
    from db.client import get_client

    db = get_client()
    result = (
        db.table("content_items")
        .select("id,source_type,source_name,source_url,title,raw_text,published_at,metadata")
        .eq("source_url", url)
        .order("scraped_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data and "youtube.com/watch" in url:
        video_id = urlparse(url).query.split("v=", 1)[-1].split("&", 1)[0]
        result = (
            db.table("content_items")
            .select("id,source_type,source_name,source_url,title,raw_text,published_at,metadata")
            .ilike("source_url", f"%{video_id}%")
            .order("scraped_at", desc=True)
            .limit(1)
            .execute()
        )
    return result.data[0] if result.data else {}


async def store_web(url: str) -> dict:
    from db.queries import content_exists_by_url, insert_content_item
    from processing.dedup import generate_content_hash

    if content_exists_by_url(url):
        return {"stored": False, "reason": "Already in database"}
    async with httpx.AsyncClient(
        timeout=45,
        follow_redirects=True,
        headers={"User-Agent": "HERALD/2.0"},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    raw_text = "\n".join(
        line for line in (s.strip() for s in soup.get_text("\n").splitlines()) if line
    )
    if len(raw_text) < 100:
        return {"stored": False, "reason": "Page did not contain enough readable text"}
    content_id = insert_content_item(
        {
            "source_type": "rss",
            "source_name": urlparse(url).netloc,
            "source_url": url,
            "title": title[:500],
            "raw_text": raw_text,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "language": "en",
            "is_voice_sample": False,
            "is_deal_signal": False,
            "topics": [],
            "metadata": {
                "content_hash": generate_content_hash(raw_text),
                "manual_add": True,
                "ingestion_method": "web",
            },
        }
    )
    return {"stored": True, "content_id": content_id, "title": title}


async def store_tiktok(url: str) -> dict:
    from config import APIFY_ACTORS
    from db.queries import content_exists_by_url, insert_content_item
    from ingestion.apify_runner import run_actor
    from processing.dedup import generate_content_hash

    if content_exists_by_url(url):
        return {"stored": False, "reason": "Already in database"}
    items = await run_actor(
        APIFY_ACTORS["tiktok_transcript_v2"],
        {"startUrls": [url], "url": url, "tiktokUrl": url},
        timeout_secs=240,
    )
    if not items:
        return {"stored": False, "reason": "No TikTok data returned"}
    item = items[0]
    text = str(
        item.get("transcript")
        or item.get("text")
        or item.get("description")
        or item.get("desc")
        or ""
    ).strip()
    if not text:
        return {"stored": False, "reason": "TikTok returned no transcript"}
    content_id = insert_content_item(
        {
            "source_type": "tiktok",
            "source_name": item.get("author") or item.get("authorMeta", {}).get("name") or "manual",
            "source_url": url,
            "title": text[:200],
            "raw_text": text,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "language": "en",
            "is_voice_sample": False,
            "is_deal_signal": False,
            "topics": [],
            "metadata": {
                "content_hash": generate_content_hash(text),
                "manual_add": True,
            },
        }
    )
    return {"stored": True, "content_id": content_id, "title": text[:100]}


async def ingest_url(url: str) -> dict:
    platform = platform_for(url)
    if platform == "youtube":
        from ingestion.youtube import normalise_youtube_url

        url = normalise_youtube_url(url)
    existing = latest_content(url)
    if existing:
        return {
            "platform": platform,
            "result": {"stored": False, "reason": "Already in database"},
            "content": {
                "id": existing.get("id"),
                "title": existing.get("title"),
                "source_name": existing.get("source_name"),
                "published_at": existing.get("published_at"),
                "raw_text": (existing.get("raw_text") or "")[:24000],
            },
        }
    if platform == "spotify":
        from ingestion.podcast import ingest_spotify_url

        result = await ingest_spotify_url(url)
    elif platform == "youtube":
        from ingestion.youtube import ingest_single_youtube_video

        result = await ingest_single_youtube_video(url)
    elif platform == "tiktok":
        result = await store_tiktok(url)
    elif platform == "twitter":
        from ingestion.twitter import ingest_twitter_url

        result = await ingest_twitter_url(url)
    elif platform == "instagram":
        from ingestion.instagram import ingest_instagram_url

        result = await ingest_instagram_url(url)
    else:
        result = await store_web(url)

    item = latest_content(url)
    return {
        "platform": platform,
        "result": result,
        "content": {
            "id": item.get("id"),
            "title": item.get("title"),
            "source_name": item.get("source_name"),
            "published_at": item.get("published_at"),
            "raw_text": (item.get("raw_text") or "")[:24000],
        },
    }


async def status() -> dict:
    from db.client import get_client
    from scheduler.edition_manager import get_current_edition_state

    db = get_client()
    state = await get_current_edition_state()
    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    def count(table: str, query=None) -> int:
        builder = db.table(table).select("id", count="exact")
        if query:
            builder = query(builder)
        return builder.execute().count or 0

    return {
        "edition": state,
        "database": {
            "content_items": count("content_items"),
            "new_last_7_days": count(
                "content_items", lambda q: q.gte("scraped_at", week_start)
            ),
            "published_editions": count("published_issues"),
            "voice_hooks": count("hook_library"),
            "active_preferences": count(
                "dom_profile", lambda q: q.eq("is_active", True)
            ),
        },
    }


async def view_plan() -> dict:
    from scheduler.edition_manager import get_current_edition_state
    from tracking.topic_store import get_all_topics_for_edition

    state = await get_current_edition_state()
    topics = get_all_topics_for_edition(state["active_edition"])
    return {"edition": state, "topics": topics}


async def save_topic(args) -> dict:
    from scheduler.edition_manager import get_current_edition_state
    from tracking.topic_store import save_topic as save

    state = await get_current_edition_state()
    edition = state["active_edition"] + args.edition_offset
    return await save(
        topic=args.topic,
        topic_type=args.topic_type,
        edition_number=edition,
    )


async def morning_brief() -> dict:
    from ingestion.tiktok import ingest_tiktok_profile
    from ingestion.youtube import ingest_youtube_channel

    results = {}
    results["Elena TikTok"] = await ingest_tiktok_profile(
        "elenanisonoff", is_voice_sample=True, results_limit=30
    )
    results["TBPN"] = await ingest_youtube_channel(
        {"name": "TBPN", "handle": "@TBPNLive", "url": "https://www.youtube.com/@TBPNLive"},
        max_videos=3,
    )
    results["All-In"] = await ingest_youtube_channel(
        {
            "name": "All-In Podcast",
            "handle": "@allinpodcast",
            "url": "https://www.youtube.com/@allinpodcast",
        },
        max_videos=3,
    )
    return {"sources": results, "new_items": sum(results.values())}


async def download_html() -> dict:
    from db.queries import get_latest_newsletter_issue

    issue = get_latest_newsletter_issue()
    if not issue or not issue.get("html_content"):
        return {"found": False, "reason": "No newsletter draft with HTML found"}
    path = Path("/tmp") / f"herald_edition_{issue['issue_number']}.html"
    path.write_text(issue["html_content"], encoding="utf-8")
    return {
        "found": True,
        "filename": str(path),
        "subject": issue.get("subject_line") or "HERALD Newsletter",
        "status": issue.get("status"),
        "issue_id": issue.get("id"),
    }


async def publish_latest() -> dict:
    from db.queries import get_latest_newsletter_issue, update_newsletter_issue
    from newsletter.beehiiv import publish_post

    issue = get_latest_newsletter_issue()
    if not issue:
        return {"success": False, "error": "No newsletter issue found"}
    post_id = issue.get("beehiiv_post_id")
    if not post_id:
        return {"success": False, "error": "Latest issue has no Beehiiv draft ID"}
    result = await publish_post(post_id)
    if result.get("success"):
        update_newsletter_issue(issue["id"], {"status": "scheduled"})
    return result


async def draft_context() -> dict:
    from db.client import get_client
    from db.queries import get_next_issue_number, get_recent_content_items
    from memory.dom_profile import get_all_active_preferences_summary
    from scheduler.edition_manager import get_current_edition_state
    from tracking.topic_store import get_all_topics_for_edition

    state = await get_current_edition_state()
    topics = get_all_topics_for_edition(state["active_edition"])
    content = get_recent_content_items(days=7, limit=80, fresh_only=False)
    preferences = await get_all_active_preferences_summary()
    hooks = get_client().table("hook_library").select("*").limit(100).execute()
    hook_rows = hooks.data or []
    hook_rows.sort(
        key=lambda row: float(
            row.get("performance_score")
            or row.get("engagement_score")
            or row.get("score")
            or 0
        ),
        reverse=True,
    )
    return {
        "issue_number": get_next_issue_number(),
        "edition": state,
        "topics": topics,
        "preferences": preferences,
        "voice_hooks": hook_rows[:20],
        "content": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "source_name": item.get("source_name"),
                "source_url": item.get("source_url"),
                "published_at": item.get("published_at"),
                "raw_text": (item.get("raw_text") or "")[:8000],
            }
            for item in content
        ],
    }


async def save_draft(path: str, push_to_beehiiv: bool) -> dict:
    from db.queries import insert_newsletter_issue, update_newsletter_issue
    from newsletter.beehiiv import push_to_beehiiv_draft
    from newsletter.builder import build_newsletter_html, build_plain_text
    from tracking.topic_store import mark_topics_used

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = ("issue_number", "subject_line", "preview_text", "sections")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError(f"Draft JSON missing: {', '.join(missing)}")

    issue_number = int(payload["issue_number"])
    sections = payload["sections"]
    week_start = date.today() - timedelta(days=date.today().weekday())
    html = await build_newsletter_html(
        sections=sections,
        visuals=payload.get("visuals", []),
        issue_number=issue_number,
        subject_line=payload["subject_line"],
        week_start=week_start,
    )
    issue_id = insert_newsletter_issue(
        {
            "issue_number": issue_number,
            "week_start": week_start.isoformat(),
            "week_end": (week_start + timedelta(days=6)).isoformat(),
            "subject_line": payload["subject_line"],
            "preview_text": payload["preview_text"],
            "html_content": html,
            "plain_text": build_plain_text(sections),
            "sections": sections,
            "visuals": payload.get("visuals", []),
            "sources_used": payload.get("sources", []),
            "status": "draft",
        }
    )
    beehiiv = {"success": False, "error": "Beehiiv push not requested"}
    if push_to_beehiiv:
        beehiiv = await push_to_beehiiv_draft(
            html,
            payload["subject_line"],
            payload["preview_text"],
            issue_number,
        )
        if beehiiv.get("success"):
            update_newsletter_issue(
                issue_id,
                {
                    "beehiiv_post_id": beehiiv.get("post_id"),
                    "beehiiv_url": beehiiv.get("web_url") or beehiiv.get("url"),
                },
            )
    mark_topics_used(int(payload.get("edition_number", issue_number)))
    return {
        "success": True,
        "issue_id": issue_id,
        "issue_number": issue_number,
        "html_bytes": len(html.encode("utf-8")),
        "beehiiv": beehiiv,
    }


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser()
    commands = cli.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest-url")
    ingest.add_argument("url")
    commands.add_parser("status")
    commands.add_parser("view-plan")
    save = commands.add_parser("save-topic")
    save.add_argument("topic")
    save.add_argument("--topic-type", default="topic")
    save.add_argument("--edition-offset", type=int, default=0)
    commands.add_parser("morning-brief")
    commands.add_parser("download-html")
    commands.add_parser("publish-latest")
    commands.add_parser("draft-context")
    draft = commands.add_parser("save-draft")
    draft.add_argument("path")
    draft.add_argument("--push-to-beehiiv", action="store_true")
    return cli


async def main() -> None:
    args = parser().parse_args()
    handlers = {
        "ingest-url": lambda: ingest_url(args.url),
        "status": status,
        "view-plan": view_plan,
        "save-topic": lambda: save_topic(args),
        "morning-brief": morning_brief,
        "download-html": download_html,
        "publish-latest": publish_latest,
        "draft-context": draft_context,
        "save-draft": lambda: save_draft(args.path, args.push_to_beehiiv),
    }
    try:
        emit(await handlers[args.command]())
    except Exception as exc:
        emit({"error": type(exc).__name__, "message": str(exc)})
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

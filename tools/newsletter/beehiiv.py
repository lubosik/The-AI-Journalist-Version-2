"""
Beehiiv API integration for HERALD.

All network operations are async (httpx).

Safety contract:
  - push_to_beehiiv_draft always creates a DRAFT — never auto-publishes.
  - publish_post is only called by the Telegram bot after Dom sends /approve.
  - If credentials are missing, operations degrade gracefully and return
    {"success": False, "error": "..."} rather than raising.

Required env vars:
  BEEHIIV_API_KEY          — Bearer token from Beehiiv dashboard
  BEEHIIV_PUBLICATION_ID   — Publication ID (pub_xxxxxxxx)
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BEEHIIV_API_BASE = "https://api.beehiiv.com/v2"

_REQUEST_TIMEOUT = 30.0
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0

# Newsletter publishes Sunday 9am ET
_PUBLISH_WEEKDAY_ET = 6  # Sunday
_PUBLISH_HOUR_ET = 9
_PUBLISH_MINUTE_ET = 0


def _approval_send_at_unix() -> int | None:
    """
    Return a Unix timestamp for when to send after Dom approves.

    - If it's before Sunday 9am ET: schedule for this Sunday 9am ET.
    - If it's Sunday 9am ET or later (approved late): return None — publish immediately.
    """
    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")
    now = datetime.now(tz=et)
    days_ahead = (_PUBLISH_WEEKDAY_ET - now.weekday()) % 7
    if days_ahead == 0:
        # It is publish day
        if now.hour >= _PUBLISH_HOUR_ET:
            return None  # past the Sunday window — publish immediately
        # Before 9am on Sunday — still schedule for 9am today
    publish_day = now + timedelta(days=days_ahead)
    publish_dt = publish_day.replace(
        hour=_PUBLISH_HOUR_ET, minute=_PUBLISH_MINUTE_ET, second=0, microsecond=0
    )
    return int(publish_dt.timestamp())


def _next_friday_9am_et_unix() -> int:
    """Return Unix timestamp for next Sunday at 9:00am Eastern Time."""
    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")
    now = datetime.now(tz=et)
    days_ahead = (_PUBLISH_WEEKDAY_ET - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= _PUBLISH_HOUR_ET:
        days_ahead = 7  # already past 9am Sunday, use next week
    publish_day = now + timedelta(days=days_ahead)
    publish_dt = publish_day.replace(
        hour=_PUBLISH_HOUR_ET, minute=_PUBLISH_MINUTE_ET, second=0, microsecond=0
    )
    return int(publish_dt.timestamp())


def get_beehiiv_credentials() -> tuple[str, str]:
    api_key = os.getenv("BEEHIIV_API_KEY", "")
    pub_id = os.getenv("BEEHIIV_PUBLICATION_ID", "")
    return api_key, pub_id


def _make_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def push_to_beehiiv_draft(
    html_content: str,
    subject_line: str,
    preview_text: str,
    issue_number: int = 0,
) -> dict:
    """Push a newsletter as a DRAFT to Beehiiv using the Create Post API.

    Returns:
        Dict with: post_id, url, web_url, success, error
    """
    api_key, pub_id = get_beehiiv_credentials()

    if not api_key or not pub_id:
        logger.warning("push_to_beehiiv_draft: Beehiiv credentials not configured — skipping.")
        return {"post_id": "", "url": "", "web_url": "", "success": False, "error": "Beehiiv not configured"}

    url = f"{BEEHIIV_API_BASE}/publications/{pub_id}/posts"

    title = subject_line or f"HERALD Weekly Brief #{issue_number}"
    friday_ts = _next_friday_9am_et_unix()
    logger.info("push_to_beehiiv_draft: scheduling for Sunday 9am ET (unix=%d)", friday_ts)
    payload = {
        "title": title,
        "subtitle": preview_text or "",
        "body_content": html_content,
        "status": "draft",
        "send_at": friday_ts,
        "email_settings": {
            "email_subject_line": subject_line,
            "email_preview_text": preview_text,
            "display_title_in_email": False,
            "display_subtitle_in_email": False,
        },
        "recipients": {
            "web": {"tier_ids": ["all"]},
            "email": {"tier_ids": ["all"]},
        },
    }

    last_error: str = ""

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                logger.info("push_to_beehiiv_draft: attempt %d/%d — POST %s", attempt, _MAX_RETRIES, url)
                response = await client.post(url, headers=_make_headers(api_key), json=payload)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", _RETRY_BACKOFF_BASE * attempt))
                    logger.warning("push_to_beehiiv_draft: rate limited. Waiting %ds.", retry_after)
                    last_error = f"Rate limited (429). Retry-After: {retry_after}s"
                    await asyncio.sleep(retry_after)
                    continue

                if response.status_code >= 500:
                    last_error = f"Beehiiv server error {response.status_code}: {response.text[:200]}"
                    logger.warning("push_to_beehiiv_draft: server error %d on attempt %d.", response.status_code, attempt)
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)
                    continue

                if response.status_code >= 400:
                    last_error = f"Beehiiv client error {response.status_code}: {response.text[:500]}"
                    logger.error("push_to_beehiiv_draft: %s", last_error)
                    return {"post_id": "", "url": "", "web_url": "", "success": False, "error": last_error}

                try:
                    data = response.json()
                except Exception as parse_err:
                    last_error = f"Failed to parse Beehiiv response: {parse_err}"
                    logger.error("push_to_beehiiv_draft: %s — body: %s", last_error, response.text[:200])
                    return {"post_id": "", "url": "", "web_url": "", "success": False, "error": last_error}

                post_data = data.get("data", data)
                post_id: str = post_data.get("id", "")
                web_url: str = post_data.get("web_url", "")

                if not post_id:
                    logger.warning("push_to_beehiiv_draft: response had no post id. Full: %s", data)

                logger.info("push_to_beehiiv_draft: draft created. post_id=%s", post_id)
                return {"post_id": post_id, "url": web_url, "web_url": web_url, "success": True, "error": ""}

            except httpx.TimeoutException as exc:
                last_error = f"Request timed out on attempt {attempt}: {exc}"
                logger.warning("push_to_beehiiv_draft: %s", last_error)
                await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)

            except httpx.RequestError as exc:
                last_error = f"Network error on attempt {attempt}: {exc}"
                logger.warning("push_to_beehiiv_draft: %s", last_error)
                await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)

    logger.error("push_to_beehiiv_draft: all %d attempts failed. Last: %s", _MAX_RETRIES, last_error)
    return {"post_id": "", "url": "", "web_url": "", "success": False, "error": last_error}


async def update_beehiiv_draft(
    post_id: str,
    html_content: str,
    subject_line: str,
    preview_text: str,
) -> dict:
    """Update an existing Beehiiv draft with new content.

    Returns:
        Dict with: success, web_url, error
    """
    if not post_id:
        return {"success": False, "web_url": "", "error": "post_id is required"}

    api_key, pub_id = get_beehiiv_credentials()
    if not api_key or not pub_id:
        return {"success": False, "web_url": "", "error": "Beehiiv not configured"}

    url = f"{BEEHIIV_API_BASE}/publications/{pub_id}/posts/{post_id}"
    payload = {
        "title": subject_line,
        "subtitle": preview_text or "",
        "body_content": html_content,
        "email_settings": {
            "email_subject_line": subject_line,
            "email_preview_text": preview_text,
            "display_title_in_email": False,
            "display_subtitle_in_email": False,
        },
    }

    last_error: str = ""

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                logger.info("update_beehiiv_draft: attempt %d/%d — PATCH %s", attempt, _MAX_RETRIES, url)
                response = await client.patch(url, headers=_make_headers(api_key), json=payload)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", _RETRY_BACKOFF_BASE * attempt))
                    last_error = f"Rate limited (429). Retry-After: {retry_after}s"
                    await asyncio.sleep(retry_after)
                    continue

                if response.status_code >= 500:
                    last_error = f"Beehiiv server error {response.status_code}: {response.text[:200]}"
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)
                    continue

                if response.status_code >= 400:
                    last_error = f"Beehiiv client error {response.status_code}: {response.text[:500]}"
                    logger.error("update_beehiiv_draft: %s", last_error)
                    return {"success": False, "web_url": "", "error": last_error}

                try:
                    data = response.json()
                except Exception as parse_err:
                    last_error = f"Failed to parse Beehiiv response: {parse_err}"
                    return {"success": False, "web_url": "", "error": last_error}

                post_data = data.get("data", data)
                web_url: str = post_data.get("web_url", "")
                logger.info("update_beehiiv_draft: post %s updated", post_id)
                return {"success": True, "web_url": web_url, "error": ""}

            except httpx.TimeoutException as exc:
                last_error = f"Request timed out on attempt {attempt}: {exc}"
                await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)
            except httpx.RequestError as exc:
                last_error = f"Network error on attempt {attempt}: {exc}"
                await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)

    logger.error("update_beehiiv_draft: all %d attempts failed. Last: %s", _MAX_RETRIES, last_error)
    return {"success": False, "web_url": "", "error": last_error}


async def publish_post(post_id: str) -> dict:
    """
    Confirm the draft for sending. Called ONLY when Dom sends /approve.

    Timing logic:
    - Before Sunday 9am ET: schedules for this Sunday 9am ET (send_at = Sunday 9am).
    - Sunday 9am or later, or any other day past the window: publishes immediately
      (no send_at — Beehiiv sends as soon as it's confirmed).

    This means a late Sunday approval publishes immediately, not the following Sunday.

    Returns:
        Dict with: success, url, error
    """
    if not post_id:
        return {"success": False, "url": "", "error": "post_id is required"}

    api_key, pub_id = get_beehiiv_credentials()
    if not api_key or not pub_id:
        return {"success": False, "url": "", "error": "Beehiiv not configured"}

    url = f"{BEEHIIV_API_BASE}/publications/{pub_id}/posts/{post_id}"
    send_at_ts = _approval_send_at_unix()

    if send_at_ts is not None:
        logger.info("publish_post: confirming post %s — scheduled for Sunday 9am ET (unix=%d)", post_id, send_at_ts)
        payload = {"status": "confirmed", "send_at": send_at_ts}
    else:
        logger.info("publish_post: confirming post %s — publishing immediately (past Sunday window)", post_id)
        payload = {"status": "confirmed"}

    last_error: str = ""

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                logger.info("publish_post: attempt %d/%d — PATCH %s", attempt, _MAX_RETRIES, url)
                response = await client.patch(url, headers=_make_headers(api_key), json=payload)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", _RETRY_BACKOFF_BASE * attempt))
                    last_error = f"Rate limited (429). Retry-After: {retry_after}s"
                    await asyncio.sleep(retry_after)
                    continue

                if response.status_code >= 500:
                    last_error = f"Beehiiv server error {response.status_code}: {response.text[:200]}"
                    await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)
                    continue

                if response.status_code >= 400:
                    last_error = f"Beehiiv client error {response.status_code}: {response.text[:500]}"
                    logger.error("publish_post: %s", last_error)
                    return {"success": False, "url": "", "error": last_error}

                try:
                    data = response.json()
                except Exception as parse_err:
                    last_error = f"Failed to parse Beehiiv response: {parse_err}"
                    return {"success": False, "url": "", "error": last_error}

                post_data = data.get("data", data)
                post_url: str = post_data.get("web_url", "")

                logger.info("publish_post: post %s published. url=%s", post_id, post_url)
                return {"success": True, "url": post_url, "error": ""}

            except httpx.TimeoutException as exc:
                last_error = f"Request timed out on attempt {attempt}: {exc}"
                await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)

            except httpx.RequestError as exc:
                last_error = f"Network error on attempt {attempt}: {exc}"
                await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)

    logger.error("publish_post: all %d attempts failed. Last: %s", _MAX_RETRIES, last_error)
    return {"success": False, "url": "", "error": last_error}


async def get_post_stats(post_id: str) -> dict:
    """Fetch detailed stats for a single post (open rate, clicks, etc).

    Returns:
        Dict with: success, stats{open_rate, unique_opens, clicks, recipients, ...}, error
    """
    if not post_id:
        return {"success": False, "stats": {}, "error": "post_id required"}

    api_key, pub_id = get_beehiiv_credentials()
    if not api_key or not pub_id:
        return {"success": False, "stats": {}, "error": "Beehiiv not configured"}

    url = f"{BEEHIIV_API_BASE}/publications/{pub_id}/posts/{post_id}"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.get(
                url,
                headers=_make_headers(api_key),
                params={"expand[]": "stats"},
            )
            if response.status_code == 404:
                return {"success": False, "stats": {}, "error": f"Post {post_id} not found"}
            if response.status_code >= 400:
                return {"success": False, "stats": {}, "error": f"Beehiiv error {response.status_code}"}

            data = response.json().get("data", {})
            stats_obj = data.get("stats", {})
            email_stats = stats_obj.get("email", {})

            stats = {
                "title": data.get("title", ""),
                "subject_line": data.get("subject_line", ""),
                "status": data.get("status", ""),
                "publish_date": data.get("publish_date"),
                "recipients": email_stats.get("recipients", 0),
                "delivered": email_stats.get("delivered", 0),
                "opens": email_stats.get("opens", 0),
                "unique_opens": email_stats.get("unique_opens", 0),
                "open_rate": email_stats.get("open_rate", 0.0),
                "clicks": email_stats.get("clicks", 0),
                "unique_clicks": email_stats.get("unique_clicks", 0),
                "click_rate": email_stats.get("click_rate", 0.0),
                "unsubscribes": email_stats.get("unsubscribes", 0),
                "spam_reports": email_stats.get("spam_reports", 0),
                "web_views": stats_obj.get("web", {}).get("views", 0),
            }

            logger.info("get_post_stats: post=%s open_rate=%.1f%%", post_id, stats["open_rate"] * 100)
            return {"success": True, "stats": stats, "error": ""}

    except Exception as exc:
        return {"success": False, "stats": {}, "error": str(exc)}


async def get_recent_posts_performance(limit: int = 5) -> list[dict]:
    """Fetch the last N published posts with their performance stats.

    Returns a list of stat dicts sorted by publish_date descending.
    """
    api_key, pub_id = get_beehiiv_credentials()
    if not api_key or not pub_id:
        return []

    url = f"{BEEHIIV_API_BASE}/publications/{pub_id}/posts"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.get(
                url,
                headers=_make_headers(api_key),
                params={
                    "status": "confirmed",
                    "limit": limit,
                    "order_by": "publish_date",
                    "direction": "desc",
                    "expand[]": "stats",
                },
            )
            if response.status_code >= 400:
                logger.warning("get_recent_posts_performance: error %d", response.status_code)
                return []

            posts = response.json().get("data", [])
            results = []
            for post in posts:
                stats_obj = post.get("stats", {})
                email_stats = stats_obj.get("email", {})
                results.append({
                    "post_id": post.get("id", ""),
                    "title": post.get("title", ""),
                    "subject_line": post.get("subject_line", ""),
                    "publish_date": post.get("publish_date"),
                    "recipients": email_stats.get("recipients", 0),
                    "open_rate": email_stats.get("open_rate", 0.0),
                    "click_rate": email_stats.get("click_rate", 0.0),
                    "unique_opens": email_stats.get("unique_opens", 0),
                    "unique_clicks": email_stats.get("unique_clicks", 0),
                    "unsubscribes": email_stats.get("unsubscribes", 0),
                    "web_views": stats_obj.get("web", {}).get("views", 0),
                })

            logger.info("get_recent_posts_performance: fetched %d posts", len(results))
            return results

    except Exception as exc:
        logger.error("get_recent_posts_performance: %s", exc)
        return []


async def get_publication_overview() -> dict:
    """Fetch aggregate stats and subscriber count for the publication.

    Returns:
        Dict with: total_subscribers, recent_posts, aggregate_stats, error
    """
    api_key, pub_id = get_beehiiv_credentials()
    if not api_key or not pub_id:
        return {"success": False, "error": "Beehiiv not configured"}

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            # Get aggregate post stats
            agg_resp = await client.get(
                f"{BEEHIIV_API_BASE}/publications/{pub_id}/posts/aggregate_stats",
                headers=_make_headers(api_key),
            )
            agg_data = {}
            if agg_resp.status_code == 200:
                agg_data = agg_resp.json().get("data", {}).get("stats", {})

            # Get recent posts for trend analysis
            recent = await get_recent_posts_performance(limit=5)

        # Calculate trend from recent posts
        if recent:
            avg_open = sum(p["open_rate"] for p in recent) / len(recent)
            avg_click = sum(p["click_rate"] for p in recent) / len(recent)
        else:
            avg_open = avg_click = 0.0

        email_stats = agg_data.get("email", {})
        return {
            "success": True,
            "publication_id": pub_id,
            "aggregate": {
                "total_recipients": email_stats.get("recipients", 0),
                "total_opens": email_stats.get("opens", 0),
                "avg_open_rate": email_stats.get("open_rate", 0.0),
                "avg_click_rate": email_stats.get("click_rate", 0.0),
                "total_unsubscribes": email_stats.get("unsubscribes", 0),
            },
            "recent_avg_open_rate": avg_open,
            "recent_avg_click_rate": avg_click,
            "recent_posts": recent,
            "error": "",
        }

    except Exception as exc:
        logger.error("get_publication_overview: %s", exc)
        return {"success": False, "error": str(exc)}


async def get_post_status(post_id: str) -> dict:
    """Get the current status and URL of a Beehiiv post."""
    if not post_id:
        return {"success": False, "status": "", "url": "", "subject": "", "error": "post_id required"}

    api_key, pub_id = get_beehiiv_credentials()
    if not api_key or not pub_id:
        return {"success": False, "status": "", "url": "", "subject": "", "error": "Beehiiv not configured"}

    url = f"{BEEHIIV_API_BASE}/publications/{pub_id}/posts/{post_id}"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.get(url, headers=_make_headers(api_key))

            if response.status_code == 404:
                return {"success": False, "status": "", "url": "", "subject": "", "error": f"Post {post_id} not found"}

            if response.status_code >= 400:
                error_msg = f"Beehiiv error {response.status_code}: {response.text[:500]}"
                logger.error("get_post_status: %s", error_msg)
                return {"success": False, "status": "", "url": "", "subject": "", "error": error_msg}

            post_data = response.json().get("data", {})
            return {
                "success": True,
                "status": post_data.get("status", ""),
                "url": post_data.get("web_url", ""),
                "subject": post_data.get("subject_line", ""),
                "error": "",
            }

    except Exception as exc:
        error_msg = f"Request error: {exc}"
        logger.error("get_post_status: %s", error_msg)
        return {"success": False, "status": "", "url": "", "subject": "", "error": error_msg}

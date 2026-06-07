"""
ingestion/podcast.py

Spotify podcast ingestion waterfall:
1. Direct Spotify episode transcript actor
2. Spotify metadata actor, YouTube mirror discovery, YouTube transcript
3. Perplexity deep-research fallback
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from openai import OpenAI

from config import APIFY_ACTORS, MODELS, OPENROUTER_BASE_URL
from db.queries import content_exists_by_url, insert_content_item
from ingestion.apify_runner import run_actor
from ingestion.youtube import normalise_youtube_url
from intelligence.relevance import check_relevance
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks
from processing.tagger import generate_tags

load_dotenv()
logger = logging.getLogger(__name__)

# Actor IDs
YOUTUBE_SEARCH_ACTOR = "streamers/youtube-scraper"  # used for both search and transcript


def _extract_transcript(item: dict) -> str:
    transcript = (
        item.get("transcript")
        or item.get("text")
        or item.get("captions")
        or item.get("subtitles")
        or item.get("searchResult")
        or ""
    )
    if isinstance(transcript, list):
        return " ".join(
            str(part.get("text") or part.get("content") or "")
            for part in transcript
            if isinstance(part, dict)
        ).strip()
    if isinstance(transcript, dict):
        return str(transcript.get("text") or transcript.get("transcript") or "").strip()
    return str(transcript).strip()


def _extract_first_youtube_url(text: str) -> str:
    match = re.search(
        r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/)|youtu\.be/)"
        r"[A-Za-z0-9_-]+[^\s<>\"]*",
        text or "",
    )
    return normalise_youtube_url(match.group(0).rstrip(".,);]")) if match else ""


async def _call_openrouter(model: str, system: str, user: str) -> str:
    client = OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )
    response = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
    )
    return response.choices[0].message.content or ""


def extract_spotify_episode_id(url: str) -> str:
    match = re.search(r'episode/([A-Za-z0-9]+)', url)
    return match.group(1) if match else ""


async def get_spotify_metadata(spotify_url: str) -> tuple[str, str, str]:
    """
    Fetch Spotify page and extract episode title, show name, description via OG tags.
    Returns (episode_title, show_name, description).
    Spotify renders OG tags server-side so a plain GET works.
    """
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Twitterbot/1.0)",
                "Accept": "text/html",
            },
        ) as client:
            resp = await client.get(spotify_url)
            html = resp.text

        # og:title format: "Episode Title | Show Name | Podcast"
        title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        desc_match = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)

        og_title = title_match.group(1) if title_match else ""
        og_desc = desc_match.group(1) if desc_match else ""

        # Parse "Episode Title | Show Name | Podcast" or "Episode Title | Show Name"
        episode_title = ""
        show_name = ""
        if og_title:
            parts = [p.strip() for p in og_title.split("|")]
            if len(parts) >= 2:
                episode_title = parts[0]
                show_name = parts[1] if parts[1].lower() != "podcast" else parts[0]
            else:
                episode_title = og_title

        logger.info(f"[podcast] Spotify metadata: title='{episode_title}' show='{show_name}'")
        return episode_title, show_name, og_desc

    except Exception as e:
        logger.warning(f"[podcast] Could not fetch Spotify metadata: {e}")
        return "", "", ""


def _title_similarity(a: str, b: str) -> float:
    """Simple word overlap similarity between two strings."""
    if not a or not b:
        return 0.0
    a_words = set(re.sub(r'[^\w\s]', '', a.lower()).split())
    b_words = set(re.sub(r'[^\w\s]', '', b.lower()).split())
    stop = {"the", "a", "an", "in", "of", "to", "and", "or", "for", "is", "are",
            "with", "at", "by", "on", "from", "this", "that", "full", "episode",
            "podcast", "show", "video", "ft", "feat", "part"}
    a_words -= stop
    b_words -= stop
    if not a_words or not b_words:
        return 0.0
    overlap = len(a_words & b_words)
    return overlap / min(len(a_words), len(b_words))


def _parse_duration_seconds(duration_str: str) -> int:
    """Parse '1:23:45' or '45:12' into total seconds."""
    if not duration_str:
        return 0
    parts = duration_str.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        pass
    return 0


def _extract_subtitles_text(item: dict) -> str:
    """
    Pull subtitle/transcript text from a streamers/youtube-scraper result item.
    The actor returns subtitles as a list of objects:
      [{"type": "auto_generated", "language": "en", "plaintext": "full text..."}]
    Returns the first English plaintext block, or empty string.
    """
    subs = item.get("subtitles") or []
    if not subs or not isinstance(subs, list):
        return ""

    # Prefer English auto-generated or manual captions
    for entry in subs:
        if not isinstance(entry, dict):
            continue
        lang = (entry.get("language") or "").lower()
        if lang.startswith("en") or not lang:
            text = entry.get("plaintext") or entry.get("text") or entry.get("content") or ""
            if text and isinstance(text, str):
                return text.strip()

    # Fallback: return first available plaintext regardless of language
    for entry in subs:
        if isinstance(entry, dict):
            text = entry.get("plaintext") or entry.get("text") or ""
            if text:
                return text.strip()
    return ""


async def search_youtube_for_episode(
    episode_title: str, show_name: str
) -> tuple[str | None, str]:
    """
    Search YouTube for a podcast episode using streamers/youtube-scraper.
    Returns (best_match_url, inline_transcript_text).
    The transcript may already be populated if the actor returned subtitles.
    """
    query = f"{show_name} {episode_title}".strip()
    if not query:
        return None, ""

    logger.info(f"[podcast] Searching YouTube: '{query}'")

    _search_input = {
        "searchQueries": [query],
        "maxResults": 8,
        "maxResultsShorts": 0,
        "maxResultStreams": 0,
        "downloadSubtitles": True,
        "subtitlesFormat": "plaintext",
        "subtitlesLanguage": "en",
    }

    results = []
    try:
        results = await run_actor(YOUTUBE_SEARCH_ACTOR, _search_input, timeout_secs=240)
    except Exception as e:
        logger.warning(f"[podcast] YouTube search actor failed: {e}")

    if not results and show_name and episode_title:
        logger.info("[podcast] Retrying YouTube search with episode title only")
        try:
            results = await run_actor(
                YOUTUBE_SEARCH_ACTOR,
                {
                    "searchQueries": [episode_title],
                    "maxResults": 5,
                    "maxResultsShorts": 0,
                    "downloadSubtitles": True,
                    "subtitlesFormat": "plaintext",
                    "subtitlesLanguage": "en",
                },
                timeout_secs=180,
            )
        except Exception as e:
            logger.warning(f"[podcast] Retry search failed: {e}")

    if not results:
        return None, ""

    logger.info(f"[podcast] YouTube search returned {len(results)} results")

    best_url = None
    best_score = 0.0
    best_item = None

    for item in results:
        video_url = item.get("url") or ""
        video_title = item.get("title") or ""
        channel_name = item.get("channelName") or ""
        duration_str = item.get("duration") or ""
        duration_secs = _parse_duration_seconds(duration_str)

        if not video_url or "youtube.com/watch" not in video_url:
            continue

        title_score = _title_similarity(episode_title, video_title)
        channel_score = _title_similarity(show_name, channel_name) * 0.3
        duration_score = 0.2 if duration_secs > 1200 else 0.0
        total = title_score + channel_score + duration_score

        logger.info(
            f"[podcast] Candidate: '{video_title}' by '{channel_name}' ({duration_str}) — score={total:.2f}"
        )

        if total > best_score:
            best_score = total
            best_url = video_url
            best_item = item

    if best_score < 0.2:
        logger.warning(f"[podcast] Best YouTube match score too low ({best_score:.2f}) — no match")
        return None, ""

    logger.info(f"[podcast] Best YouTube match: {best_url} (score={best_score:.2f})")

    inline_transcript = _extract_subtitles_text(best_item) if best_item else ""
    if inline_transcript:
        logger.info(f"[podcast] Inline subtitles from search: {len(inline_transcript)} chars")

    return best_url, inline_transcript


async def get_youtube_transcript(youtube_url: str) -> str:
    """
    Fetch a transcript with the single-video transcript actor.
    Returns plaintext transcript or an empty string.
    """
    youtube_url = normalise_youtube_url(youtube_url)
    logger.info(f"[podcast] Getting YouTube transcript: {youtube_url}")
    try:
        result = await run_actor(
            APIFY_ACTORS["youtube_transcript_v2"],
            {"videoUrl": youtube_url},
            timeout_secs=180,
        )
        if not result or not isinstance(result, list):
            return ""

        transcript = _extract_transcript(result[0])
        if transcript:
            logger.info(f"[podcast] Transcript extracted: {len(transcript)} chars")
        return transcript

    except Exception as e:
        logger.warning(f"[podcast] Transcript fetch failed for {youtube_url}: {e}")
    return ""


async def store_podcast_content(
    transcript: str,
    title: str,
    source_url: str,
    method: str,
    metadata: dict,
) -> dict:
    """Deduplicate, chunk, embed, tag and store podcast content."""
    if content_exists_by_url(source_url):
        return {"stored": False, "method": method, "reason": "Already in database"}

    if is_duplicate(source_url, transcript):
        return {"stored": False, "method": method, "reason": "Duplicate content"}

    # Lower relevance threshold for manually submitted content
    relevance = await check_relevance(transcript[:2000])
    if relevance["score"] < 2:
        logger.info(f"[podcast] Storing despite low relevance ({relevance['score']}) — manually submitted")

    show_name = ""
    if isinstance(metadata.get("show"), dict):
        show_name = metadata["show"].get("name", "")
    elif isinstance(metadata.get("show_name"), str):
        show_name = metadata["show_name"]

    published_raw = metadata.get("releaseDate") or metadata.get("release_date")
    try:
        published_at = (
            datetime.fromisoformat(str(published_raw).replace("Z", "+00:00")).isoformat()
            if published_raw
            else datetime.now(timezone.utc).isoformat()
        )
    except Exception:
        published_at = datetime.now(timezone.utc).isoformat()

    content_hash = generate_content_hash(transcript)
    tags = await generate_tags(transcript)

    item = {
        "source_type": "podcast",
        "source_name": show_name or metadata.get("source_name", "Podcast"),
        "source_url": source_url,
        "title": title[:500] if title else "Podcast Episode",
        "raw_text": transcript,
        "published_at": published_at,
        "language": "en",
        "is_voice_sample": False,
        "is_deal_signal": tags.get("is_deal_signal", False),
        "topics": tags.get("topics", []),
        "metadata": {**metadata, "ingestion_method": method, "content_hash": content_hash},
    }

    content_id = insert_content_item(item)
    chunks = chunk_text(transcript)
    await embed_and_store_chunks(content_id, chunks)

    logger.info(f"[podcast] Stored: '{title[:60]}' method={method} chunks={len(chunks)}")
    return {
        "stored": True,
        "method": method,
        "title": title,
        "content_id": content_id,
        "chunks": len(chunks),
    }


async def ingest_spotify_url(spotify_url: str) -> dict:
    """
    Full waterfall ingestion for any Spotify episode URL.
    Strategy: direct Spotify transcript -> metadata/YouTube mirror -> deep research.
    Returns: {stored, method, title, content_id, chunks, reason}
    """
    clean_url = spotify_url.split("?")[0]

    logger.info(f"[podcast] Starting Spotify ingestion: {clean_url}")

    episode_title = ""
    show_name = ""
    description = ""
    metadata: dict = {}

    # Step 1: direct Spotify transcript.
    try:
        direct = await run_actor(
            APIFY_ACTORS["spotify_episodes"],
            {"episodeUrls": [clean_url], "fetchDetails": True},
            timeout_secs=300,
        )
        if direct:
            item = direct[0]
            transcript = _extract_transcript(item)
            episode_title = item.get("name") or item.get("title") or ""
            show = item.get("show") or {}
            show_name = show.get("name", "") if isinstance(show, dict) else str(show or "")
            if len(transcript) > 200:
                return await store_podcast_content(
                    transcript,
                    episode_title or "Podcast Episode",
                    clean_url,
                    "spotify_direct",
                    item,
                )
    except Exception as e:
        logger.warning(f"[podcast] Spotify direct transcript failed: {e}")

    # Step 2: metadata actor, then locate and transcribe a YouTube mirror.
    try:
        meta_result = await run_actor(
            APIFY_ACTORS["spotify_metadata"],
            {"episodeUrls": [clean_url]},
            timeout_secs=240,
        )
        if meta_result:
            metadata = meta_result[0]
            episode_title = metadata.get("name") or metadata.get("title") or episode_title
            show = metadata.get("show") or {}
            show_name = (
                show.get("name", "")
                if isinstance(show, dict)
                else str(show or metadata.get("show_name") or show_name)
            )
            description = metadata.get("description") or ""

        if not episode_title:
            page_title, page_show, page_description = await get_spotify_metadata(clean_url)
            episode_title = page_title or episode_title
            show_name = page_show or show_name
            description = page_description or description

        youtube_url = ""
        if episode_title or show_name:
            discovery = await _call_openrouter(
                MODELS["research"],
                "Find the matching YouTube video. Return only its URL.",
                f'Find the YouTube URL for this podcast episode: "{show_name}" "{episode_title}"',
            )
            youtube_url = _extract_first_youtube_url(discovery)

        inline_transcript = ""
        if not youtube_url and (episode_title or show_name):
            youtube_url, inline_transcript = await search_youtube_for_episode(
                episode_title,
                show_name,
            )
            youtube_url = normalise_youtube_url(youtube_url or "")

        transcript = inline_transcript if len(inline_transcript) > 200 else ""
        if youtube_url and not transcript:
            transcript = await get_youtube_transcript(youtube_url)

        if len(transcript) > 200:
            return await store_podcast_content(
                transcript,
                episode_title or "Podcast Episode",
                clean_url,
                "youtube_mirror",
                {
                    **metadata,
                    "show_name": show_name,
                    "youtube_url": youtube_url,
                    "description": description,
                },
            )
    except Exception as e:
        logger.warning(f"[podcast] Spotify metadata/YouTube mirror failed: {e}")

    # Step 3: deep research fallback.
    logger.info("[podcast] Falling back to Perplexity deep research")
    try:
        research = await _call_openrouter(
            MODELS["deep_research"],
            "Research podcast episodes. Return key insights, specific claims, data points, and notable quotes.",
            (
                f"What are the key insights from the {show_name or 'podcast'} episode "
                f'"{episode_title or clean_url}"? Include specific claims, data points, '
                "and notable quotes."
            ),
        )
        if len(research) > 200:
            return await store_podcast_content(
                research,
                episode_title or "Podcast Episode",
                clean_url,
                "perplexity_fallback",
                {
                    **metadata,
                    "show_name": show_name,
                    "description": description,
                    "ingestion_method": "perplexity_research",
                },
            )
    except Exception as e:
        logger.error(f"[podcast] Spotify all steps failed: {e}")
        return {
            "stored": False,
            "method": "all_failed",
            "reason": str(e),
            "title": episode_title,
        }

    return {
        "stored": False,
        "method": "all_failed",
        "reason": "All Spotify transcript, YouTube mirror, and research paths failed",
        "title": episode_title,
    }

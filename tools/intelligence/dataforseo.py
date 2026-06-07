"""
intelligence/dataforseo.py

Thin async client for DataForSEO. Two endpoints we actually use:
  - /v3/keywords_data/google_ads/search_volume/live  →  search volume + CPC for
    a list of seed keywords.
  - /v3/dataforseo_labs/google/related_keywords/live →  related search terms
    around a single seed (tells us what audiences are actually asking).

Both calls are POST + JSON, Basic auth header. Pre-encoded creds live in
DATAFORSEO_AUTH_B64; falls back to constructing from login/password.

Cost: search_volume ≈ $0.075 per request (up to 1000 keywords). related_keywords
billed per request as well. Cache for 6 hours so we don't repeatedly query the
same seeds during back-and-forth pitching.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dataforseo.com/v3"
DEFAULT_LOCATION = "United States"
DEFAULT_LANGUAGE = "English"
TIMEOUT_SEC = 45.0

_cache: dict[tuple, tuple[float, Any]] = {}
_CACHE_TTL = 6 * 60 * 60  # 6 hours


def _auth_header() -> str:
    """Return the Authorization header value, building it from creds if needed."""
    pre = (os.getenv("DATAFORSEO_AUTH_B64") or "").strip()
    if pre:
        return f"Basic {pre}"
    login = os.getenv("DATAFORSEO_LOGIN", "")
    password = os.getenv("DATAFORSEO_PASSWORD", "")
    if not login or not password:
        raise RuntimeError(
            "DataForSEO credentials not configured — set DATAFORSEO_AUTH_B64 "
            "or DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD"
        )
    raw = f"{login}:{password}".encode("utf-8")
    return f"Basic {base64.b64encode(raw).decode('ascii')}"


def _headers() -> dict[str, str]:
    return {
        "Authorization": _auth_header(),
        "Content-Type": "application/json",
    }


def _cache_get(key: tuple) -> Any | None:
    item = _cache.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: tuple, value: Any) -> None:
    _cache[key] = (time.time(), value)


async def _post(path: str, body: list[dict] | dict) -> dict:
    """POST to DataForSEO with auth, return parsed JSON. Raises on transport error."""
    url = f"{BASE_URL}{path}"
    payload = body if isinstance(body, list) else [body]
    async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
        resp = await client.post(url, headers=_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
    status = data.get("status_code")
    if status not in (20000, 20100):
        # Surface the API error string so we can debug from logs
        msg = data.get("status_message", "unknown error")
        logger.warning(f"[dataforseo] {path} status={status} msg={msg!r}")
    return data


async def get_search_volume(
    keywords: list[str],
    location: str = DEFAULT_LOCATION,
    language: str = DEFAULT_LANGUAGE,
) -> list[dict]:
    """
    Return search-volume data for a list of seed keywords. Result shape:
      [
        {"keyword": "venture secondaries", "search_volume": 720, "cpc": 4.21,
         "competition": "MEDIUM"},
        ...
      ]
    Sorted by search_volume desc. Empty list on failure (so the caller can keep going).
    """
    seeds = [k.strip() for k in keywords if k and k.strip()]
    if not seeds:
        return []

    # Dedup + cap at 700 per request (DataForSEO limit)
    seeds = list(dict.fromkeys(seeds))[:700]
    cache_key = ("sv", tuple(seeds), location, language)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    body = [{
        "keywords": seeds,
        "location_name": location,
        "language_name": language,
        "search_partners": False,
        "include_adult_keywords": False,
        "sort_by": "search_volume",
    }]

    try:
        data = await _post(
            "/keywords_data/google_ads/search_volume/live", body
        )
    except Exception as e:
        logger.error(f"[dataforseo] search_volume call failed: {e}")
        return []

    out: list[dict] = []
    for task in data.get("tasks", []) or []:
        for row in task.get("result", []) or []:
            if not row:
                continue
            out.append({
                "keyword": row.get("keyword", ""),
                "search_volume": int(row.get("search_volume") or 0),
                "cpc": float(row.get("cpc") or 0.0),
                "competition": row.get("competition", ""),
                "competition_index": int(row.get("competition_index") or 0),
            })
    out.sort(key=lambda r: r["search_volume"], reverse=True)
    _cache_set(cache_key, out)
    return out


async def get_related_keywords(
    seed: str,
    limit: int = 30,
    depth: int = 1,
    location: str = DEFAULT_LOCATION,
    language: str = DEFAULT_LANGUAGE,
) -> list[dict]:
    """
    Return related search terms for a single seed. Each item:
      {"keyword": str, "search_volume": int, "cpc": float, "competition_level": str}
    Sorted by search_volume desc. Empty on failure.
    """
    seed = (seed or "").strip()
    if not seed:
        return []

    cache_key = ("rel", seed.lower(), limit, depth, location, language)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    body = [{
        "keyword": seed,
        "location_name": location,
        "language_name": language,
        "depth": max(0, min(depth, 4)),
        "limit": max(1, min(limit, 1000)),
        "include_seed_keyword": True,
    }]

    try:
        data = await _post(
            "/dataforseo_labs/google/related_keywords/live", body
        )
    except Exception as e:
        logger.error(f"[dataforseo] related_keywords call failed: {e}")
        return []

    out: list[dict] = []
    for task in data.get("tasks", []) or []:
        for result in task.get("result", []) or []:
            for item in result.get("items", []) or []:
                kd = item.get("keyword_data") or {}
                kw = kd.get("keyword") or ""
                ki = kd.get("keyword_info") or {}
                if not kw:
                    continue
                out.append({
                    "keyword": kw,
                    "search_volume": int(ki.get("search_volume") or 0),
                    "cpc": float(ki.get("cpc") or 0.0),
                    "competition_level": ki.get("competition_level", ""),
                })
    out.sort(key=lambda r: r["search_volume"], reverse=True)
    _cache_set(cache_key, out)
    return out

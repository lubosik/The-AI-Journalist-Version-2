import asyncio
import logging
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

APIFY_BASE_URL = "https://api.apify.com/v2"
POLL_INTERVAL_SECS = 10


def _actor_path(actor_id: str) -> str:
    """Convert actor_id with '/' to '~' for REST API URL path."""
    return actor_id.replace("/", "~")


async def run_actor(actor_id: str, input_data: dict, timeout_secs: int = 300) -> list:
    """
    Run an Apify actor and return its dataset items.

    Steps:
    1. POST to start the actor run
    2. Poll until status is SUCCEEDED or FAILED
    3. Fetch and return dataset items

    Raises on FAILED status or timeout.
    """
    token = os.getenv("APIFY_API_TOKEN")
    if not token:
        raise ValueError("APIFY_API_TOKEN not set")

    actor_path = _actor_path(actor_id)
    start_url = f"{APIFY_BASE_URL}/acts/{actor_path}/runs"
    start_time = time.time()

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Start the run — retry once on transient 5xx errors
        logger.info(f"Starting Apify actor: {actor_id}")
        resp = None
        for attempt in range(3):
            try:
                resp = await client.post(
                    start_url,
                    params={"token": token},
                    json=input_data,
                )
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (502, 503, 504) and attempt < 2:
                    wait = 5 * (attempt + 1)
                    logger.warning(f"Apify {actor_id} start got {e.response.status_code}, retrying in {wait}s (attempt {attempt+1}/3)")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"Failed to start Apify actor {actor_id}: {e.response.status_code} {e.response.text[:300]}")
                    raise

        run_data = resp.json().get("data", {})
        run_id = run_data.get("id")
        if not run_id:
            raise ValueError(f"No run_id returned from Apify for actor {actor_id}")

        logger.info(f"Apify run started: actor={actor_id} run_id={run_id}")

        # Poll for completion
        status_url = f"{APIFY_BASE_URL}/acts/{actor_path}/runs/{run_id}"
        dataset_id = None

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout_secs:
                raise TimeoutError(
                    f"Apify actor {actor_id} run {run_id} timed out after {timeout_secs}s"
                )

            await asyncio.sleep(POLL_INTERVAL_SECS)

            try:
                poll_resp = await client.get(
                    status_url, params={"token": token}
                )
                poll_resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.warning(f"Poll error for run {run_id}: {e}")
                continue

            run_info = poll_resp.json().get("data", {})
            status = run_info.get("status", "UNKNOWN")
            logger.info(f"Apify run {run_id} status: {status} (elapsed: {elapsed:.0f}s)")

            if status == "SUCCEEDED":
                dataset_id = run_info.get("defaultDatasetId")
                duration = time.time() - start_time
                logger.info(f"Apify run {run_id} SUCCEEDED in {duration:.1f}s")
                break
            elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise RuntimeError(
                    f"Apify actor {actor_id} run {run_id} ended with status: {status}"
                )
            # Otherwise keep polling (RUNNING, READY, etc.)

        if not dataset_id:
            logger.warning(f"No dataset_id for run {run_id}, returning empty list")
            return []

        # Fetch dataset items
        items_url = f"{APIFY_BASE_URL}/datasets/{dataset_id}/items"
        try:
            items_resp = await client.get(
                items_url, params={"token": token, "format": "json"}
            )
            items_resp.raise_for_status()
            items = items_resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to fetch dataset items for run {run_id}: {e}")
            raise

        logger.info(
            f"Apify actor={actor_id} run_id={run_id} returned {len(items)} items "
            f"in {time.time() - start_time:.1f}s"
        )
        return items

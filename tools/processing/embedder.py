import asyncio
import logging
import os
from typing import Optional

from openai import OpenAI
from dotenv import load_dotenv

from config import EMBED_BATCH_SIZE, MODELS, OPENROUTER_BASE_URL
from db.queries import insert_content_chunk, update_content_embedding

load_dotenv()

logger = logging.getLogger(__name__)


def get_openrouter_client() -> OpenAI:
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Embed a list of texts via OpenRouter embeddings endpoint.
    Batches into groups of EMBED_BATCH_SIZE.
    Returns list of embedding vectors.
    """
    if not texts:
        return []
    if os.getenv("HERALD_USE_LEGACY_AI", "false").lower() != "true":
        logger.info("Skipping embeddings because legacy AI is disabled")
        return []

    client = get_openrouter_client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        try:
            response = await asyncio.to_thread(
                client.embeddings.create,
                model=MODELS["embeddings"],
                input=batch,
            )
            batch_embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embeddings)
            logger.debug(f"Embedded batch {i // EMBED_BATCH_SIZE + 1}: {len(batch)} texts")
        except Exception as e:
            logger.error(f"embed_texts batch error at index {i}: {e}")
            raise

    return all_embeddings


async def embed_and_store_chunks(content_item_id: str, chunks: list[dict]) -> int:
    """
    For each chunk: embed the text, insert the chunk record, then update its embedding.
    Returns the number of chunks stored.
    """
    if not chunks:
        return 0
    if os.getenv("HERALD_USE_LEGACY_AI", "false").lower() != "true":
        return 0

    texts = [c["chunk_text"] for c in chunks]
    embeddings = await embed_texts(texts)

    stored = 0
    for chunk, embedding in zip(chunks, embeddings):
        try:
            chunk_record = {
                "content_item_id": content_item_id,
                "chunk_index": chunk["chunk_index"],
                "chunk_text": chunk["chunk_text"],
            }
            chunk_id = insert_content_chunk(chunk_record)
            update_content_embedding(chunk_id, embedding)
            stored += 1
        except Exception as e:
            logger.error(f"embed_and_store_chunks error for chunk {chunk['chunk_index']}: {e}")

    logger.debug(f"Stored {stored}/{len(chunks)} chunks for content item {content_item_id}")
    return stored

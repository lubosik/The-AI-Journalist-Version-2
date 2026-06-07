import logging
import tiktoken

from config import CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS

logger = logging.getLogger(__name__)

_encoding = None


def _get_encoding():
    global _encoding
    if _encoding is None:
        _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding


def chunk_text(raw_text: str) -> list[dict]:
    """
    Split text into chunks of CHUNK_SIZE_TOKENS with CHUNK_OVERLAP_TOKENS overlap.
    Returns list of {"chunk_index": int, "chunk_text": str, "token_count": int}.
    """
    if not raw_text or not raw_text.strip():
        return []

    enc = _get_encoding()
    tokens = enc.encode(raw_text)
    total_tokens = len(tokens)

    # If shorter than chunk size, return as single chunk
    if total_tokens <= CHUNK_SIZE_TOKENS:
        return [
            {
                "chunk_index": 0,
                "chunk_text": raw_text,
                "token_count": total_tokens,
            }
        ]

    chunks = []
    chunk_index = 0
    start = 0

    while start < total_tokens:
        end = min(start + CHUNK_SIZE_TOKENS, total_tokens)
        chunk_tokens = tokens[start:end]
        chunk_text_str = enc.decode(chunk_tokens)

        chunks.append(
            {
                "chunk_index": chunk_index,
                "chunk_text": chunk_text_str,
                "token_count": len(chunk_tokens),
            }
        )

        chunk_index += 1

        if end >= total_tokens:
            break

        # Advance with overlap
        start = end - CHUNK_OVERLAP_TOKENS

    logger.debug(f"Chunked {total_tokens} tokens into {len(chunks)} chunks")
    return chunks

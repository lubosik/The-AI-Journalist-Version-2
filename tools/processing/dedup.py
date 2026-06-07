import hashlib
import logging

from db.queries import content_exists_by_url, content_exists_by_hash

logger = logging.getLogger(__name__)


def generate_content_hash(text: str) -> str:
    """Generate a SHA-256 hash of the first 1000 characters of the text."""
    return hashlib.sha256(text[:1000].encode()).hexdigest()


def is_duplicate(source_url: str | None, raw_text: str) -> bool:
    """Return True if this content already exists in the database."""
    if source_url and content_exists_by_url(source_url):
        logger.debug(f"Duplicate by URL: {source_url}")
        return True
    h = generate_content_hash(raw_text)
    if content_exists_by_hash(h):
        logger.debug(f"Duplicate by content hash: {h[:16]}...")
        return True
    return False

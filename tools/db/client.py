from supabase import create_client, Client
from dotenv import load_dotenv
import os
from urllib.parse import urlsplit, urlunsplit

load_dotenv()

_client: Client | None = None


def normalise_supabase_url(raw_url: str) -> str:
    """Return the Supabase project origin even if Railway includes a REST path."""
    value = (raw_url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SUPABASE_URL must be an absolute HTTP(S) URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in environment")
        _client = create_client(normalise_supabase_url(url), key)
    return _client

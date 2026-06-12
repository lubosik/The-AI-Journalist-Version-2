TIKTOK_PROFILES = [
    "elenanisonoff",
]

YOUTUBE_CHANNELS = [
    {"name": "TBPN", "handle": "@TBPNLive", "url": "https://www.youtube.com/@TBPNLive"},
    {"name": "All-In Podcast", "handle": "@allin", "url": "https://www.youtube.com/@allin"},
]

TWITTER_ACCOUNTS = []

# Accounts tracked by the Grok x_search intelligence sweep.
# Batched into groups of 10 (API max per call). handle must have no @ prefix.
# category is used for brief display grouping only.
X_INTELLIGENCE_ACCOUNTS = [
    # Pre-IPO company official accounts
    {"handle": "SpaceX", "category": "company"},
    {"handle": "openai", "category": "company"},
    {"handle": "databricks", "category": "company"},

    # Top VC / investor voices
    {"handle": "pmarca", "category": "investor"},
    {"handle": "bhorowitz", "category": "investor"},
    {"handle": "naval", "category": "investor"},
    {"handle": "paulg", "category": "investor"},
    {"handle": "sama", "category": "investor"},
    {"handle": "elonmusk", "category": "investor"},
    {"handle": "sequoia", "category": "investor"},
    {"handle": "a16z", "category": "investor"},
    {"handle": "foundersfund", "category": "investor"},

    # VC secondaries / deal flow specific
    {"handle": "unusual_whales", "category": "secondaries"},
    {"handle": "citrini7", "category": "secondaries"},
    {"handle": "forgeofficial", "category": "secondaries"},
    {"handle": "hiiveofficial", "category": "secondaries"},
    {"handle": "ericnewcomer", "category": "secondaries"},
    {"handle": "danprimack", "category": "secondaries"},

    # Analyst / market signal accounts
    {"handle": "karaswisher", "category": "analyst"},
    {"handle": "benedictevans", "category": "analyst"},
    {"handle": "stratechery", "category": "analyst"},
]

# Keyword queries for the daily broad X search sweep (no handle filter).
X_KEYWORD_SEARCHES = [
    "SpaceX secondary shares tender offer",
    "Anthropic pre-IPO equity secondary",
    "OpenAI cap table secondary",
    "Anduril fundraise secondary",
    "xAI valuation secondary tender",
    "Databricks secondary shares",
    "Stripe secondary market pre-IPO",
    "VC secondaries deal insider",
    "pre-IPO tender offer 2026",
]

X_TRACKED_COMPANIES = [
    "Anthropic",
    "OpenAI",
    "SpaceX",
    "Databricks",
    "Anduril",
    "xAI",
    "Stripe",
]

# Style/comedy accounts — scraped for satirical voice training, not VC content
# These are ingested daily but do NOT appear in the intelligence brief
STYLE_TWITTER_ACCOUNTS = [
    {"name": "oracles", "handle": "oracles", "url": "https://x.com/oracles", "style_category": "satire"},
    {"name": "chasedownleads", "handle": "chasedownleads", "url": "https://x.com/chasedownleads", "style_category": "satire"},
]

RSS_FEEDS = []

INSTAGRAM_ACCOUNTS = []

VC_VOICE_SAMPLE_URLS = [
    "https://newcomer.substack.com/feed",
    "https://thediff.co/feed",
    "https://sacra.com/feed/",
    "https://fortune.com/tag/term-sheet/feed/",
    "https://strictlyvc.com/feed/",
]

APIFY_ACTORS = {
    "tiktok_profile": "clockworks/tiktok-profile-scraper",
    # Primary single-video transcript actor — handles short/redirect URLs, returns structured transcript
    "tiktok_transcript_v2": "agentx/tiktok-transcript",
    # Fallback single-video transcript actor
    "tiktok_transcripts": "sian.agency/best-tiktok-ai-transcript-extractor",
    # Step 1: get video URLs from a channel (returns metadata + url per video)
    "youtube_channel": "apidojo/youtube-scraper",
    # Step 2: get transcript for specific video URLs (daily ingestion)
    "youtube_transcripts": "scrape-creators/best-youtube-transcripts-scraper",
    # Phase 2 training: YouTube transcript scraper for individual videos
    "youtube_transcript_v2": "pintostudio/youtube-transcript-scraper",
    # Spotify waterfall: direct episode transcript, then metadata for YouTube matching.
    "spotify_episodes": "apiharvest/spotify-episodes-search-and-scraper",
    "spotify_metadata": "benthepythondev/spotify-podcast-scraper",
    # Twitter/X profile and search actors.
    # Legacy key retained for handlers that still use apidojo's search input schema.
    "twitter": "apidojo/tweet-scraper",
    "twitter_profile": "mikolabs/twitter-x-profile-scraper",
    "twitter_search": "altimis/scweet",
    # Instagram profile and single-post actors.
    "instagram_profile": "muhammetakkurtt/instagram-scraper",
    "instagram_post": "patient_discovery/instagram-posts",
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# OpenRouter provider-routing preferences — passed via extra_body on tool-using
# requests so OpenRouter only routes to providers that actually support the
# requested parameters (tools / tool_choice). Without this, models that have
# providers without tool-use support can return 404 "No endpoints found that
# support tool use".
OPENROUTER_TOOL_PROVIDER_PREFS = {
    "provider": {
        "require_parameters": True,
    }
}

MODELS = {
    # Agentic reasoning — main tool-calling loop that interprets Dom's messages,
    # calls tools, and decides what to do next.
    # GPT-5.5: strongest reasoning and instruction-following for agent decisions.
    "agent": "openai/gpt-5.5",

    # Casual / conversational replies — short-form chat, affirmations, greetings.
    # Sonnet is the minimum here — Opus context is wasted on one-liners.
    "casual": "anthropic/claude-sonnet-4-6",

    # Backwards compatibility alias — same as agent.
    "brain": "openai/gpt-5.5",

    # Fast & cheap — intent classification, relevance scoring, tagging, summarisation.
    # Sonnet handles these reliably; Haiku occasionally misclassifies intent.
    "fast": "anthropic/claude-sonnet-4-6",

    # Writing — all newsletter content, LinkedIn, tweets, talking points.
    # GPT-5.5: strongest available OpenAI writing model.
    "writer": "openai/gpt-5.5",

    # Editorial review — newsletter quality gate, runs once per issue.
    # Sonnet is sufficient for structured scoring; Opus would be overkill here.
    "reviewer": "anthropic/claude-sonnet-4-6",

    # Web research — live Perplexity search for standard queries and creator lookups
    # Sonar base: $1/$1 per 1M tokens
    "research": "perplexity/sonar",

    # Deep research — only used when explicitly requested (deep=True)
    # Sonar Reasoning Pro: $2/$8 per 1M tokens (vs Sonar Pro $3/$15 — 47% cheaper on output)
    # Same multi-step search depth as Sonar Pro, adds Chain-of-Thought reasoning
    "deep_research": "perplexity/sonar-reasoning-pro",

    # Embeddings — vector storage and semantic search (unchanged, best value)
    "embeddings": "openai/text-embedding-3-small",

    # Vision — image and video multimodal analysis
    # Gemini 2.5 Flash: only model at this price that handles video natively
    "vision": "google/gemini-2.5-flash",

    # Image generation — newsletter visuals (3 per issue)
    # Recraft V3: flat $0.04/image (vs GPT Image 2 ~$0.20+/image — ~80% cheaper)
    # #1 ELO-rated image model, strong for structured editorial/brand visuals
    "image": "recraft/recraft-v3",

    # Image fallback — if primary generation fails
    "image_fallback": "google/gemini-2.5-flash",

    # xAI Grok — X intelligence sweeps via Responses API with x_search tool
    "grok_x": "grok-4.3",

}

CHUNK_SIZE_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 50
EMBED_BATCH_SIZE = 50
# Direct source ingestion needs the current newsletter week, while the morning
# brief still filters display/research to the last 48 hours.
LOOKBACK_DAYS = 7
SCHEDULE_HOUR_ET = 6

VC_SECONDARIES_KEYWORDS = [
    # Top-tier company names
    "Anthropic", "OpenAI", "SpaceX", "Anduril", "xAI", "Grok", "Stripe", "Databricks",
    "Musk", "Altman", "Andreessen", "Sequoia", "Founders Fund", "a16z",
    # Deal and cap table activity
    "pre-IPO", "tender offer", "cap table", "secondary", "secondaries",
    "fundraise", "Series E", "Series F", "Series G", "growth round", "valuation",
    "fund stake", "LP interest", "carried interest",
    # Legal / regulatory
    "Musk Altman lawsuit", "Altman Musk trial", "FTC OpenAI", "DOJ tech",
    # General top-tier signals
    "unicorn fundraise", "billion valuation", "IPO filing", "pre-IPO shares",
    "insider round", "secondary trade", "prominent startup", "top VC",
]

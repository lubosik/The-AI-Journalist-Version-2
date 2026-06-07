# HERALD v2

HERALD v2 is a private Chainlit workspace for Dom Pandolfo's VC secondaries
research and newsletter workflow. Hermes is the conversational agent. The
existing HERALD ingestion, Supabase, voice, and Beehiiv modules are copied into
`tools/` and exposed through `herald_cli.py`.

## Local setup

1. Copy the required values into `.env` from the existing HERALD deployment.
2. Set secure values for `CHAINLIT_AUTH_SECRET`, `HERALD_DOM_PASSWORD`, and
   `HERALD_ADMIN_PASSWORD`.
3. Install dependencies with `python3 -m pip install -r requirements.txt`.
4. Verify Hermes with `hermes -z "Reply with OK"`.
5. Run `chainlit run app.py --host 0.0.0.0 --port 8002`.

The app intentionally does not use OpenRouter. Legacy AI helpers fall back to
deterministic metadata so Hermes can perform the reasoning and analysis.

## Deployment note

Railway can run the Chainlit app and copied Python tools, but a Railway
container does not inherit the VPS's Codex subscription or
`/root/.hermes` credentials. A hosted deployment must install Hermes and
provide a supported Hermes authentication mechanism. Until then, run the full
agent on the VPS; direct status, topic, ingestion, HTML, and Beehiiv tools still
work wherever their environment variables are configured.


# HERALD v2

HERALD v2 is a private Chainlit workspace for Dom Pandolfo's VC secondaries
research and newsletter workflow. Hermes is the conversational agent. The
HERALD ingestion, Supabase, voice, research, and newsletter modules live inside
this repository and are exposed through `herald_cli.py`.

## Local setup

1. Configure the required values in `.env`.
2. Set secure values for `CHAINLIT_AUTH_SECRET`, `HERALD_DOM_PASSWORD`, and
   `HERALD_ADMIN_PASSWORD`.
3. Install dependencies with `python3 -m pip install -r requirements.txt`.
4. Verify Hermes with `hermes -z "Reply with OK"`.
5. Run `chainlit run app.py --host 0.0.0.0 --port 8002`.

The app uses OpenRouter as its hosted AI gateway and keeps all runtime code in
this repository.

## Deployment note

Railway runs the Chainlit app and all Python tools from this repository. Configure
the environment variables documented in `.env.example`; no legacy checkout or
external newsletter publishing API is required.

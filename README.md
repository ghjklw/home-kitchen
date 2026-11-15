# Home Kitchen

A configurable household kitchen app for recipes, meal planning, and shopping. It is a small progressive web app backed by a REST API and an MCP endpoint. The app starts with an empty household: there are no bundled recipes or family defaults.

## Run locally

Requires Python 3.12 and `uv`.

```sh
uv sync --locked
KITCHEN_LOCAL_DEV=true uv run python -m uvicorn home_kitchen.app:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Open <http://127.0.0.1:8000>. Local development access is accepted only from a loopback client. Without `KITCHEN_LOCAL_DEV=true`, startup requires Entra tenant, audience, and allowed-group configuration; authentication is locked by default. The database defaults to `data/kitchen.sqlite3`.

To use a separate writable uv cache, set `UV_CACHE_DIR` for the command, for example `UV_CACHE_DIR=/tmp/home-kitchen-uv uv sync --locked`.

## What it does

- Lets a household configure its name, adults, children's ages, preferred language, serving size, and dietary preferences. These values are entered by the household; no family data or recipes are preloaded.
- Stores and edits recipes, plans meals, scales ingredient quantities to planned servings, and builds a shopping list.
- Imports supported public Matprat and Marmiton recipe pages as drafts for review. The import retains source ingredient text; check quantities and instructions before saving.
- Can draft a recipe with a configured Anthropic API model. AI output is a draft for review; the model has no purchasing or database tools. AI arithmetic and allergy safety are not authoritative.
- Tracks regular shopping items separately from the shopping-list checkoff. A due suggestion must be confirmed to add it to the list; recording a purchase updates its last-purchased date. Checking off a list item does not claim a purchase.
- Provides curated MCP tools at `/mcp/` behind bearer authentication. The endpoint is implemented, but the remote OAuth broker and public HTTPS ingress are deployment work still to verify. This source repo alone does not mean a phone AI app can connect.
- Shows Oda's official MCP service as a separate connector for an AI client. Kitchen does not submit an Oda cart or place an order; review the cart and pay at Oda. Connector availability and AI-client support depend on their setup.

## Not implemented

- Cookidoo API integration or device-side cooking control. Recipe imports may contain source instructions, but Kitchen does not connect to a Thermomix device.
- Google Keep integration. Google documents a Keep API, but it is restricted to approved Workspace use cases and is not a general consumer sync API: <https://developers.google.com/workspace/keep/api/guides>.

See [docs/decisions.md](docs/decisions.md) for provisional product and deployment assumptions to revisit.

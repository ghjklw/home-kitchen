# Home Kitchen

A configurable household kitchen app for recipes, meal planning, and shopping. It is a progressive web app with a REST API and an MCP endpoint. It starts without recipe records or household-specific data; settings begin with neutral defaults that the household can change.

## Run locally

Requires Python 3.12 and `uv`, plus a PostgreSQL database reachable from the app. Runtime storage uses PostgreSQL through SQLAlchemy and psycopg; schema changes are managed with Alembic.

```sh
uv sync --locked
KITCHEN_DATABASE_URL='postgresql+psycopg://kitchen:replace-me@127.0.0.1:5432/kitchen' uv run python -m alembic upgrade head
KITCHEN_DATABASE_URL='postgresql+psycopg://kitchen:replace-me@127.0.0.1:5432/kitchen' KITCHEN_LOCAL_DEV=true uv run python -m uvicorn home_kitchen.app:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Replace the example DSN with a local database account; do not commit credentials. `KITCHEN_DATABASE_URL` is required and must use the `postgresql+psycopg` driver. Open <http://127.0.0.1:8000>. Local development access is accepted only from a loopback client. Without `KITCHEN_LOCAL_DEV=true`, startup requires Entra tenant, audience, and allowed-group configuration; authentication is locked by default. Do not enable local development mode in a deployed environment.

If the default cache is read-only, set `UV_CACHE_DIR` to a writable, disk-backed cache directory.

FastAPI publishes the current route schema at `/openapi.json` and its interactive reference at `/docs`.

## What it does

- Lets a household configure its name, adults, children's ages, preferred language, serving size, and dietary preferences. No household data or recipes are preloaded.
- Stores recipes, including optional HTTPS image URLs supplied by a person or retained from a supported source. It does not invent or bundle food photos.
- Plans meals, scales ingredient quantities to planned servings, and builds a shopping list from meal-plan needs, regular items, and quick additions.
- Imports supported public Matprat and Marmiton recipe pages as drafts for review. The import retains source ingredient text and any HTTPS image URL returned by the source; check quantities and instructions before saving.
- Can draft a recipe or suggest a weekly plan with an explicitly configured Anthropic model and API credential. Generated content is a draft for review; the model has no purchasing or database tools. Any image URL returned in a draft is unverified and must be checked. Kitchen does not generate photos. AI arithmetic and allergy safety are not authoritative. The automated suite uses controlled test models; it does not prove live provider availability, model behavior, or cost.
- Separates shopping needs from exact product selections in basket review. A product selection includes a provider reference, package size, price, availability, evidence time, and alternatives. Current API/MCP-submitted candidates are forced to `unverified`; no connected product-search or verification provider is enabled. Treat product details as review material, not verified facts or citations.
- Tracks regular shopping items separately from shopping-list checkoff. A due suggestion must be confirmed to add it to the list; recording a purchase updates its last-purchased date. Checking off a list item does not claim a purchase.
- Records only interactions a user or caller explicitly submits. Saved feedback, preferences, ratings, purchases, and selected domain events can inform later recommendations. Kitchen does not collect full external chats automatically.
- Provides curated MCP tools at `/mcp/` behind bearer authentication. The remote OAuth broker and public HTTPS ingress are deployment concerns and must be verified separately; this source repo alone does not mean a phone AI app can connect.
- Shows Oda's official MCP service as a separate connector for an AI client. Kitchen does not submit an Oda cart or place an order; review and pay in Oda. Connector availability and AI-client support depend on their setup.

## Not implemented

- A connected, authenticated product catalog/search/verification provider, checkout, or order placement.
- Cookidoo API integration or device-side cooking control. Recipe imports may contain source instructions, but Kitchen does not connect to a Thermomix device.
- Google Keep integration. Google documents a Keep API, but it is restricted to approved Workspace use cases and is not a general consumer sync API: <https://developers.google.com/workspace/keep/api/guides>.

See [docs/architecture.md](docs/architecture.md) for runtime boundaries and data flows, and [docs/decisions.md](docs/decisions.md) for provisional assumptions to revisit.

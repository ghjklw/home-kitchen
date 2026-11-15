# Home Kitchen

A configurable household kitchen app for recipes, meal planning, and shopping. It is a progressive web app with a REST API and an MCP endpoint. It starts without recipe records or household-specific data; settings begin with neutral defaults that the household can change.

## Run locally

Requires Python 3.12 and `uv`, plus a PostgreSQL database reachable from the app. Runtime storage uses PostgreSQL through SQLAlchemy and psycopg; schema changes are managed with Alembic.

```sh
uv sync --locked
KITCHEN_DATABASE_URL='postgresql+psycopg://kitchen:replace-me@127.0.0.1:5432/kitchen' uv run python -m alembic upgrade head
KITCHEN_DATABASE_URL='postgresql+psycopg://kitchen:replace-me@127.0.0.1:5432/kitchen' KITCHEN_LOCAL_DEV=true uv run python -m uvicorn home_kitchen.app:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
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
- Separates shopping needs from exact product selections in basket review. A product selection includes a provider reference, package size, price, availability, evidence time, and alternatives. Current API/MCP-submitted candidates are forced to `unverified`; Oda search uses a direct, actor-scoped OAuth connection when configured; fulfillment quantities still require review. Treat product details as review material, not verified facts or citations.
- Tracks regular shopping items separately from shopping-list checkoff. A due suggestion must be confirmed to add it to the list; recording a purchase updates its last-purchased date. Checking off a list item does not claim a purchase.
- Records only interactions a user or caller explicitly submits. Saved feedback, preferences, ratings, purchases, and selected domain events can inform later recommendations. Kitchen does not collect full external chats automatically.
- Provides curated MCP tools at `/mcp/` behind bearer authentication. The remote OAuth broker and public HTTPS ingress are deployment concerns and must be verified separately; this source repo alone does not mean a phone AI app can connect.
- Connects to Oda directly using browser consent, PKCE, and encrypted server-side tokens. Product search and cart inspection are read-only. The app can enrich a result from its exact public Oda product-page URL with existing product images and explicit pack descriptions; it never guesses image URLs or package quantities. A user can review an expiring, actor-bound proposal containing positive package additions, then explicitly confirm it. Confirmation checks a fresh cart fingerprint and records a single-use attempt before sending; an uncertain attempt is not retried. This is not checkout or payment. OAuth and cart writes have mocked tests only and still need live acceptance testing.
- Keeps the shopping plan separate from purchasing. Stock already at home offsets required quantities; Oda basket review and a category-sorted in-store picking checklist use only the net quantities. In-store checkmarks are progress, not proof of payment or automatic stock consumption.

## Not implemented

- Completed live Oda sign-in/search/cart-write validation, provider-verified package conversion, checkout, or order placement.
- Cookidoo API integration or device-side cooking control. Recipe imports may contain source instructions, but Kitchen does not connect to a Thermomix device.
- Google Keep integration. Google documents a Keep API, but it is restricted to approved Workspace use cases and is not a general consumer sync API: <https://developers.google.com/workspace/keep/api/guides>.

See [docs/architecture.md](docs/architecture.md) for runtime boundaries and data flows, and [docs/decisions.md](docs/decisions.md) for provisional assumptions to revisit.

## AI configuration

AI requires `KITCHEN_AI_MODEL` and either `KITCHEN_ANTHROPIC_API_KEY` or explicit workload identity authentication. For workload identity, set `KITCHEN_ANTHROPIC_WORKLOAD_IDENTITY=true`, `KITCHEN_ANTHROPIC_FEDERATION_RULE_ID`, `KITCHEN_ANTHROPIC_ORGANIZATION_ID` (raw UUID), and `KITCHEN_ANTHROPIC_SERVICE_ACCOUNT_ID`; `KITCHEN_ANTHROPIC_WORKSPACE_ID` is optional. Supply `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and an absolute `AZURE_FEDERATED_TOKEN_FILE` path to the projected Kubernetes token. Kitchen exchanges that token through Azure Workload Identity for scope `api://claude-api-federation/.default`; the Anthropic SDK exchanges the Entra token and refreshes its access tokens. Configure the audience registration and Anthropic federation rule separately before enabling this mode. API key and workload identity modes cannot be combined; ambient Anthropic credentials and Azure default/managed identity discovery are not used.

## Direct Oda connection

Set `KITCHEN_PUBLIC_URL` to the deployed HTTPS origin and inject `KITCHEN_ODA_ENCRYPTION_KEY` as a Fernet key from your secret manager. Kitchen registers an OAuth client only when a signed-in user chooses Connect. Consent and credentials stay on Oda; encrypted connection records are scoped to the authenticated actor. Callback state expires after ten minutes and is single use. Keep the encryption key backed up with controlled access: replacing it requires reconnecting existing accounts. Do not log callback query strings.

Oda's published search schema does not contain product photos or numerical package quantities. Kitchen can extract existing image references and explicit pack descriptions only from the matching public Oda product page. Missing information stays unknown; never derive a pack size from a unit-price label. See [Oda's official MCP specification](https://oda.com/mcp/manifest/).

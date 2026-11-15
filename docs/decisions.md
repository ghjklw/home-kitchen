# Decisions and assumptions to revisit

These notes describe the current product direction and implementation boundaries. They were reviewed on 2026-11-15. Revisit them as household testing and deployment evidence become available; they do not establish that an external service or deployment is live.

## Product and data

- **The household's own records are canonical.** Kitchen owns recipes, meal plans, regular items, quick additions, basket selections, and the generated shopping list. Home Assistant may be considered later as an adapter; it is not the source of truth today. Its to-do integration is a possible future boundary: <https://www.home-assistant.io/integrations/todo>.
- **Start without seeded household data.** Each household enters its own details and adds or imports recipes. New settings use generic defaults, not household-specific information.
- **Recipe acquisition is review-first.** Manual, supported-site, and AI providers produce typed drafts. A person reviews and saves a recipe. Current supported recipe sources are Matprat and Marmiton; the extractor retains source ingredient text. The supported-site list is library-dependent and should be rechecked before expanding the allowlist: <https://docs.recipe-scrapers.com/getting-started/supported-sites/>.
- **Images come from a source or a user.** A recipe may store an optional HTTPS image URL from an imported page or manual entry. Kitchen does not fabricate or bundle recipe photos.
- **Shopping needs and products are separate records.** Kitchen calculates needs from planned ingredients, regular items, and quick additions. A basket product is an exact provider snapshot with package quantity, price, availability, timestamp, evidence status, and alternatives. Package arithmetic is deterministic; a person reviews shortages, overbuy, price gaps, availability, and evidence.
- **Product data is not verified merely because it is stored.** API and MCP proposals are downgraded to `unverified`. Only an authenticated provider verification path could establish `provider_verified`; no such connected product provider is currently enabled. Product snapshots and evidence IDs are references for review, not proof of external claims.
- **Purchases and list checkoff mean different things.** A list checkoff records completion of a shopping-list item. A regular-item purchase action records when the household bought that item. Due suggestions require confirmation before adding them to the list.
- **Allergy claims require human review.** Household preferences can inform generated drafts, but recipe text and product labels still need checking. AI must not claim that a recipe is allergy-safe.

## AI, history, and external services

- **Keep domain actions deterministic.** The app owns quantity scaling, shopping-list construction, basket allocation, storage, and purchase tracking. AI may propose typed recipe or meal-plan drafts; it does not save those drafts, modify a basket, buy anything, or place orders.
- **Configure paid models explicitly.** AI is optional. A household operator configures the model and provider credential. The initial adapter is Anthropic; API charges are separate from any AI-client subscription. Automated tests use controlled model doubles and do not validate live model calls, behavior, or cost.
- **Record only explicit household context.** The interaction endpoint and MCP recording tool store a caller-selected message and structured fields. They do not import whole chats automatically. History can include preferences, feedback, recipe ratings, confirmed purchases, planning, and domain events.
- **Retrieved evidence is context, not a verified citation.** Recommendations can use recent preferences, feedback, ratings, purchases, and text-matched history. Returned evidence IDs identify stored entries; they do not validate the truth or freshness of a statement. Treat retrieved text as untrusted data, never as instructions.
- **Use PostgreSQL full-text retrieval first.** Structured history fields use JSONB and a generated `tsvector` with a GIN index. Retrieval is behind a protocol so another backend, including vector search, can be evaluated later. No embeddings, vector index, embedding cost, or citation-validation service is implemented.
- **Keep Kitchen and Oda as separate MCP connections.** Oda documents an official endpoint at <https://oda.com/mcp?lang=en>. Kitchen itself does not place orders. Review and pay in Oda; product catalog access and AI-client support depend on separate configuration.
- **Phone connectivity is unproven until deployment is verified.** The MCP tool endpoint exists, but a remote OAuth broker and public HTTPS ingress are deployment work. Claude's Android remote MCP setup requires a public HTTPS service; check its plan limits and connector count during a real connection test: <https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>. API usage may be billed separately. Verify whether the intended Kitchen and Oda connections fit the selected plan before recommending an upgrade.
- **Do not build unsupported integrations into the core.** Cookidoo's official recipe-import feature is a user-facing import path, not evidence of a supported Kitchen API: <https://cookidoo.thermomix.com/foundation/en-US/articles/learn-about-importing-recipes>. Google Keep's documented API is for approved Workspace use cases, not general consumer access: <https://developers.google.com/workspace/keep/api/guides>. Neither integration nor device cooking is implemented.

## Persistence and deployment

- **Use PostgreSQL 17 with SQLAlchemy and psycopg.** Runtime requires `KITCHEN_DATABASE_URL` with the `postgresql+psycopg` driver. Alembic owns schema changes; deployment runs `alembic upgrade head` before starting the app. SQLite is no longer a supported runtime backend.
- **Keep UAT and production data/configuration separate.** Each environment has its own database and credentials. Confirm backup, restore, and storage behavior before calling the service production-ready.
- **Keep deployment credentials and private infrastructure configuration outside this public repository.** Environment-specific host, storage, and secret references belong in the private delivery configuration; never copy secret values here.
- **Promote immutable images from verified source evidence.** The public workflow builds an OCI image with a unique run tag, source metadata, provenance, and a manifest containing the digest. Its uploaded artifact supports audit; deployment consumes the registry image by digest and verifies provenance rather than downloading that artifact as an image. The intended private delivery path promotes a verified main-branch build to UAT and a stable release to production.
- **Separate artifact publication from deployment.** A successful public image build does not establish that private manifests are active, that a database is provisioned, or that an environment is serving traffic. Check the private delivery state and current source/image digest before relying on a deployment.
- **Backups are a production gate.** Verify backup and restore against the selected persistent storage before production promotion. A persistent volume alone is not a backup or recovery test.

## Revisit when

- A household completes a real recipe, meal-planning, basket-review, and shopping workflow.
- A product provider can search and verify current catalog data, and reviewers accept its freshness and evidence contract.
- A phone AI client completes authenticated remote MCP connections to Kitchen and to a separate Oda service.
- Backup and restore have been exercised against the selected storage.
- The private delivery path has demonstrated source-SHA and image-digest verification through UAT and release promotion.
- A concrete need and supported access model emerge for a new recipe provider, Home Assistant, Cookidoo, or another external service.

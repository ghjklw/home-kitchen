# Decisions and assumptions to revisit

These notes record the current direction, not a promise that integrations or deployment are live. They were written on 2026-11-15. Revisit them as household testing and deployment evidence become available.

## Product and data

- **The household's own list is canonical.** Kitchen owns recipes, meal plans, regular items, and the generated shopping list. Home Assistant can be considered later as an adapter; it is not the source of truth today. Its to-do integration is a possible future boundary: <https://www.home-assistant.io/integrations/todo>.
- **Start without seeded household data.** Each household configures its own details and adds or imports recipes.
- **Imports stay reviewable.** Current supported recipe sources are Matprat and Marmiton. The extractor returns a draft; a person reviews and saves it. The supported-site list is library-dependent and should be rechecked before expanding the allowlist: <https://docs.recipe-scrapers.com/getting-started/supported-sites/>.
- **Purchases and list checkoff mean different things.** A list checkoff records completion of a shopping-list item. A regular-item purchase action records when the household bought that item. Due suggestions require confirmation before adding them to the list.
- **Allergy claims require human review.** Household preferences can inform generated drafts, but recipe text and labels still need checking. AI must not claim that a recipe is allergy-safe.

## AI and external services

- **Keep domain actions deterministic.** The app owns quantity scaling, shopping-list construction, storage, and purchase tracking. AI may propose typed recipe drafts; it does not calculate authoritative quantities, save recipes, or buy anything.
- **Do not select a paid model by default.** A household operator configures the model and provider credential. The initial adapter is Anthropic; model API charges are separate from any AI-client subscription.
- **Treat Kitchen and Oda as separate MCP connections.** Oda documents an official endpoint at <https://oda.com/mcp?lang=en>. The household should review and pay in Oda; Kitchen itself does not place orders.
- **Phone connectivity is unproven until deployment is verified.** The MCP tool endpoint exists, but a remote OAuth broker and public HTTPS ingress are not configured by this app repository. Claude's Android remote MCP setup requires a public HTTPS service; its plan limits and connector count should be checked during a real connection test: <https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>. API usage may be billed separately. Verify whether the intended Kitchen and Oda connections fit the selected plan before recommending an upgrade.
- **Do not build unsupported integrations into the core.** Cookidoo's official recipe-import feature is a user-facing import path, not evidence of a supported Kitchen API: <https://cookidoo.thermomix.com/foundation/en-US/articles/learn-about-importing-recipes>. Google Keep's documented API is for approved Workspace use cases, not general consumer access: <https://developers.google.com/workspace/keep/api/guides>. Neither integration nor device cooking is implemented.

## Deployment and delivery

- **Use SQLite on one persistent volume for the initial single-pod deployment.** Keep UAT and production data and configuration separate. Confirm backup, restore, and storage behavior before treating this as production-ready.
- **Keep deployment credentials and private infrastructure configuration outside this public repository.** Use a separate private deployment repository for environment-specific configuration and secrets.
- **Promote immutable images from verified source evidence.** This repository's workflow runs CI, publishes an image with provenance and a digest, and uploads source SHA, event, digest, and build-run metadata. The intended policy is for the private delivery configuration to verify successful build provenance before promoting a main-branch image to UAT; a stable published release is the production promotion input.
- **Verify delivery status before relying on it.** The public workflow proves image publication and metadata generation only. Whether the private delivery process currently polls, verifies, and deploys these artifacts to UAT or production must be checked in that private system; this document does not claim a live deployment.

## Revisit when

- A household completes a real recipe, meal-planning, and shopping workflow.
- A phone AI client completes an authenticated remote MCP connection to Kitchen and a separate Oda connection.
- Backup and restore have been exercised against the selected persistent storage.
- The private delivery path has demonstrated source-SHA and image-digest verification through UAT and release promotion.
- A concrete need and supported access model emerge for a new recipe provider, Home Assistant, Cookidoo, or another external service.

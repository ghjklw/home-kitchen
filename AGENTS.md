# Repository Agent Instructions (`ghjklw/home-kitchen`)

## Pre-Approved Autonomous Operations

For this repository (`ghjklw/home-kitchen`), the repository owner has **pre‑approved** the following operations so agents can work through issues and maintenance autonomously without pausing to ask for confirmation:

- **GitHub Issues**: Create, update, label (`in progress`, `needs review`, `blocked`, `good first issue`, etc.), comment on, reopen, and close issues.
- **Git & Branches**: Stage, commit, push branches, rebase/merge `origin/main`, and clean up merged local/remote branches and temporary worktrees.
- **Pull Requests**: Create, update, review, and merge PRs (once relevant tests and checks pass).
- **Testing & QA**: Run unit, integration, Node/JS, and Playwright/browser tests, and start/update local QA servers.
- **Deployment**: Run repository‑defined deployment/release checks and promote approved/tested releases as configured.

## Issue Management Guidelines

1. **Creating an Issue**
   - Use the appropriate template (bug, enhancement, documentation, etc.).
   - Add a clear, concise title and a detailed description.
   - Apply initial labels: `triage` and a type label (`bug`, `enhancement`, `documentation`).
   - If the issue is a good entry point for newcomers, add `good first issue`.
2. **Updating an Issue**
   - When work begins, add the `in progress` label and remove `triage`.
   - Reference the related branch or PR in a comment (`Refs #<issue>`).
   - Provide regular status updates in comments; use check‑list items to track progress.
3. **Transitioning Labels**
   - Move from `in progress` → `needs review` when a PR is ready for review.
   - Add `blocked` if external dependencies prevent progress; include a comment explaining the blocker.
   - Once approved, replace `needs review` with `ready to merge`.
4. **Closing an Issue**
   - After the PR is merged, comment `Fixes #<issue>` (or use the “Close issue” button) and add the `done` label.
   - Ensure all acceptance criteria are met and tests pass before closing.
5. **Comment Practices**
   - Use @mentions to draw reviewers or stakeholders.
   - Summarize decisions and action items; keep comments focused and professional.
   - Link to related PRs, documentation, or external tickets.

## Working Conventions

1. **Worktrees**: Use isolated worktrees under `/home/malo.jaffre/Code/.agent-tmp/` branched from `origin/main` rather than mutating the primary checkout directly.
2. **Verification**: Run relevant `pytest` and Node/JS tests before committing or merging a PR.
3. **Model Selection**: Prefer low‑cost fast models (`flash` / Gemini 3.8 Flash, or `gpt‑oss 120B` when Gemini quota is constrained) for routine backlog implementation and research, reserving heavier models for complex debugging or architectural review.
4. **Safety**: Never commit secrets, `.env` files, or credentials. Keep changes focused on the issue being addressed.

## Local Development & Testing

- **Skill:** `local-test-instance`
  - **Description:** Starts the development PostgreSQL container (if not already running), runs Alembic migrations, and launches the FastAPI server on `http://localhost:18099`.
  - **Usage:**
    ```bash
    docker start hk-pg 2>/dev/null || docker run -d --name hk-pg -e POSTGRES_USER=kitchen -e POSTGRES_PASSWORD=secret -e POSTGRES_DB=kitchen -p 5432:5432 postgres:16
    export KITCHEN_DATABASE_URL=postgresql+psycopg://kitchen:secret@127.0.0.1:5432/kitchen
    uv run alembic upgrade head
    KITCHEN_LOCAL_DEV=true PYTHONPATH=src uv run uvicorn home_kitchen.app:create_app --factory --host 127.0.0.1 --port 18099
    ```
  - **Intent:** Provides a single command agents (or developers) can invoke to obtain a reproducible local testing environment.

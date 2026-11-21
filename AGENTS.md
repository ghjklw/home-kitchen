# Repository Agent Instructions (`ghjklw/home-kitchen`)

## Pre-Approved Autonomous Operations

For this repository (`ghjklw/home-kitchen`), the repository owner has **pre-approved** the following operations so agents can work through issues and maintenance autonomously without pausing to ask for confirmation:

- **GitHub Issues**: Create, update, label (`in progress`, etc.), comment on, reopen, and close issues.
- **Git & Branches**: Stage, commit, push branches, rebase/merge `origin/main`, and clean up merged local/remote branches and temporary worktrees.
- **Pull Requests**: Create, update, review, and merge PRs (once relevant tests and checks pass).
- **Testing & QA**: Run unit, integration, Node/JS, and Playwright/browser tests, and start/update local QA servers.
- **Deployment**: Run repository-defined deployment/release checks and promote approved/tested releases as configured.

## Working Conventions

1. **Worktrees**: Use isolated worktrees under `/home/malo.jaffre/Code/.agent-tmp/` branched from `origin/main` rather than mutating the primary checkout directly.
2. **Verification**: Run relevant `pytest` and Node/JS tests before committing or merging a PR.
3. **Model Selection**: Prefer low-cost fast models (`flash` / Gemini 3.8 Flash, or `gpt-oss 120B` when Gemini quota is constrained) for routine backlog implementation and research, reserving heavier models for complex debugging or architectural review.
4. **Safety**: Never commit secrets, `.env` files, or credentials. Keep changes focused on the issue being addressed.

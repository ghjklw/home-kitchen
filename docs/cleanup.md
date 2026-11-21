# Worktree Cleanup

This document records the cleanup of stale agent worktrees under `/home/malo.jaffre/Code/.agent-tmp/` for the `home-kitchen` repository.

## Retained Worktrees & Branches

- `main` (`/home/malo.jaffre/Home/home-kitchen`)
- `refactor/issue-112-step3-features-split` (`/home/malo.jaffre/Code/.agent-tmp/home-kitchen-arch-step3`)
- `refactor/issue-112-step4-matching-split` (`/home/malo.jaffre/Code/.agent-tmp/home-kitchen-arch-step4-matching`)
- `verify/trumf-receipt-sync` (`/home/malo.jaffre/Code/.agent-tmp/verify_trumf`)

## Removed Worktrees

All other stale temporary agent worktrees and detached HEAD checkouts under `/home/malo.jaffre/Code/.agent-tmp/` have been pruned and removed to reclaim disk space and memory headroom.

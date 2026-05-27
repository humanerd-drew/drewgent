---

title: 1Password CLI get-started (summary)
type: document
space: concept
tags: [concept]
created: 2026-05-20
updated: 2026-05-20
links: []
links:
  - "[[P4-cortex/knowledge/NEURONFS_RULES]]"
---


# 1Password CLI get-started (summary)

Official docs: https://developer.1password.com/docs/cli/get-started/

## Core flow

1. Install `op` CLI.
2. Enable desktop app integration in 1Password app.
3. Unlock app.
4. Run `op signin` and approve prompt.
5. Verify with `op whoami`.

## Multiple accounts

- Use `op signin --account <subdomain.1password.com>`
- Or set `OP_ACCOUNT`

## Non-interactive / automation

- Use service accounts and `OP_SERVICE_ACCOUNT_TOKEN`
- Prefer `op run` and `op inject` for runtime secret handling

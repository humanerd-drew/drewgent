# Huly Content Review → Publish Workflow

## Planned Pipeline
```
Content-manager → draft files (SVG + MD + PNG)
  → Huly issue created (assignee: drew, status: "Todo")
  → Drew reviews in Huly kanban
  → Status changes to "Done" (or equivalent)
  → Watcher detects change
  → WordPress MCP pushes to humanerd.kr
```

## Huly MCP Server
- Provider: `@bgx4k3p/huly-mcp-server@latest`
- Workspace: `humanerd` on huly.app
- 81 tools available
- Auth: HULY_KEY from `~/.hermes/.env`

## Key Tools for Content Workflow
- `create_issue` — create content review task
- `list_issues` — find content tasks
- `update_issue` — change status
- `add_comment` — review notes
- `search_issues` — find by keywords

## Status: Huly webhooks NOT available
Huly doesn't have outgoing webhooks yet (GitHub issues #6996, #9187).
Planned approach: lightweight polling via gateway internal scheduler.

## Alternative: Local Kanban Board
If Hulu integration isn't ready, use the local kanban system:
1. Content-manager creates kanban task (status: "blocked" = needs review)
2. Dashboard: `localhost:8644` or via launchctl `ai.drewgent.kanban-dashboard`
3. Unblock → triggers WordPress publish via dispatch_once_content.py

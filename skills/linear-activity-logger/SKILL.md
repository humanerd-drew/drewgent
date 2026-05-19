---
title: Linear Activity Logger
description: Monitor Discord sessions for work patterns and create kanban cards + optional Linear issues. Run every 5 minutes via cron.
version: 1.0.0
author: drewgent-core
license: MIT
metadata:
  drewgent:
    tags: [kanban, activity-logger, discord, linear, cron]
    category: kanban
    trigger: /linear-activity-logger
links:
  - "[[P4-cortex/growth/drewgent-kanban-implementation-plan]]"
  - "[[P4-cortex/growth/KANBAN-USER-GUIDE]]"
  - "[[P5-ego/SELF_MODEL]]"
---

# Linear Activity Logger

Monitors Discord sessions for work patterns, creates kanban cards in drewgent_tasks.db,
and optionally creates Linear issues. Designed to run as a cron job every 5 minutes.

## Purpose

Closes the gap between Discord activity and the Drewgent kanban board:

```
Discord conversation (work pattern detected)
    → Linear Activity Logger (5min cron)
    → Drewgent kanban card created (status=ready)
    → kanban-dispatcher (1min cron) sees ready task
    → Worker spawned to handle task
```

Without this, Discord conversations about work never become kanban cards,
so the dispatcher never picks them up.

## How It Works

### 1. Session Scanning

- Finds all `.json` session files in `~/.drewgent/P2-hippocampus/sessions/` modified in the last 1 hour
- Loads messages from each session
- For each user message, checks work pattern

### 2. Work Pattern Detection

A message is a "work trigger" if it matches any of:

| Pattern | Example |
|---------|---------|
| 진행해줘 | "SEO article 해줘" |
| 끝났어 | "완료 끝났어" |
| 시작해줘 | "새로 시작해줘" |
| 해봅시다 | "elm do it 해봅시다" |

Skipped (not work triggers):
- 확인해 봐 (just checking)
- 질문 확인 (just a question)

### 3. Idempotency

Each session+message combination is tracked in `~/.drewgent/state/linear_activity_logger.json`.
Once a card is created for a message, it won't be created again even if the logger runs multiple times.

### 4. Kanban Card Creation

```python
task_create(
    title=f"[{ctx}] {content[:80]}...",
    body=content,
    assignee="drewgent",
    status="ready",
    priority=2,
    trigger_source="activity_logger",
    idempotency_key=f"activity:{session_id}:{message_idx}",
)
```

### 5. Optional Linear Issue Creation

If `LINEAR_API_KEY` and `LINEAR_TEAM_ID` env vars are set, also creates a Linear issue
with the same title/body. Parent relationships can be set via `linear_parent_ids`.

## Running

### As cron job

Add to `~/.drewgent/cron/jobs.json`:

```json
{
  "id": "xxxxxxxxxxxx",
  "name": "linear-activity-logger",
  "prompt": "Run Linear Activity Logger.\n\nSteps:\n1. cd ~/.drewgent/source/drewgent-agent\n2. python3 scripts/linear_activity_logger.py\n3. Report: sessions_scanned=N, messages_processed=N, cards_created=N\n4. If cards_created > 0: show card titles",
  "skills": [],
  "skill": null,
  "schedule": {"kind": "cron", "expr": "*/5 * * * *", "display": "*/5 * * * *"},
  "repeat": {"times": null, "completed": 0},
  "enabled": true,
  "state": "scheduled",
  "deliver": "local"
}
```

### Manual run

```bash
cd ~/.drewgent/source/drewgent-agent
python3 scripts/linear_activity_logger.py
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LINEAR_API_KEY` | No | Linear API key for issue creation |
| `LINEAR_TEAM_ID` | No | Linear team ID for issue creation |
| `DREWGENT_HOME` | No | Override Drewgent home path |

## State File

`~/.drewgent/state/linear_activity_logger.json`:

```json
{
  "processed_messages": ["session_xxx:42", "session_yyy:15"],
  "last_run_at": "2026-05-19T12:00:00+09:00",
  "created_cards": 23
}
```

Last 5000 processed message keys are kept to avoid unbounded growth.

## Files

- `scripts/linear_activity_logger.py` — core script
- `skills/linear-activity-logger/SKILL.md` — this file
- `state/linear_activity_logger.json` — persisted state

## Design Decisions

1. **No Linear SDK** — uses raw GraphQL over `requests` (no extra deps)
2. **Stateless scan** — doesn't track session position; uses processed_messages set to avoid duplicates
3. **Short scan window** — only last 1 hour to keep each tick fast
4. **Board defaults to 'default'** — content pipeline gets its own board via `board=` param
5. **Created_by = 'activity_logger'** — distinguishes cron-triggered cards from manual cards

## Related

- [[tools/drewgent_kanban_db.py]] — task_create, dispatch_once
- [[tools/linear_kanban_tools.py]] — Linear API bridge
- [[P4-cortex/growth/KANBAN-USER-GUIDE]] — user-facing docs
- [[skills/kanban-dashboard]] — n8n dashboard (board notification delivery pending)
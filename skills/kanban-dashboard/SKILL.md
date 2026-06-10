---
title: Skill
name: kanban-dashboard
type: skill
description: Drewgent kanban board UI — Flask dashboard server for visual task management
space: outcome
tags: [outcome]
created: 2026-05-20
updated: 2026-05-31
links:
  - "[[P2-hippocampus/kanban/KANBAN_INDEX]]"
  - "[[skills/automation/DESCRIPTION]]"
---




# Kanban Dashboard Skill

# Kanban Dashboard — Flask Server (Primary)

Flask server가 kanban board를 렌더링. SSE 실시간 업데이트 + 드래그드롭 + 모바일 대응.

**URL**: `http://macmini:8765/kanban`

## 레이아웃

- **보드 탭**: All / default / content / integrations — 탭 클릭으로 보드 필터
- **컬럼**: To Do | Ready | In Progress | Blocked | Completed — 상태별 한 줄
- **카드**: 우선순위 P1/P2/P3, 보드명, 작업자, 생성일, 만료시간
- **실시간**: SSE 스트림 연결 → 카드 액션 시 즉시 화면 반영 (초록 점으로 연결 상태 표시)
- **드래그드롭**: 카드를 다른 컬럼으로 드래그하면 상태 자동 변경
- **모바일**: 터치 스크롤, 작은 화면에서 컬럼 너비 축소

## LaunchAgent (Self-Healing, Auto-Restart)

```
/Users/drew/Library/LaunchAgents/ai.drewgent.kanban-dashboard.plist
```

- `KeepAlive: SuccessfulExit=false` → 프로세스 죽으면 자동 재시작
- MacMini 재부팅해도 자동 실행
- 로그: `/Users/drew/.drewgent/P6-prefrontal/logs/kanban-server.log`

## 관리 명령

```bash
# 상태 확인
launchctl list | grep kanban

# 수동 시작/정지
launchctl start ai.drewgent.kanban-dashboard
launchctl stop ai.drewgent.kanban-dashboard

# 로그 확인
tail -f /Users/drew/.drewgent/P6-prefrontal/logs/kanban-server.log
```

## 엔드포인트

| Method | Path | Description |
|--------|------|-------------|
| GET | `/kanban` | Kanban board HTML (SSE 실시간 업데이트) |
| GET | `/kanban/api/board` | JSON board state |
| GET | `/kanban/api/stream` | SSE 실시간 스트림 |
| POST | `/kanban/api/complete` | task complete |
| POST | `/kanban/api/claim` | task claim |
| POST | `/kanban/api/block` | task block |
| POST | `/kanban/api/unblock` | task unblock |
| POST | `/kanban/api/create` | task create |
| POST | `/kanban/api/delete` | task delete |
| POST | `/kanban/api/dispatch` | task dispatch (spawn worker) |
| POST | `/kanban/api/update_status` | task status 변경 (드래그드롭) |

## 파일

- Server script: `/Users/drew/.drewgent/P4-cortex/scripts/kanban_dashboard_server.py`
- LaunchAgent plist: `/Users/drew/Library/LaunchAgents/ai.drewgent.kanban-dashboard.plist`

## Pitfalls

- `get_tasks()` in server uses its own `init_db()` — task table schema must match `drewgent_tasks.db` (both use same board column). If schema mismatches, board returns empty.
- Access from outside: `http://macmini:8765/kanban` (same network). MacMini hostname or IP 사용.
- **f-string escape bug**: Python f-string에서 `{{var}}` → literal `{var}` 출력. Python 변수 사용은 `{var}` (single brace).
- **Server restart required**: Changing `kanban_dashboard_server.py` doesn't auto-reload. Must run `launchctl stop ai.drewgent.kanban-dashboard && launchctl start ai.drewgent.kanban-dashboard` to apply changes.
- **Python 3.14 compat**: Server runs under Python 3.14.4 (from `.venv`). Syntax is OK but test in the actual venv, not system python3.

## Board UI Workflow (n8n)

### Trigger
- **Cron**: Every 5 minutes (`*/5 * * * *`)

### Nodes

```
1. Cron Trigger (every 5min)
   ↓
2. SQLite Node — Query drewgent_tasks.db
   SQL: |
     SELECT id, title, status, assignee, created_at,
            last_heartbeat_at, consecutive_failures
     FROM tasks
     WHERE board = 'default'
     ORDER BY priority ASC NULLS LAST, created_at DESC
     LIMIT 50
   ↓
3. Discord Bot Token (from .env: DISCORD_BOT_TOKEN)
   ↓
4. Discord Embed Builder (per status group)
   ↓
5. Edit Message — post board to designated Discord channel
```

### Board Embed Format

```
=== Drewgent Kanban Board ===
Board: default | Updated: 2026-05-19 10:30 KST

[todo] 3 tasks
  🟡 t_abc123 — Implement kanban-dashboard skill
  🟡 t_def456 — Fix cycle detection bug

[ready] 2 tasks
  ⚪ t_ghi789 — Deploy n8n workflow

[in_progress] 1 task
  🔵 t_jkl012 — kanban-orchestrator skill (worker: pid 12345)

[blocked] 1 task
  🔴 t_mno345 — gateway notifier (failures: 3)

[completed] 7 tasks (today: 2)
  ✅ t_pqr678 — multi-board support
  ✅ t_stu901 — activity logger integration

React to manage:
  ✅ = complete | 🔄 = unblock | ❌ = block
```

### Task Groups (by status)

| Status | Color | Emoji | Meaning |
|--------|-------|-------|---------|
| todo | 🟡 | YELLOW | Not yet ready |
| ready | ⚪ | WHITE | Claimable |
| in_progress | 🔵 | BLUE | Worker active |
| blocked | 🔴 | RED | Waiting / failed |
| completed | ✅ | GREEN | Done |

## Reaction → Action Workflow

When user reacts to the board message, n8n captures the reaction event:

```
1. Discord Reaction Event (add)
   ↓
2. Extract: message_id, emoji, user_id
   ↓
3. SQLite — find task by id (from message content parsing)
   ↓
4. Switch on emoji:
   - ✅ → kanban_complete(task_id, result="manual")
   - 🔄 → kanban_unblock(task_id)
   - ❌ → kanban_block(task_id, reason="manual")
   - 🔁 → kanban_claim(task_id, ttl_seconds=3600)
   ↓
5. Edit board message (refresh status)
```

### Emoji Mapping

| Emoji | Action | Tool |
|-------|--------|------|
| ✅ | Complete task | `kanban_complete` |
| 🔄 | Unblock task | `kanban_unblock` |
| ❌ | Block task | `kanban_block` |
| 🔁 | Claim task | `kanban_claim` |

## Gateway Notifier (Phase 2)

Push task events to Discord subscribers.

### SQLite Schema Addition

```sql
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    platform    TEXT NOT NULL,  -- 'discord', 'telegram', etc.
    chat_id     TEXT NOT NULL,
    thread_id   TEXT,
    subscriber  TEXT NOT NULL,  -- user_id or channel_id
    created_at   TEXT NOT NULL,
    UNIQUE(task_id, platform, chat_id, subscriber)
);

CREATE INDEX IF NOT EXISTS idx_notify_task ON kanban_notify_subs(task_id);
```

### Notifier Workflow

```
Task event fires (completed/blocked/crashed)
  ↓
1. Event Listener (from DrewgentTaskStore.task_events table)
   ↓
2. Lookup subscribers for this task_id
   ↓
3. Per subscriber:
   - Build Discord embed with event details
   - Send via Discord webhook / bot
   - Include: task title, status change, result summary
   ↓
4. Log notification in task_events
```

### Notification Embed Format

```
🎉 Task Completed
  [content] multi-board support
  Result: boards table + board-aware dispatch_once
  Time: 2026-05-19 10:35 KST
  Trigger: integration_workflow
```

## n8n Workflow JSON (board poll)

```json
{
  "name": "Drewgent Kanban Board",
  "nodes": [
    {
      "name": "Cron Trigger",
      "type": "n8n-nodes-base.cron",
      "parameters": {
        "rule": {"interval": [{"field": "minutes", "minutes": 5}]}
      }
    },
    {
      "name": "Query Tasks",
      "type": "n8n-nodes-base.sql",
      "parameters": {
        "operation": "executeQuery",
        "dataMode": "resolve",
        "query": "SELECT id, title, status, assignee, created_at FROM tasks WHERE board = 'default' ORDER BY created_at DESC LIMIT 50"
      }
    },
    {
      "name": "Build Embed",
      "type": "n8n-nodes-base.code",
      "parameters": {
        "jsCode": "// Group by status, build Discord embed JSON"
      }
    },
    {
      "name": "Post to Discord",
      "type": "n8n-nodes-discord.api",
      "parameters": {
        "webhook": "{{$env.DISCORD_WEBHOOK_KANBAN}}"
      }
    }
  ]
}
```

## Discord Channel Setup

Board message posted to: `1492883985473208522` (content-notify-channel)
Reaction events captured via Discord bot intents: `GUILD_MESSAGES`, `MESSAGE_REACTION_ADD`

## Verification

1. n8n workflow active and running
2. Board embed posted to Discord channel
3. Reaction → task action confirmed
4. Subscriber notifications delivered on task completion

## Pitfalls (n8n/Discord)

- **Poll frequency**: 5min is default, too frequent (1min) may hit rate limits
- **Message vs thread**: Board message in channel, reactions on that message
- **Emoji uniqueness**: Multiple reactions from same user → deduplicate by user_id + emoji
- **Board refresh**: After reaction action, edit the board message (not new message) to keep context

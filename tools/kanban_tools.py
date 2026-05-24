"""
Kanban Tools — Drewgent internal task store tools.

These tools provide direct access to the DrewgentTaskStore (drewgent_kanban_db).
They mirror the Hermes kanban tool interface but operate on Drewgent's own
SQLite-backed task store rather than Linear.

Tools:
    kanban_create    — Create a task in drewgent_tasks.db
    kanban_complete   — Mark task completed (with hallucination detection)
    kanban_list       — List tasks by status/assignee
    kanban_get        — Get a single task by ID
    kanban_block      — Block a task
    kanban_unblock    — Unblock a task
    kanban_claim      — Claim a task for current worker
    kanban_heartbeat  — Send heartbeat for a running task
    kanban_link       — Create parent-child dependency
    kanban_add_comment — Add a comment to a task

State: ~/.drewgent/state/drewgent_tasks.db
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

# =============================================================================
# Import from drewgent_kanban_db (Phase 1 store)
# =============================================================================

from tools.drewgent_kanban_db import (
    task_create,
    task_complete as _db_task_complete,
    task_get,
    task_list as _db_task_list,
    task_block,
    task_unblock,
    task_claim,
    task_heartbeat as _db_heartbeat,
    task_link,
    task_add_comment as _db_add_comment,
)

# =============================================================================
# Schemas
# =============================================================================

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": "Create a new task in Drewgent's task store. "
                   "If parent_task_ids are provided and any parent is not completed, "
                   "task goes to 'todo' (waiting for parent); otherwise goes to 'ready'.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Task title"},
            "body": {"type": "string", "description": "Task body/description", "default": ""},
            "assignee": {"type": "string", "description": "Assignee name (e.g. 'drewgent')", "default": ""},
            "status": {"type": "string", "description": "Initial status override (default auto: ready or todo)", "default": ""},
            "priority": {"type": "integer", "description": "Priority: 1=Urgent, 2=High, 3=Medium, 4=Low", "default": 3},
            "workspace_kind": {"type": "string", "description": "Workspace type: 'coding', 'research', 'writing', 'general'", "default": "general"},
            "workspace_path": {"type": "string", "description": "Working directory or file path relevant to this task", "default": ""},
            "parent_task_ids": {"type": "array", "items": {"type": "string"}, "description": "Task IDs this task is blocked by (parent tasks)", "default": []},
            "idempotency_key": {"type": "string", "description": "Unique key to prevent duplicate task creation", "default": ""},
            "skills": {"type": "array", "items": {"type": "string"}, "description": "Skill names to load for this task", "default": []},
            "max_runtime_seconds": {"type": "integer", "description": "Max runtime before auto-block (0=no limit)", "default": 0},
            "trigger_source": {"type": "string", "description": "What triggered creation: 'manual', 'activity_logger', 'cron', 'integration_workflow', 'subagent'", "default": "manual"},
            "board": {"type": "string", "description": "Kanban board name (default: 'default')", "default": "default"},
            "mode": {"type": "string", "description": "Task mode: 'design' (AI decomposes then waits for approval) or 'execution' (auto-run). Default: 'execution'", "default": "execution"},
        },
        "required": ["title"],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": "Mark a task as completed. "
                   "Hallucination detection: if you created subtasks (new kanban cards) during work, "
                   "pass their IDs in created_cards — the system verifies each exists in the DB. "
                   "A fake ID will block completion and fire a completion_blocked_hallucination event.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to complete (e.g. 't_abc123def456')"},
            "result": {"type": "string", "description": "Result/output of the task", "default": ""},
            "summary": {"type": "string", "description": "Short summary of what was done", "default": ""},
            "metadata": {"type": "object", "description": "Additional structured metadata about the completion", "default": {}},
            "created_cards": {"type": "array", "items": {"type": "string"}, "description": "Task IDs of subtasks created during this task (for hallucination check)", "default": []},
            "expected_run_id": {"type": "string", "description": "Run ID that is expected to complete this task", "default": ""},
        },
        "required": ["task_id"],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": "List tasks from Drewgent's task store with optional filters.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status: 'todo', 'ready', 'in_progress', 'completed', 'blocked', 'canceled'", "default": ""},
            "assignee": {"type": "string", "description": "Filter by assignee name", "default": ""},
            "board": {"type": "string", "description": "Filter by board name (default: 'default')", "default": ""},
            "limit": {"type": "integer", "description": "Max results to return", "default": 50},
        },
        "required": [],
    },
}

KANBAN_GET_SCHEMA = {
    "name": "kanban_get",
    "description": "Get a single task by ID. Returns all fields including current status, body, assignee, etc.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID (e.g. 't_abc123def456')"},
        },
        "required": ["task_id"],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": "Block a task with a reason. Blocked tasks cannot be claimed until unblocked.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to block"},
            "reason": {"type": "string", "description": "Reason for blocking", "default": ""},
        },
        "required": ["task_id"],
    },
}

KANBAN_GET_EVENTS_SCHEMA = {
    "name": "kanban_get_events",
    "description": "Get event log for a task — history of status changes, claims, completions, blocks, etc.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID"},
            "limit": {"type": "integer", "description": "Max events to return", "default": 50},
        },
        "required": ["task_id"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": "Unblock a blocked task. Task returns to 'todo' status.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to unblock"},
        },
        "required": ["task_id"],
    },
}

KANBAN_CLAIM_SCHEMA = {
    "name": "kanban_claim",
    "description": "Claim a task for the current worker process. "
                   "Sets status='in_progress' and records worker PID and claim expiry. "
                   "Only claims tasks that are 'todo' or 'ready'.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to claim"},
            "ttl_seconds": {"type": "integer", "description": "Claim expiry TTL in seconds (default 3600)", "default": 3600},
        },
        "required": ["task_id"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": "Send a heartbeat for a running task. Updates last_heartbeat_at timestamp. "
                   "Workers should call this every ~5 minutes during long tasks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to heartbeat"},
            "note": {"type": "string", "description": "Progress note (e.g. 'parsing input', 'writing code')", "default": ""},
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": "Create a parent-child dependency between two tasks. "
                   "Child task is automatically set to 'todo' if parent is not yet completed. "
                   "Use cycle detection to prevent circular dependencies.",
    "input_schema": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task ID (this task must complete first)"},
            "child_id": {"type": "string", "description": "Child task ID (blocked by parent)"},
        },
        "required": ["parent_id", "child_id"],
    },
}

KANBAN_ADD_COMMENT_SCHEMA = {
    "name": "kanban_add_comment",
    "description": "Add a comment to a task. Comments are used for human guidance, "
                   "revision requests, and approval/rejection feedback.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to comment on"},
            "author": {"type": "string", "description": "Comment author name", "default": "drewgent"},
            "body": {"type": "string", "description": "Comment text"},
        },
        "required": ["task_id", "body"],
    },
}

# =============================================================================
# Tool Handlers
# =============================================================================

def _ok(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _tool_error(msg: str) -> str:
    """Backwards-compatible alias."""
    return _err(msg)


def handle_kanban_create(args: dict) -> str:
    try:
        result = task_create(
            title=args["title"],
            body=args.get("body"),
            assignee=args.get("assignee") or None,
            priority=args.get("priority"),
            workspace_kind=args.get("workspace_kind"),
            workspace_path=args.get("workspace_path"),
            parent_task_ids=args.get("parent_task_ids") or None,
            idempotency_key=args.get("idempotency_key") or None,
            skills=args.get("skills") or None,
            max_runtime_seconds=args.get("max_runtime_seconds", 0),
            trigger_source=args.get("trigger_source", "manual"),
            board=args.get("board", "default"),
        )
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_create failed")
        return _err(str(e))


def handle_kanban_complete(args: dict) -> str:
    try:
        result = _db_task_complete(
            task_id=args["task_id"],
            result=args.get("result"),
            summary=args.get("summary"),
            metadata=args.get("metadata"),
            created_cards=args.get("created_cards") or None,
            expected_run_id=args.get("expected_run_id") or None,
        )
        if not result.get("ok"):
            return _err(result.get("error", "completion failed"))
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_complete failed")
        return _err(str(e))


def handle_kanban_list(args: dict) -> str:
    try:
        tasks = _db_task_list(
            status=args.get("status") or None,
            assignee=args.get("assignee") or None,
            board=args.get("board") or None,
        )
        # Apply limit
        limit = args.get("limit", 50)
        tasks = tasks[:limit]
        return _ok({"ok": True, "tasks": tasks, "count": len(tasks)})
    except Exception as e:
        logger.exception("kanban_list failed")
        return _err(str(e))


def handle_kanban_get(args: dict) -> str:
    try:
        task = task_get(args["task_id"])
        if task is None:
            return _err(f"Task '{args['task_id']}' not found")
        return _ok({"ok": True, "task": task})
    except Exception as e:
        logger.exception("kanban_get failed")
        return _err(str(e))


def handle_kanban_get_events(args: dict) -> str:
    from drewgent_kanban_db import _get_connection
    try:
        task_id = args["task_id"]
        limit = args.get("limit", 50)
        with _get_connection() as conn:
            rows = conn.execute("""
                SELECT task_id, run_id, kind, payload, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (task_id, limit)).fetchall()
        events = [dict(row) for row in rows]
        return _ok({"ok": True, "task_id": task_id, "events": events, "count": len(events)})
    except Exception as e:
        logger.exception("kanban_get_events failed")
        return _err(str(e))


def handle_kanban_block(args: dict) -> str:
    try:
        result = task_block(args["task_id"], args.get("reason"))
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_block failed")
        return _err(str(e))


def handle_kanban_unblock(args: dict) -> str:
    try:
        result = task_unblock(args["task_id"])
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return _err(str(e))


def handle_kanban_claim(args: dict) -> str:
    try:
        result = task_claim(args["task_id"], args.get("ttl_seconds", 3600))
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_claim failed")
        return _err(str(e))


def handle_kanban_heartbeat(args: dict) -> str:
    try:
        result = _db_heartbeat(args["task_id"], args.get("note"))
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return _err(str(e))


def handle_kanban_link(args: dict) -> str:
    try:
        result = task_link(args["parent_id"], args["child_id"])
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_link failed")
        return _err(str(e))


def handle_kanban_add_comment(args: dict) -> str:
    try:
        result = _db_add_comment(args["task_id"], args.get("author", "drewgent"), args["body"])
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_add_comment failed")
        return _err(str(e))


# =============================================================================
# Registry
# =============================================================================

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=lambda args, **kw: handle_kanban_create(args),
    check_fn=None,
    requires_env=[],
    description="Create a new task in Drewgent's task store",
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=lambda args, **kw: handle_kanban_complete(args),
    check_fn=None,
    requires_env=[],
    description="Mark a task as completed (with hallucination detection for created subtasks)",
    emoji="✅",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=lambda args, **kw: handle_kanban_list(args),
    check_fn=None,
    requires_env=[],
    description="List tasks by status and/or assignee",
    emoji="📑",
)

registry.register(
    name="kanban_get",
    toolset="kanban",
    schema=KANBAN_GET_SCHEMA,
    handler=lambda args, **kw: handle_kanban_get(args),
    check_fn=None,
    requires_env=[],
    description="Get a single task by ID with full details",
    emoji="🔍",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=lambda args, **kw: handle_kanban_block(args),
    check_fn=None,
    requires_env=[],
    description="Block a task with a reason",
    emoji="🚫",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=lambda args, **kw: handle_kanban_unblock(args),
    check_fn=None,
    requires_env=[],
    description="Unblock a blocked task (back to todo)",
    emoji="🔓",
)

registry.register(
    name="kanban_claim",
    toolset="kanban",
    schema=KANBAN_CLAIM_SCHEMA,
    handler=lambda args, **kw: handle_kanban_claim(args),
    check_fn=None,
    requires_env=[],
    description="Claim a task for the current worker (sets in_progress + records PID)",
    emoji="✋",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=lambda args, **kw: handle_kanban_heartbeat(args),
    check_fn=None,
    requires_env=[],
    description="Send heartbeat for a running task (every ~5 min during long tasks)",
    emoji="💓",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=lambda args, **kw: handle_kanban_link(args),
    check_fn=None,
    requires_env=[],
    description="Create parent-child dependency between tasks",
    emoji="🔗",
)

registry.register(
    name="kanban_add_comment",
    toolset="kanban",
    schema=KANBAN_ADD_COMMENT_SCHEMA,
    handler=lambda args, **kw: handle_kanban_add_comment(args),
    check_fn=None,
    requires_env=[],
    description="Add a comment to a task",
    emoji="💬",
)

registry.register(
    name="kanban_get_events",
    toolset="kanban",
    schema=KANBAN_GET_EVENTS_SCHEMA,
    handler=lambda args, **kw: handle_kanban_get_events(args),
    check_fn=None,
    requires_env=[],
    description="Get event log for a task",
    emoji="📋",
)


# =============================================================================
# Notify / Delivery Tools
# =============================================================================


def handle_kanban_board_summary(args: dict) -> str:
    """Send a kanban board summary embed to Discord webhook."""
    try:
        from drewgent_kanban_notify import send_board_summary
        board = args.get("board") or "default"
        ok = send_board_summary(board)
        return _ok({"ok": ok, "board": board, "delivered": ok})
    except Exception as e:
        logger.exception("kanban_board_summary failed")
        return _err(str(e))


KANBAN_BOARD_SUMMARY_SCHEMA = {
    "name": "kanban_board_summary",
    "description": "Send a kanban board summary embed to the configured Discord webhook. "
                   "Use this to manually post the current board state to Discord.",
    "input_schema": {
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Board name to post (default: 'default')", "default": "default"},
        },
        "required": [],
    },
}


def handle_kanban_notify_subscribe_board(args: dict) -> str:
    """Subscribe a Discord channel to all notifications for a board."""
    try:
        board = args.get("board") or "default"
        webhook_url = args.get("webhook_url") or ""
        if not webhook_url:
            return _err("webhook_url is required")
        # Store per-board webhook in boards table
        from drewgent_kanban_db import _get_connection, init_db
        init_db()
        with _get_connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO boards (name, discord_webhook, updated_at)
                   VALUES (?, ?, ?)""",
                (board, webhook_url, datetime.now().isoformat()),
            )
        return _ok({"ok": True, "board": board, "subscribed": True})
    except Exception as e:
        logger.exception("kanban_notify_subscribe_board failed")
        return _err(str(e))


KANBAN_NOTIFY_SUBSCRIBE_BOARD_SCHEMA = {
    "name": "kanban_notify_subscribe_board",
    "description": "Subscribe a Discord channel to kanban board notifications. "
                   "Stores the webhook URL in the boards table so all future events on this board are posted there.",
    "input_schema": {
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Board name to subscribe", "default": "default"},
            "webhook_url": {"type": "string", "description": "Discord webhook URL for this channel"},
        },
        "required": ["webhook_url"],
    },
}


def handle_kanban_notify_unsubscribe_board(args: dict) -> str:
    """Remove the Discord webhook subscription for a board."""
    try:
        board = args.get("board") or "default"
        from drewgent_kanban_db import _get_connection, init_db
        init_db()
        with _get_connection() as conn:
            conn.execute(
                "UPDATE boards SET discord_webhook=NULL WHERE name=?",
                (board,),
            )
        return _ok({"ok": True, "board": board, "unsubscribed": True})
    except Exception as e:
        logger.exception("kanban_notify_unsubscribe_board failed")
        return _err(str(e))


KANBAN_NOTIFY_UNSUBSCRIBE_BOARD_SCHEMA = {
    "name": "kanban_notify_unsubscribe_board",
    "description": "Remove the Discord webhook subscription for a kanban board.",
    "input_schema": {
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Board name to unsubscribe", "default": "default"},
        },
        "required": [],
    },
}

KANBAN_DELETE_SCHEMA = {
    "name": "kanban_delete",
    "description": "Delete a task and all its related data (events, comments, links, runs). "
                  "Cannot be undone. Use only for垃圾 task cleanup.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to delete (e.g. 't_abc123def456')"},
        },
        "required": ["task_id"],
    },
}


def handle_kanban_delete(args: dict) -> str:
    from tools.drewgent_kanban_db import task_delete as _db_task_delete
    try:
        result = _db_task_delete(task_id=args["task_id"])
        if not result.get("ok"):
            return _err(result.get("error", "delete failed"))
        return _ok(result)
    except Exception as e:
        logger.exception("kanban_delete failed")
        return _err(str(e))


registry.register(
    name="kanban_board_summary",
    toolset="kanban",
    schema=KANBAN_BOARD_SUMMARY_SCHEMA,
    handler=lambda args, **kw: handle_kanban_board_summary(args),
    check_fn=None,
    requires_env=[],
    description="Send a kanban board summary embed to Discord webhook",
    emoji="📊",
)

registry.register(
    name="kanban_notify_subscribe_board",
    toolset="kanban",
    schema=KANBAN_NOTIFY_SUBSCRIBE_BOARD_SCHEMA,
    handler=lambda args, **kw: handle_kanban_notify_subscribe_board(args),
    check_fn=None,
    requires_env=[],
    description="Subscribe a Discord channel to kanban board notifications",
    emoji="🔔",
)

registry.register(
    name="kanban_notify_unsubscribe_board",
    toolset="kanban",
    schema=KANBAN_NOTIFY_UNSUBSCRIBE_BOARD_SCHEMA,
    handler=lambda args, **kw: handle_kanban_notify_unsubscribe_board(args),
    check_fn=None,
    requires_env=[],
    description="Remove Discord webhook subscription for a kanban board",
    emoji="🔕",
)

registry.register(
    name="kanban_delete",
    toolset="kanban",
    schema=KANBAN_DELETE_SCHEMA,
    handler=lambda args, **kw: handle_kanban_delete(args),
    check_fn=None,
    requires_env=[],
    description="Delete a task and all its related data (cascade delete)",
    emoji="🗑️",
)
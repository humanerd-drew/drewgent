"""
drewgent_kanban_notify — Discord notification delivery for kanban events.

Fire notifications to Discord when tasks complete/block/unblock.
Uses Discord webhooks (HTTP POST) so it works from cron/worker context
without needing the gateway's platform adapters.

Board webhook URLs are stored in the boards.discord_webhook column.
If no per-board webhook is set, falls back to the kanban_notify.default_webhook_url.
If neither is configured, silently skips delivery (no error).
"""
import json
import logging
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from drewgent_kanban_db import _get_connection, init_db

logger = logging.getLogger(__name__)

DEFAULT_WEBHOOK_URL: Optional[str] = None  # Set on first load from config


def _load_default_webhook() -> Optional[str]:
    """Load default Discord webhook URL from config.yaml on first call."""
    global DEFAULT_WEBHOOK_URL
    if DEFAULT_WEBHOOK_URL is not None:
        return DEFAULT_WEBHOOK_URL
    try:
        import yaml
        config_path = Path.home() / ".drewgent" / "config.yaml"
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            # kanban_notify.default_webhook_url or platforms.discord.webhook_url
            DEFAULT_WEBHOOK_URL = (
                cfg.get("kanban_notify", {}).get("default_webhook_url")
                or cfg.get("platforms", {}).get("discord", {}).get("webhook_url")
                or None
            )
    except Exception:
        DEFAULT_WEBHOOK_URL = None
    return DEFAULT_WEBHOOK_URL


def _get_board_webhook_url(board: str) -> Optional[str]:
    """Get webhook URL for a specific board, falling back to default."""
    init_db()
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT discord_webhook FROM boards WHERE name=?",
            (board,)
        ).fetchone()
    board_webhook = row["discord_webhook"] if row else None
    return board_webhook or _load_default_webhook()


def _get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a task by ID."""
    init_db()
    with _get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def _format_message(event_kind: str, task: Dict[str, Any]) -> Dict[str, Any]:
    """Format a Discord embed message for a kanban event."""

    EMOJI = {
        "completed": "✅",
        "blocked": "🚫",
        "unblocked": "🔓",
        "created": "🆕",
        "claimed": "🏃",
        "reclaimed": "♻️",
    }
    COLOR = {
        "completed": 0x00C853,
        "blocked": 0xFF1744,
        "unblocked": 0x00BCD4,
        "created": 0x5BC0EB,
        "claimed": 0xFFB300,
        "reclaimed": 0xFF6F00,
    }

    emoji = EMOJI.get(event_kind, "📋")
    color = COLOR.get(event_kind, 0x5BC0EB)
    title = task.get("title", "(no title)")
    task_id = task.get("id", "")
    board = task.get("board", "default")
    result = task.get("result") or ""

    embed: Dict[str, Any] = {
        "title": f"{emoji} Task {event_kind}: {title}",
        "color": color,
        "fields": [
            {"name": "Task ID", "value": task_id, "inline": True},
            {"name": "Board", "value": board, "inline": True},
        ],
        "footer": {"text": f"Drewgent Kanban • {datetime.now().strftime('%H:%M:%S')}"},
    }

    if result:
        embed["description"] = result[:500]  # truncate long results

    return {"embeds": [embed]}


def send_notification(
    task_id: str,
    event_kind: str,
    board: Optional[str] = None,
) -> bool:
    """
    Send a Discord notification for a kanban event.

    Args:
        task_id: the task that triggered the event
        event_kind: 'completed', 'blocked', 'unblocked', 'created', 'claimed', 'reclaimed'
        board: board name (auto-detected from task if not provided)

    Returns:
        True if notification was sent or skipped (no webhook), False on error.
    """
    # Resolve board from task if not provided
    if board is None:
        task = _get_task(task_id)
        if task is None:
            logger.warning("notify: task %s not found", task_id)
            return False
        board = task.get("board") or "default"
        task_title = task.get("title", "(no title)")
    else:
        task = _get_task(task_id)
        task_title = task.get("title", "(no title)") if task else "(no title)"

    webhook_url = _get_board_webhook_url(board)
    if not webhook_url:
        logger.debug("notify: no webhook configured for board=%s, skipping", board)
        return True  # Skipped, not failed

    if task is None:
        logger.warning("notify: task %s not found", task_id)
        return False

    payload = _format_message(event_kind, task)
    body = json.dumps(payload).encode("utf-8")

    try:
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                logger.warning(
                    "notify: Discord returned status=%d for task=%s event=%s",
                    resp.status, task_id, event_kind,
                )
                return False
        logger.info(
            "notify: sent %s for task=%s (%s) to board=%s",
            event_kind, task_id, task_title, board,
        )
        return True
    except Exception as e:
        logger.error(
            "notify: failed to send %s for task=%s: %s",
            event_kind, task_id, e,
        )
        return False


def send_board_summary(board: str) -> bool:
    """
    Send a full kanban board summary to Discord.
    Called by the kanban-dashboard cron job to post the board.
    """
    webhook_url = _get_board_webhook_url(board)
    if not webhook_url:
        return False

    init_db()
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, status, assignee FROM tasks WHERE board=? ORDER BY status, priority, created_at",
            (board,),
        ).fetchall()

    if not rows:
        return True  # Nothing to report

    fields = []
    by_status: Dict[str, List] = {}
    for row in rows:
        status = row["status"]
        by_status.setdefault(status, []).append(row)

    for status in ("todo", "ready", "in_progress", "blocked"):
        items = by_status.get(status, [])
        if items:
            value = "\n".join(
                f"`{t['id'][:12]}` — {t['title'][:40]}" for t in items
            )
            fields.append({"name": f"{status.upper()} ({len(items)})", "value": value, "inline": False})

    embed = {
        "title": f"📋 Kanban Board: {board}",
        "color": 0x5BC0EB,
        "fields": fields,
        "footer": {"text": f"Drewgent Kanban • {datetime.now().strftime('%Y-%m-%d %H:%M')}"},
    }
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")

    try:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        logger.error("board_summary: failed for board=%s: %s", board, e)
        return False

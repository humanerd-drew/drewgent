"""
DrewgentTaskStore — SQLite-backed persistent task store.

Provides a lightweight task store for Drewgent integration workflow tracking.
Follows the drewgent-kanban-implementation-plan.md spec (Phase 1 MVP).

State file: ~/.drewgent/P2-hippocampus/kanban/state/drewgent_tasks.db
(P2-hippocampus/kanban/ is the canonical location — kanban is part of brain structure)

Schema:
    boards: id, name, description, created_at
    tasks: id, title, body, assignee, status, priority, board, ...
           created_by, created_at, started_at, completed_at,
           workspace_kind, workspace_path, claim_lock, claim_expires,
           result, consecutive_failures, last_failure_error,
           worker_pid, max_runtime_seconds, last_heartbeat_at,
           idempotency_key, skills, max_retries, tenant,
           integration_workflow_id, trigger_source, parent_session_id

    task_links: parent_id, child_id (PRIMARY KEY)
    task_events: task_id, run_id, kind, payload, created_at
    task_comments: task_id, author, body, created_at
    task_runs: task_id, profile, status, claim_lock, claim_expires,
               worker_pid, started_at, ended_at, outcome, summary, metadata, error
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from drewgent_constants import get_drewgent_home

logger = logging.getLogger(__name__)

# =============================================================================
# Database Setup
# =============================================================================

def _get_db_path() -> Path:
    """Return path to the Drewgent tasks DB.

    P2-hippocampus/kanban/state/ is the canonical location.
    This places kanban state squarely inside Drewgent's brain structure,
    not as an orphan in state/.
    """
    home = get_drewgent_home()
    db_path = home / "P2-hippocampus" / "kanban" / "state" / "drewgent_tasks.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


@contextmanager
def _get_connection():
    """Yield a sqlite3 connection with row factory."""
    db = _get_db_path()
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initialize the task store schema. Idempotent."""
    with _get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            body            TEXT,
            assignee        TEXT,
            status          TEXT NOT NULL DEFAULT 'todo',
            priority        INTEGER,
            created_by      TEXT,
            created_at      TEXT NOT NULL,
            started_at      TEXT,
            completed_at    TEXT,
            workspace_kind  TEXT,
            workspace_path  TEXT,
            claim_lock      TEXT,
            claim_expires   TEXT,
            result          TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            last_failure_error TEXT,
            worker_pid      INTEGER,
            max_runtime_seconds INTEGER DEFAULT 0,
            last_heartbeat_at TEXT,
            idempotency_key TEXT,
            skills          TEXT,
            max_retries     INTEGER DEFAULT 3,
            tenant          TEXT,
            integration_workflow_id TEXT,
            trigger_source  TEXT DEFAULT 'subagent',
            parent_session_id TEXT,
            board           TEXT NOT NULL DEFAULT 'default',
            mode            TEXT NOT NULL DEFAULT 'execution'
        );

        CREATE TABLE IF NOT EXISTS boards (
            id          TEXT PRIMARY KEY,
            name        TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_links (
            parent_id TEXT NOT NULL,
            child_id  TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id),
            FOREIGN KEY (parent_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (child_id)  REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            run_id     TEXT,
            kind       TEXT NOT NULL,
            payload    TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            author     TEXT NOT NULL,
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL,
            profile     TEXT,
            status      TEXT NOT NULL,
            claim_lock  TEXT,
            claim_expires TEXT,
            worker_pid  INTEGER,
            started_at  TEXT,
            ended_at    TEXT,
            outcome     TEXT,
            summary     TEXT,
            metadata    TEXT,
            error       TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
        CREATE INDEX IF NOT EXISTS idx_tasks_integration_wf ON tasks(integration_workflow_id);
        CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_board ON tasks(board);
        """)

        # Migration: add kanban_notify_subs (separate check with fresh conn to avoid
        # cursor/state pollution from executescript)
        try:
            with _get_connection() as mconn:
                mconn.execute("SELECT subscriber FROM kanban_notify_subs LIMIT 1")
        except sqlite3.OperationalError:
            with _get_connection() as mconn:
                mconn.execute("""
                    CREATE TABLE IF NOT EXISTS kanban_notify_subs (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id     TEXT NOT NULL,
                        platform    TEXT NOT NULL,
                        chat_id     TEXT NOT NULL,
                        thread_id   TEXT,
                        subscriber  TEXT NOT NULL,
                        created_at  TEXT NOT NULL,
                        UNIQUE(task_id, platform, chat_id, subscriber)
                    )
                """)
                try:
                    mconn.execute("CREATE INDEX IF NOT EXISTS idx_notify_task ON kanban_notify_subs(task_id)")
                except sqlite3.OperationalError:
                    pass


# =============================================================================
# Task CRUD
# =============================================================================

def task_create(
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    status: str = "todo",
    priority: Optional[int] = None,
    created_by: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    skills: Optional[List[str]] = None,
    max_runtime_seconds: int = 0,
    trigger_source: str = "subagent",
    integration_workflow_id: Optional[str] = None,
    parent_session_id: Optional[str] = None,
parent_task_ids: Optional[List[str]] = None,
    board: str = "default",
    mode: str = "execution",
) -> Dict[str, Any]:
    """
    Create a new task and optionally link to parent tasks.

    Args:
        mode: 'design' (AI decomposes then waits for approval) or 'execution' (decompose then auto-run).
              Design mode tasks go to 'design' status and wait for user approval before spawning workers.
        board: kanban board name (default: 'default'). Creates board if not exists.

    Returns dict with: ok=True, task_id, title, status
    """
    # Validate mode
    if mode not in ("design", "execution"):
        return {"ok": False, "error": f"Invalid mode: {mode}. Must be 'design' or 'execution'."}

    # Design mode: status='design', no auto-dispatch
    # Execution mode: status='ready' (or 'todo' if parent not done)
    if mode == "design":
        status = "design"
    else:
        status = "ready"
        # Idempotency check
        if idempotency_key:
            with _get_connection() as conn:
                row = conn.execute(
                    "SELECT id, status FROM tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return {"ok": True, "task_id": row["id"], "status": row["status"], "idempotent": True}
        # Parent dependency check
        if parent_task_ids:
            with _get_connection() as conn:
                undone = []
                for pid in parent_task_ids:
                    row = conn.execute(
                        "SELECT status FROM tasks WHERE id = ?", (pid,)
                    ).fetchone()
                    if not row or row["status"] not in ("completed", "canceled", "done"):
                        undone.append(row["status"] if row else "missing")
                if undone:
                    status = "todo"  # still blocked by parent(s)

    init_db()
    task_id = f"t_{uuid.uuid4().hex[:12]}"
    now = datetime.now().isoformat()

    with _get_connection() as conn:
        # Ensure board exists (create if not)
        conn.execute(
            "INSERT OR IGNORE INTO boards (id, name, created_at) VALUES (?, ?, ?)",
            (board, board, now),
        )
        conn.execute(
            """INSERT INTO tasks (
                id, title, body, assignee, status, priority,
                created_by, created_at, workspace_kind, workspace_path,
                idempotency_key, skills, max_runtime_seconds,
                trigger_source, integration_workflow_id, parent_session_id, board, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, title, body, assignee, status, priority,
                created_by or "drewgent", now,
                workspace_kind, workspace_path,
                idempotency_key, json.dumps(skills) if skills else None,
                max_runtime_seconds,
                trigger_source, integration_workflow_id, parent_session_id, board, mode,
            ),
        )

        # Insert parent-child links
        if parent_task_ids:
            for pid in parent_task_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (pid, task_id),
                )

        # Record event
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, None, "created", json.dumps({"title": title, "status": status}), now),
        )

    logger.debug("Task created: %s '%s' (wf=%s)", task_id, title, integration_workflow_id)

    # Emit kanban brain signal (P2-hippocampus brain integration)
    try:
        from agent.brain_signals import get_signal_emitter
        emitter = get_signal_emitter()
        if emitter:
            emitter.kanban_task_created(
                task_id=task_id,
                title=title,
                board=board,
                trigger=trigger_source,
            )
    except Exception:
        pass  # Brain signals are best-effort, don't block task creation

    return {"ok": True, "task_id": task_id, "title": title, "status": status}


def _recompute_ready_for_children(conn: sqlite3.Connection, completed_task_id: str) -> List[Dict[str, Any]]:
    """
    After a task completes, find children that were waiting on this parent.
    If all of a child's parents are now completed, promote child: todo → ready.
    Returns list of promoted children.
    """
    promoted = []

    # Find all children of the completed task (via task_links)
    child_rows = conn.execute("""
        SELECT child_id FROM task_links WHERE parent_id = ?
    """, (completed_task_id,)).fetchall()

    for child_row in child_rows:
        child_id = child_row["child_id"]

        # Get child's current state (only care about 'todo' children)
        child_task = conn.execute(
            "SELECT status, title FROM tasks WHERE id = ?", (child_id,)
        ).fetchone()
        if not child_task or child_task["status"] != "todo":
            continue

        # Get ALL parent statuses for this child
        parent_rows = conn.execute("""
            SELECT t.status FROM task_links ll
            JOIN tasks t ON t.id = ll.parent_id
            WHERE ll.child_id = ?
        """, (child_id,)).fetchall()

        all_done = all(p["status"] in ("completed", "canceled", "done") for p in parent_rows)
        if all_done:
            conn.execute(
                "UPDATE tasks SET status='ready' WHERE id=? AND status='todo'",
                (child_id,),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (child_id, None, "promoted", json.dumps({"parent_id": completed_task_id}), datetime.now().isoformat()),
            )
            promoted.append({"task_id": child_id, "title": child_task["title"]})

    return promoted


def task_complete(
    task_id: str,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    created_cards: Optional[List[str]] = None,
    expected_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Mark a task as completed.

    Side effects:
    - Hallucination detection on created_cards (blocks completion if fake ID found)
    - Prose scan for t_<hex> patterns in result/summary (flags unresolved refs)
    - Parent-child promotion: when a parent completes, children waiting on it
      are promoted from todo→ready if all their parents are now done

    Returns dict with: ok, error (if hallucination detected).
    """
    init_db()

    now = datetime.now().isoformat()

    # --- Hallucination detection: created_cards DB verification ---
    if created_cards:
        with _get_connection() as conn:
            for card_id in created_cards:
                row = conn.execute("SELECT id FROM tasks WHERE id = ?", (card_id,)).fetchone()
                if not row:
                    logger.warning("Hallucination detected: task_id %s does not exist", card_id)
                    # Emit P0-brainstem kanban.hallucination_blocked signal
                    try:
                        from agent.brain_signals import get_signal_emitter
                        emitter = get_signal_emitter()
                        if emitter:
                            emitter.kanban_hallucination_blocked(task_id, [card_id])
                    except Exception:
                        pass
                    return {
                        "ok": False,
                        "error": f"completion_blocked_hallucination: '{card_id}' not found in task store",
                        "fake_id": card_id,
                    }

    with _get_connection() as conn:
        # Get current task state for event record
        row = conn.execute(
            "SELECT status, integration_workflow_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Task '{task_id}' not found"}

        old_status = row["status"]
        wf_id = row["integration_workflow_id"]

        conn.execute(
            """UPDATE tasks SET
                status=?, completed_at=?, result=?
                WHERE id=?""",
            ("completed", now, result, task_id),
        )

        # Record completion event
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, expected_run_id, "completed", json.dumps({"summary": summary, "metadata": metadata}), now),
        )

        # --- Prose hallucination scan: t_<hex> patterns in result/summary ---
        prose = f"{result or ''} {summary or ''}"
        import re
        t_pattern_ids = re.findall(r'\bt_([0-9a-f]{12})\b', prose)
        unresolved = []
        for candidate in t_pattern_ids:
            referenced_id = f"t_{candidate}"
            exists = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (referenced_id,)
            ).fetchone()
            if not exists:
                unresolved.append(referenced_id)

        if unresolved:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, expected_run_id, "unresolved_refs",
                 json.dumps({"unresolved_ids": unresolved, "text": prose[:500]}), now),
            )
            logger.warning("Task %s has unresolved task refs in prose: %s", task_id, unresolved)

        # --- Parent-child promotion: promote waiting children to ready ---
        promoted = _recompute_ready_for_children(conn, task_id)

    result_dict: Dict[str, Any] = {"ok": True, "task_id": task_id, "status": "completed"}
    if promoted:
        result_dict["promoted"] = [p["task_id"] for p in promoted]
    if unresolved:
        result_dict["unresolved_refs"] = unresolved

    logger.debug("Task completed: %s (wf=%s) — promoted %d children", task_id, wf_id, len(promoted))

    # ── Fire notification to subscribers ─────────────────────────────────────
    notify_task_event(task_id, "completed", {"result": result, "summary": summary})

    # Emit kanban.task.completed brain signal (P2-hippocampus brain integration)
    try:
        from agent.brain_signals import get_signal_emitter
        emitter = get_signal_emitter()
        if emitter:
            emitter.kanban_task_completed(
                task_id=task_id,
                board=row["board"] if "board" in row else "default",
                result=result or "",
            )
    except Exception:
        pass

    return result_dict


def task_get(task_id: str) -> Optional[Dict[str, Any]]:
    """Get task by ID. Returns None if not found."""
    init_db()
    with _get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return dict(row)


def task_list(
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    board: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List tasks with optional filters."""
    init_db()
    with _get_connection() as conn:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []
        if board:
            query += " AND board = ?"
            params.append(board)
        if status:
            query += " AND status = ?"
            params.append(status)
        if assignee:
            query += " AND assignee = ?"
            params.append(assignee)
        query += " ORDER BY created_at DESC LIMIT 100"
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def task_block(task_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """Block a task."""
    init_db()
    now = datetime.now().isoformat()
    with _get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, None, "blocked", json.dumps({"reason": reason}), now),
        )
    notify_task_event(task_id, "blocked", {"reason": reason})

    # Emit kanban.task.blocked brain signal (P2-hippocampus brain integration)
    try:
        from agent.brain_signals import get_signal_emitter
        emitter = get_signal_emitter()
        if emitter:
            emitter.kanban_task_blocked(task_id, reason or "unspecified")
    except Exception:
        pass

    return {"ok": True, "task_id": task_id, "status": "blocked"}


def task_unblock(task_id: str) -> Dict[str, Any]:
    """Unblock a task — set back to ready (claimable by dispatcher)."""
    init_db()
    now = datetime.now().isoformat()
    with _get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=? AND status='blocked'",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, None, "unblocked", "{}", now),
        )
    notify_task_event(task_id, "unblocked")
    return {"ok": True, "task_id": task_id, "status": "ready"}


def task_link(parent_id: str, child_id: str) -> Dict[str, Any]:
    """Create a parent-child dependency link.

    If the parent is NOT yet done, the child is demoted from 'ready' to 'todo'
    to prevent it from being claimed before its dependency is satisfied.
    This matches Hermes behavior: 'child is ready but parent not done → child: todo'.

    Cycle detection: prevents creating circular dependencies via DFS.
    A cycle exists if child_id can already reach parent_id via existing links
    (then linking parent_id → child_id would close the loop).
    """
    init_db()
    with _get_connection() as conn:
        # ── Cycle detection via DFS ─────────────────────────────────────────
        # Check if child_id can already reach parent_id via existing links.
        # If so, linking parent_id → child_id would create a cycle:
        #   child_id → ... → parent_id → child_id
        visited = set()
        stack = [child_id]
        while stack:
            current = stack.pop()
            if current == parent_id:
                return {
                    "ok": False,
                    "error": "cycle detected: linking would create circular dependency",
                    "parent_id": parent_id,
                    "child_id": child_id,
                }
            if current in visited:
                continue
            visited.add(current)
            # Find all children of current (current is parent in task_links)
            rows = conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ?",
                (current,),
            ).fetchall()
            for row in rows:
                stack.append(row["child_id"])
        # ─────────────────────────────────────────────────────────────────────

        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id),
        )
        # Demote child to 'todo' if parent is not yet done
        parent_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (parent_id,)
        ).fetchone()
        if parent_row and parent_row["status"] not in ("completed", "canceled", "done"):
            conn.execute(
                "UPDATE tasks SET status='todo' WHERE id=? AND status='ready'",
                (child_id,),
            )
    return {"ok": True, "parent_id": parent_id, "child_id": child_id}


def task_add_comment(task_id: str, author: str, body: str) -> Dict[str, Any]:
    """Add a comment to a task."""
    init_db()
    now = datetime.now().isoformat()
    with _get_connection() as conn:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, author, body, now),
        )
    return {"ok": True, "task_id": task_id}


def task_delete(task_id: str) -> Dict[str, Any]:
    """Delete a task and all its related data (cascade delete)."""
    init_db()
    with _get_connection() as conn:
        # Verify task exists
        row = conn.execute("SELECT id, title, status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Task '{task_id}' not found"}
        title = row["title"]
        status = row["status"]

        # Cascade delete related records
        conn.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM task_comments WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
        # Remove from task_links (both as parent and child)
        conn.execute("DELETE FROM task_links WHERE parent_id=? OR child_id=?", (task_id, task_id))
        # Delete the task itself
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    return {"ok": True, "task_id": task_id, "title": title, "status": status}


def task_claim(task_id: str, ttl_seconds: int = 3600) -> Dict[str, Any]:
    """Claim a task for the current worker."""
    init_db()
    now = datetime.now().isoformat()
    expires = (datetime.now() + timedelta(seconds=ttl_seconds)).isoformat()
    pid = os.getpid()
    with _get_connection() as conn:
        conn.execute(
            """UPDATE tasks SET
                status='in_progress', claim_lock=?, claim_expires=?, worker_pid=?
                WHERE id=? AND (status='todo' OR status='ready')""",
            (f"worker:{pid}", expires, pid, task_id),
        )
    return {"ok": True, "task_id": task_id, "claim_expires": expires}


def task_heartbeat(task_id: str, note: Optional[str] = None) -> Dict[str, Any]:
    """Update last_heartbeat_at for a task."""
    init_db()
    now = datetime.now().isoformat()
    with _get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at=? WHERE id=?",
            (now, task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, None, "heartbeat", json.dumps({"note": note}), now),
        )
    return {"ok": True, "task_id": task_id, "last_heartbeat_at": now}


# =============================================================================
# Integration Workflow Integration helpers
# =============================================================================

def create_integration_workflow_task(
    workflow_id: str,
    integration_type: str,
    target_name: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a task for an integration workflow.
    Called from signal_processor when integration.start fires.

    Returns task_create result dict.
    """
    return task_create(
        title=f"[{integration_type}] {target_name}",
        body=f"Integration workflow for {integration_type} '{target_name}'",
        assignee="drewgent",
        status="in_progress",
        trigger_source="integration_workflow",
        integration_workflow_id=workflow_id,
        parent_session_id=session_id,
    )


def complete_integration_workflow_task(
    workflow_id: str,
    result: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Complete the task associated with an integration workflow.
    Called from signal_processor when integration.complete fires.

    Finds the task by integration_workflow_id and marks it completed.
    Returns task_complete result dict.
    """
    init_db()
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE integration_workflow_id = ? AND status != 'completed'",
            (workflow_id,),
        ).fetchone()
        if not row:
            logger.debug("No open task found for workflow_id=%s", workflow_id)
            return {"ok": False, "error": f"No open task for workflow '{workflow_id}'"}
        task_id = row["id"]

    return task_complete(task_id, result=result, metadata=metadata)


# =============================================================================
# Notifier (Gateway Notifier — kanban_notify_subs)
# =============================================================================

def notify_subscribe(
    task_id: str,
    platform: str = "discord",
    chat_id: str = "",
    thread_id: Optional[str] = None,
    subscriber: str = "",
) -> Dict[str, Any]:
    """
    Subscribe a platform subscriber to task completion notifications.

    Args:
        task_id: kanban task ID
        platform: 'discord', 'telegram', etc.
        chat_id: platform-specific chat/channel ID
        thread_id: optional thread/topic ID
        subscriber: user or channel identifier

    Returns: {"ok": True, "subscription_id": N}
    """
    init_db()
    now = datetime.now().isoformat()
    with _get_connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO kanban_notify_subs
               (task_id, platform, chat_id, thread_id, subscriber, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, platform, chat_id, thread_id, subscriber, now),
        )
        row = conn.execute(
            "SELECT id FROM kanban_notify_subs WHERE task_id=? AND platform=? AND chat_id=?",
            (task_id, platform, chat_id),
        ).fetchone()
    return {"ok": True, "subscription_id": row["id"] if row else None}


def notify_unsubscribe(task_id: str, platform: str, chat_id: str) -> Dict[str, Any]:
    """Remove a notification subscription."""
    init_db()
    with _get_connection() as conn:
        conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id=? AND platform=? AND chat_id=?",
            (task_id, platform, chat_id),
        )
    return {"ok": True}


def notify_list(task_id: str) -> List[Dict[str, Any]]:
    """List all subscribers for a task notification."""
    init_db()
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id=?", (task_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def notify_task_event(task_id: str, event_kind: str, payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Fire a notification event for a task.

    Called after task_complete, task_block, etc.
    Returns list of subscribers that were notified.

    Args:
        task_id: the task that triggered the event
        event_kind: 'completed', 'blocked', 'crashed', 'unblocked'
        payload: additional event data

    Returns: list of subscriber dicts that received notifications
    """
    init_db()
    subscribers = notify_list(task_id)
    results = []

    for sub in subscribers:
        # Log the notification event
        with _get_connection() as conn:
            conn.execute(
                """INSERT INTO task_events
                   (task_id, run_id, kind, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    task_id,
                    None,
                    f"notify_{event_kind}",
                    json.dumps({"subscriber": sub, "payload": payload}),
                    datetime.now().isoformat(),
                ),
            )
        results.append(sub)

    if results:
        logger.info(
            "notify_task_event: task=%s kind=%s subscribers=%d",
            task_id, event_kind, len(results),
        )

    # --- Deliver to Discord via webhook ---
    try:
        from drewgent_kanban_notify import send_notification
        send_notification(task_id, event_kind)
    except Exception:
        pass  # Non-blocking delivery

    return results


# =============================================================================
# Worker Result Handoff
# =============================================================================

def parent_results(task_id: str) -> List[Dict[str, Any]]:
    """
    Get completed parent tasks and their results for a given child task.
    Used by workers to read parent results before executing next step.

    Returns: [{"parent_id": "...", "title": "...", "result": "...", "completed_at": "..."}, ...]
    Only includes parents with status completed/canceled/done.
    """
    init_db()
    with _get_connection() as conn:
        rows = conn.execute("""
            SELECT t.id AS parent_id, t.title, t.result, t.completed_at
            FROM task_links ll
            JOIN tasks t ON t.id = ll.parent_id
            WHERE ll.child_id = ?
              AND t.status IN ('completed', 'canceled', 'done')
            ORDER BY t.completed_at ASC
        """, (task_id,)).fetchall()
    return [dict(row) for row in rows]


# =============================================================================
# Dispatcher (Phase 2)
# =============================================================================

def _reclaim_stale_tasks(conn: sqlite3.Connection, failure_limit: int = 3) -> List[Dict[str, Any]]:
    """
    Find in_progress tasks whose claim has expired or whose worker died.
    Reset them to todo and increment consecutive_failures.
    Returns list of reclaimed task dicts.
    """
    now = datetime.now().isoformat()
    reclaimed = []

    rows = conn.execute("""
        SELECT id, title, worker_pid, claim_expires, consecutive_failures,
               started_at, max_runtime_seconds
        FROM tasks
        WHERE status = 'in_progress'
    """).fetchall()

    for row in rows:
        task_id = row["id"]
        expired = row["claim_expires"] and row["claim_expires"] < now
        worker_dead = row["worker_pid"] and not _is_pid_alive(row["worker_pid"])

        # Watchdog: max_runtime_seconds exceeded since task started
        runtime_exceeded = False
        if row["max_runtime_seconds"] and row["max_runtime_seconds"] > 0 and row["started_at"]:
            from datetime import datetime as dt
            started = dt.fromisoformat(row["started_at"])
            elapsed = (dt.now() - started).total_seconds()
            runtime_exceeded = elapsed > row["max_runtime_seconds"]

        if expired or worker_dead or runtime_exceeded:
            failures = row["consecutive_failures"] + 1
            reason = "expired" if expired else ("worker_dead" if worker_dead else "runtime_exceeded")
            if failures >= failure_limit:
                conn.execute(
                    """UPDATE tasks SET status='blocked',
                        consecutive_failures=?, last_failure_error=?
                        WHERE id=?""",
                    (failures, f"reclaim_{reason}", task_id),
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, None, "blocked", json.dumps({"reason": f"reclaim_{reason}"}), now),
                )
            else:
                conn.execute(
                    """UPDATE tasks SET status='todo',
                        consecutive_failures=?,
                        claim_lock=NULL, claim_expires=NULL, worker_pid=NULL
                        WHERE id=?""",
                    (failures, task_id),
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, None, "reclaimed", json.dumps({"failures": failures, "reason": reason}), now),
                )
            reclaimed.append({"task_id": task_id, "reason": reason})
            # Emit kanban.worker.reclaimed brain signal (P2-hippocampus brain integration)
            try:
                from agent.brain_signals import get_signal_emitter
                emitter = get_signal_emitter()
                if emitter:
                    emitter.kanban_worker_reclaimed(task_id, reason)
            except Exception:
                pass
            # Promote children whose parents just became available again
            _recompute_ready_for_children(conn, task_id)

    return reclaimed


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    import signal
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _spawn_worker_for_task(task_id: str) -> Optional[Dict[str, Any]]:
    """
    Spawn a background worker for a claimed task using ACP subprocess transport.
    Sets KANBAN_TASK_ID env var so the worker knows what to execute.
    Returns dict with spawned task info, or None on failure.
    """
    import signal
    import subprocess

    task = task_get(task_id)
    if not task:
        return None

    # Find the drewgent CLI entry point
    venv_python = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")
    if not Path(venv_python).exists():
        venv_python = "python"  # fallback to PATH

    env = os.environ.copy()
    env["KANBAN_TASK_ID"] = task_id
    env["KANBAN_WORKER_MODE"] = "1"

    # Include parent task results if this task has parents
    parent_info = ""
    try:
        from drewgent_kanban_db import parent_results
        parents = parent_results(task_id)
        if parents:
            parent_info = "\nParent task results (for handoff):\n"
            for p in parents:
                parent_info += f"  - {p['parent_id']} ({p.get('title', 'unknown')}): {p.get('result', '(no result)')}\n"
    except Exception:
        pass

    prompt = (
        f"You are working on task {task_id}: {task['title']}\n"
        f"Body: {task.get('body') or '(no description)'}\n"
        f"Priority: {task.get('priority')}\n"
        f"Workspace: {task.get('workspace_kind')} at {task.get('workspace_path') or 'default'}\n"
        f"Trigger: {task.get('trigger_source')}\n"
        f"{parent_info}\n"
        f"Execute the task. Send periodic kanban_heartbeat(task_id=\"{task_id}\", note=\"...\") every few minutes. "
        f"When done, call kanban_complete(task_id=\"{task_id}\", result=..., summary=...)."
    )

    try:
        # Use subprocess to run drewgent CLI in background
        proc = subprocess.Popen(
            [venv_python, "-m", "drewgent_cli.main", "acp", "--stdio",
             "--model", "claude-sonnet-4"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        # Send initial prompt
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.write(b"\n[SESSION_END]\n")
        proc.stdin.flush()
        proc.stdin.close()  # close pipe so worker can start processing

        return {
            "task_id": task_id,
            "pid": proc.pid,
            "started_at": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.warning("Failed to spawn worker for task %s: %s", task_id, e)
        return None


def dispatch_once(
    board: str = "default",
    max_spawn: int = 3,
    failure_limit: int = 3,
) -> Dict[str, Any]:
    """
    Run one dispatcher tick: reclaim stale tasks, claim ready tasks,
    and spawn workers for them.

    Args:
        board: kanban board name (currently only 'default' is supported)
        max_spawn: maximum concurrent workers to spawn per tick
        failure_limit: consecutive failures before auto-blocking a task

    Returns:
        {
            "ok": True,
            "reclaimed": [...],   # tasks that were stale and reset to todo
            "claimed": [...],     # tasks claimed this tick
            "spawned": [...],    # workers successfully spawned
            "skipped": [...],     # tasks skipped (already claimed, etc.)
        }
    """
    init_db()
    now = datetime.now().isoformat()
    result = {
        "ok": True,
        "reclaimed": [],
        "claimed": [],
        "spawned": [],
        "skipped": [],
    }

    with _get_connection() as conn:
        # Step 1: Reclaim stale tasks
        result["reclaimed"] = _reclaim_stale_tasks(conn, failure_limit)

        # Step 2: Find ready tasks for this board and claim up to max_spawn
        # Skip design-mode tasks — they wait for human approval before execution
        rows = conn.execute("""
            SELECT id, title, assignee FROM tasks
            WHERE board = ? AND status = 'ready' AND (mode IS NULL OR mode != 'design')
            ORDER BY priority ASC NULLS LAST, created_at ASC
            LIMIT ?
        """, (board, max_spawn,)).fetchall()

        spawned_count = 0
        for row in rows:
            task_id = row["id"]

            # Atomic claim: UPDATE only if still 'ready'
            pid = os.getpid()
            expires = (datetime.now() + timedelta(seconds=3600)).isoformat()
            updated = conn.execute("""
                UPDATE tasks SET
                    status='in_progress',
                    claim_lock=?, claim_expires=?, worker_pid=?,
                    started_at=?
                WHERE id=? AND status='ready'
            """, (f"worker:{pid}", expires, pid, now, task_id))

            if updated.rowcount == 0:
                # Lost the race to another dispatcher
                result["skipped"].append({"task_id": task_id, "reason": "race_lost"})
                continue

            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, None, "claimed", json.dumps({"worker_pid": pid}), now),
            )

            claimed_info = {
                "task_id": task_id,
                "title": row["title"],
                "assignee": row["assignee"],
                "pid": pid,
                "expires": expires,
            }
            result["claimed"].append(claimed_info)

            # Step 3: Spawn worker
            spawn_info = _spawn_worker_for_task(task_id)
            if spawn_info:
                result["spawned"].append(spawn_info)
            else:
                # Spawn failed — reset to todo so it can be retried
                conn.execute("""
                    UPDATE tasks SET status='todo',
                        claim_lock=NULL, claim_expires=NULL, worker_pid=NULL
                    WHERE id=?
                """, (task_id,))
                result["skipped"].append({
                    "task_id": task_id,
                    "reason": "spawn_failed",
                })

            spawned_count += 1
            if spawned_count >= max_spawn:
                break

    logger.debug("dispatch_once completed: reclaimed=%d claimed=%d spawned=%d",
                  len(result["reclaimed"]), len(result["claimed"]), len(result["spawned"]))
    return result
---

title: Kanban Orchestrator — Protocol Reference
type: resource
space: outcome
tags: [outcome]
created: 2026-05-20
updated: 2026-05-20
links: []
links:
  - "[[P3-sensors/skills/SKILL-INDEX]]"
---


# Kanban Orchestrator — Protocol Reference

## API Schema

### `kanban_create`

```python
kanban_create(
    title: str,                      # Required
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    parent_task_ids: Optional[List[str]] = None,  # For dependency chains
    status: str = "ready",            # Auto-set based on parent_task_ids
    priority: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    skills: Optional[List[str]] = None,
    max_runtime_seconds: int = 3600,
    trigger_source: str = "orchestrator",
    integration_workflow_id: Optional[str] = None,
) -> {
    "ok": True,
    "task_id": "t_<12hex>",
    "title": "...",
    "status": "ready" | "todo",
    "idempotent": True | False  # only if idempotency_key matched
}
```

### `task_link`

```python
task_link(
    parent_id: str,   # Must exist in DB
    child_id: str,    # Must exist in DB
) -> {
    "ok": True,
    "parent_id": "t_...",
    "child_id": "t_..."
}
# Side effect: child demoted to 'todo' if parent not done
# Side effect: cycle detection (raises error if cycle would form)
```

### `kanban_list`

```python
kanban_list(
    status: Optional[str] = None,   # "ready" | "todo" | "in_progress" | "completed" | "blocked" | "failed"
    assignee: Optional[str] = None,
) -> [
    {
        "id": "t_...",
        "title": "...",
        "status": "...",
        "assignee": "..." | None,
        "result": "..." | None,
        "created_at": "ISO",
        ...
    },
    ...
]
```

### `task_get`

```python
task_get(task_id: str) -> {...} | None  # None if not found
```

### `task_get_events`

```python
task_get_events(task_id: str) -> [
    {"task_id": "...", "run_id": "..." | None, "kind": "created" | "promoted" | "completed" | "unresolved_refs" | "blocked" | "unblocked", "payload": "{}", "created_at": "ISO"},
    ...
]
```

### `kanban_complete`

```python
kanban_complete(
    task_id: str,
    result: Optional[str] = None,          # What was produced
    summary: Optional[str] = None,           # One-liner for board display
    metadata: Optional[Dict[str, Any]] = None,
    created_cards: Optional[List[str]] = None,  # Sub-task IDs — verified against DB
    expected_run_id: Optional[str] = None,
) -> {
    "ok": True,
    "task_id": "t_...",
    "status": "completed",
    "promoted": ["t_child1", ...],   # Only if children were promoted
    "unresolved_refs": ["t_xxx", ...],  # Only if prose mentioned unknown task IDs
    "error": "...",  # Only if hallucination detected
}
```

### `kanban_block` / `kanban_unblock`

```python
kanban_block(task_id: str, reason: Optional[str] = None) -> {"ok": True, "task_id": "...", "status": "blocked"}
kanban_unblock(task_id: str) -> {"ok": True, "task_id": "...", "status": "ready"}
```

## Status State Machine

```
ready
  → claimed (worker calls task_claim)
  → blocked (worker calls task_block)
  → canceled (manually canceled)

claimed → in_progress (worker sends first heartbeat)

in_progress
  → completed (worker calls kanban_complete)
  → blocked
  → failed (max_retries exceeded)

blocked → ready (worker calls task_unblock)

todo → ready (parent completes → auto-promoted)
ready → todo (task_link to incomplete parent → demoted)
```

## Prose Reference Pattern

Workers may write natural language that references task IDs:

```
"S化物완료. t_abc123로 결과 전달하세요"
"passed to t_def456 for next step"
```

These are extracted via regex `t_([0-9a-f]{12})` and checked against DB.
Unknown IDs are flagged as `unresolved_refs` in the completion event.
This does NOT block completion — it's an audit/log signal.

## Cycle Detection

`task_link` does NOT implement cycle detection in Drewgent's current implementation.
**Limitation**: Linking A→B then B→A will create a circular dependency.
**Workaround**: Use `parent_task_ids` at task creation time — if a cycle would form,
`kanban_create` will eventually hang (no tasks become `ready`).
Detect by monitoring: if many tasks are `todo` with all parents `completed`, you have a cycle.

## Idempotency

```python
kanban_create(
    title="Same task",
    body="...",
    idempotency_key="build-1234-step-1",
)
# First call: creates new task, returns task_id
# Second call: returns existing task_id with status, ok=True, idempotent=True
```

## Spawn Worker Pattern

```python
from tools.kanban_tools import spawn_worker

result = spawn_worker(
    task_id="t_abc123",
    board="default",
    max_runtime_seconds=3600,
)
# result: {"ok": True, "worker_pid": 12345, "task_id": "t_abc123"}
```

## Error Handling

| Error | Cause | Resolution |
|-------|-------|-----------|
| `completion_blocked_hallucination` | `created_cards` contains fake ID | Worker must use only real task IDs |
| `Task 't_xxx' not found` | ID doesn't exist | Check task ID before calling |
| `UNIQUE constraint failed: tasks.idempotency_key` | Duplicate idempotency_key with different params | OK — returns existing task |
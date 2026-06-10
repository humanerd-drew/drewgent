---
title: Launchd Process Health Check
name: launchd-process-health-check
type: skill
space: outcome
description: Diagnose launchd service state when launchctl list shows stale exit codes but process is actually running
tags: [skill, launchd, troubleshooting, diagnostics]
created: 2026-05-31
updated: 2026-05-31
links:
  - "[[P3-sensors/skills/SKILL-INDEX]]"
  - "[[P4-cortex/growth/harness-autonomous-behaviors]]"
---

# Launchd Process Health Check

## Problem

When checking `launchctl list | grep <name>`, the Exit code and PID columns can be **misleading**. A process can:
- Show "Exit -15" but actually be running (someone else restarted it)
- Show old PID but have been replaced by a new process
- Be managed by launchd but running under a different binary

## Always Verify With These 3 Commands

```bash
# 1. launchd's view (may be stale)
launchctl list | grep <service-name>

# 2. Is it actually listening? (ground truth)
lsof -i :<port> 2>/dev/null

# 3. Is there a process? (ground truth)
ps aux | grep -i <process-name> | grep -v grep
```

## Common Exit Codes Explained

| Exit Code | Meaning |
|-----------|---------|
| 0 | Clean exit (normal) |
| -15 | SIGTERM received (killed) |
| -64 | Exit with signal 64 (often network/socket issue) |
| 87299 | Large number — usually a PID shown as "last exit status", not an exit code |

**Important**: The PID column shows the LAST process that exited, not the current one. A "running" service can show an old PID with a non-zero exit code.

## Real Example (Kanban Dashboard)

```
$ launchctl list | grep kanban-dashboard
87299  -15  ai.drewgent.kanban-dashboard
```

This looks crashed. But:

```
$ lsof -i :8765
Python  87299  ...  TCP *:ultraseek-http (LISTEN)  ✅ RUNNING

$ ps aux | grep kanban_dashboard
drew  87299  ...  python@3.14 ... kanban_dashboard_server.py  ✅ RUNNING
```

**Interpretation**: The process IS running. The "Exit -15" is a stale snapshot — launchd saw the process die at some point, but something (another launchd restart attempt, manual start) brought it back. The Exit code reflects that historical death, not current state.

## When the Plist Path Doesn't Match Reality

In the kanban dashboard case:
- **plist**: `.venv/bin/python` (drewgent-agent venv)
- **actual**: `python@3.14` (homebrew system Python)

This mismatch means launchd can't properly manage/restart the service. Fix:

```bash
# Stop
launchctl stop ai.drewgent.kanban-dashboard

# Fix plist to use actual python path
# Or reinstall the service

# Start
launchctl start ai.drewgent.kanban-dashboard

# Verify
lsof -i :<port>
ps aux | grep <process-name>
```

## Checklist

- [ ] `launchctl list` → note PID and Exit
- [ ] `lsof -i :<port>` → confirm LISTEN
- [ ] `ps aux | grep <name>` → confirm process
- [ ] If process running but launchd shows dead → check plist ProgramArguments matches actual binary
- [ ] If running under wrong python → update plist or fix venv

## Related
- [[P4-cortex/growth/harness-autonomous-behaviors]] — harness self-healing patterns
- brain-dashboard-system skill
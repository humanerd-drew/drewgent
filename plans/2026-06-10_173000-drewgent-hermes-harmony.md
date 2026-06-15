# Drewgent ↔ Hermes Harmony Plan (대전제 기반)

> **For Drewgent:** Use subagent-driven-development skill to implement this plan task-by-task.

**대전제 (user-given, 2026-06-10)**:
> .drewgent의 내부 구조를 유지하면서 hermes-agent의 기능을 사용한다. 이는 나만의 맥락을 따라 나에게 맞춰 작동하는 hermes-agent가 되길 바라는 것이다.

**Goal:** Make hermes-agent work *for Drewgent* (not the other way around). Eliminate the 5
architectural drift points so a single user question produces one consistent answer.

**Architecture decision tree (대전제 → implementation choice):**

| 대전제 적용 | 결과 |
|---|---|
| .drewgent 구조 유지 | 신경계 layer (P0-P6) 절대 안 건드림 |
| hermes-agent를 *내* 맥락에 맞춰 작동 | Hermes 코드를 *내 환경에서* customize — upstream PR 안 함 |
| hermes는 consumer, .drewgent는 controller | .drewgent가 *어떤* hermes 동작을 강제할지 결정 |

**대상 시스템**:
- Drewgent 신경계: `~/.drewgent/P0`-`P6` (건드리지 않음)
- Drewgent 인프라: `~/.drewgent/source/drewgent-agent/`, `~/.drewgent/scripts/`, `~/.drewgent/cron/`
- launchd plists: `~/Library/LaunchAgents/ai.drewgent.*` + `com.drewgent.*` (소유)
- Hermes infra: `~/.hermes/hermes-agent/` (Drewgent가 *wrap*하여 customize)
- 내 customize layer: `~/.drewgent/customize/` (신규, hermes wrapper들 위치)

**Tech Stack:** Python 3.14, bash 3.2 (macOS), launchd plists, cron, Obsidian neurons.

---

## 5 Drift Points — 대전제 적용 후 결정

| D# | 균열 | 대전제 적용한 해결 | Layer |
|---|---|---|---|
| **D1** | hermes가 우리 gateway를 못 찾음 (`get_launchd_label()` hardcoded) | hermes를 *내 환경*에 맞춰 customize. `.drewgent/customize/hermes_cli/gateway.py`에 `get_launchd_label()` override. `PYTHONPATH`로 hijack | Hermes (override) |
| **D2** | jobs.json mtime drift | harmony check cron이 mtime mismatch 감지. `.drewgent/scripts/jobs_mtime_check.py` 추가 | .drewgent |
| **D3** | Memory dual-source | `.drewgent/P2-hippocampus/memories/MEMORY.md`만 canonical. `~/.codex/memories/`는 무시. harmony check가 .codex가 더 최신이면 alert (drift 지표) | .drewgent |
| **D4** | Neuron auto-trigger 없음 | `run_agent.py`에 *한 줄* trigger scan 추가 (prompt cache 무효화 회피) | .drewgent |
| **D5** | cron-runner ≠ gateway scheduler | unify. `scripts/dispatch_once_*.py` → jobs.json entries. cron-runner plist 폐기 | .drewgent |

---

## Phase 1 — Customization Layer Setup (D1)

### Goal
`~/.drewgent/customize/` 디렉토리 셋업. hermes가 import할 때 .drewgent가 *먼저* 로드되도록.

### Task 1.1: Customize layer 디렉토리 구조 작성

**Files to create:**
- `~/.drewgent/customize/README.md` (layer 목적 설명)
- `~/.drewgent/customize/__init__.py` (Python package marker)
- `~/.drewgent/customize/hermes_cli/__init__.py` (override용 namespace)
- `~/.drewgent/customize/sitecustomize.py` (Python startup 시 자동 로드)

**Step 1: Create directories**

```bash
mkdir -p ~/.drewgent/customize/hermes_cli
touch ~/.drewgent/customize/__init__.py
touch ~/.drewgent/customize/hermes_cli/__init__.py
```

**Step 2: Write `~/.drewgent/customize/README.md`**

```markdown
# Drewgent Customization Layer for Hermes

This directory contains code that **overrides** hermes-agent internals to make
hermes work for Drewgent's context (not the upstream generic hermes).

## How it works

1. `sitecustomize.py` runs at Python startup
2. It inserts `~/.drewgent/customize/` to `sys.path` BEFORE hermes
3. When hermes does `from hermes_cli.gateway import ...`, Python first checks
   `~/.drewgent/customize/hermes_cli/`
4. If a custom version exists, that wins; otherwise hermes's own loads

## Why this layer exists

Hermes's `get_launchd_label()` returns `ai.hermes.gateway` (hardcoded), but
Drewgent uses `ai.drewgent.gateway`. Override here so hermes's CLI sees the
right label for *this* environment.

## Activation

Set `PYTHONPATH=~/.drewgent/customize:$PYTHONPATH` in shell env, or symlink
`sitecustomize.py` to a path Python automatically loads.

## Files in this layer

- `hermes_cli/gateway.py` — overrides hermes's gateway.py with our label logic
- `hermes_cli/cron.py` — overrides hermes's cron.py with our health-check logic
```

**Step 3: Write `~/.drewgent/customize/sitecustomize.py`**

```python
"""Drewgent sitecustomize — auto-load customize layer at Python startup.

Insert ~/.drewgent/customize/ at sys.path[0] so 'from hermes_cli.gateway'
loads OUR gateway.py first.
"""
import os
import sys
from pathlib import Path

CUSTOMIZE = Path.home() / ".drewgent" / "customize"
if CUSTOMIZE.exists() and str(CUSTOMIZE) not in sys.path:
    sys.path.insert(0, str(CUSTOMIZE))
```

**Step 4: Test that sitecustomize loads**

```bash
PYTHONSTARTUP=~/.drewgent/customize/sitecustomize.py python3 -c "
import sys
assert str(Path.home() / '.drewgent' / 'customize') in sys.path
print('OK: customize layer loaded')
"
```

Expected: `OK: customize layer loaded`

**Step 5: Wire up sitecustomize in launchd plist env**

Modify `~/Library/LaunchAgents/ai.drewgent.gateway.plist` to include:
```xml
<key>PYTHONPATH</key>
<string>/Users/drew/.drewgent/customize</string>
```

This makes the gateway's Python process auto-load the customize layer.

**Step 6: Commit (not applicable — `.drewgent/customize/` is not in git)**

No commit. Verify with `ls -la ~/.drewgent/customize/`.

---

### Task 1.2: Override `get_launchd_label()` in customize layer

**Files to create:**
- `~/.drewgent/customize/hermes_cli/__init__.py` (extend to re-export)
- `~/.drewgent/customize/hermes_cli/gateway.py` (override)

**Step 1: Read hermes's `gateway.py` around `find_gateway_pids` and `get_launchd_label`**

Done in 6/10 investigation. Lines 557-584 (find_gateway_pids), 3064-3067
(get_launchd_label).

**Step 2: Create override `gateway.py`**

```python
"""Drewgent override of hermes_cli.gateway — gateway label is ai.drewgent.* not ai.hermes.*

Strategy: copy minimal code from hermes's gateway.py but swap the label.
Import the rest of hermes's symbols transparently.
"""
import os
import sys

# Make sure the real hermes_cli is in path so we can import its other symbols
_REAL_HERMES = os.path.expanduser("~/.hermes/hermes-agent")
if _REAL_HERMES not in sys.path:
    sys.path.insert(1, _REAL_HERMES)

# Re-export everything from real hermes_cli.gateway except our overrides
from hermes_cli.gateway import *  # noqa: F401,F403

# --- Override: get_launchd_label ---
def get_launchd_label() -> str:
    """Drewgent uses ai.drewgent.gateway (not ai.hermes.gateway)."""
    return "ai.drewgent.gateway"

# Bind it back to the real hermes_cli.gateway module so hermes's internal
# references resolve to our version.
import hermes_cli.gateway as _real_gw
_real_gw.get_launchd_label = get_launchd_label
```

**Step 3: Test that the override works**

```bash
PYTHONPATH=~/.drewgent/customize python3 -c "
from hermes_cli.gateway import get_launchd_label
print('Label:', get_launchd_label())
"
```

Expected: `Label: ai.drewgent.gateway`

**Step 4: Test that hermes cron list no longer prints the warning**

```bash
hermes cron list 2>&1 | grep -c "Gateway is not running"
```

Expected: `0`

**Step 5: Verify the gateway itself still works**

```bash
ps aux | grep "drewgent_cli.main.*gateway" | grep -v grep
# gateway should still be running
```

**Step 6: Restart gateway to pick up the customize layer (it was started before sitecustomize was active)**

```bash
launchctl kickstart -k gui/$(id -u)/ai.drewgent.gateway
sleep 5
ps aux | grep "drewgent_cli.main.*gateway" | grep -v grep
# gateway should be running with new env
```

---

### Task 1.3: Override `hermes_cli/cron.py` health check

**File to create:**
- `~/.drewgent/customize/hermes_cli/cron.py`

**Step 1: Re-export from real hermes_cli.cron with our find_gateway_pids**

```python
"""Drewgent override of hermes_cli.cron — use our gateway detection."""
import os
import sys

_REAL_HERMES = os.path.expanduser("~/.hermes/hermes-agent")
if _REAL_HERMES not in sys.path:
    sys.path.insert(1, _REAL_HERMES)

# Re-export everything from real hermes_cli.cron
from hermes_cli.cron import *  # noqa: F401,F403

# Replace find_gateway_pids call sites with a version that ALSO looks for
# ai.drewgent.gateway. Real hermes uses ai.hermes.gateway; we accept both.
def _drewgent_find_gateway_pids() -> list:
    """Find gateway PIDs across all label variants Drewgent supports."""
    import subprocess
    pids = set()
    for label in ("ai.drewgent.gateway", "ai.hermes.gateway"):
        try:
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] == label:
                        try:
                            pid = int(parts[0])
                            if pid > 0:
                                pids.add(pid)
                        except ValueError:
                            pass
        except Exception:
            pass
    return list(pids)
```

**Step 2: Patch hermes's cron.py to use our detection**

Hermes's `cron.py:143-148`:
```python
from hermes_cli.gateway import find_gateway_pids
if not find_gateway_pids():
    print("⚠  Gateway is not running")
```

This is *inside* a function body. We can't easily patch the function body
from outside. The cleanest workaround is to make `find_gateway_pids` return
truthy when our gateway is alive (which we did in Task 1.2 by replacing
`get_launchd_label()`). So this Task 1.3 might be unnecessary.

**Verification**: Test if Task 1.2 alone fixes the warning. If yes, skip Task 1.3.
If no, do Task 1.3 by monkey-patching `hermes_cli.cron.cron_list_print`'s
internal call (more invasive; document why needed).

---

## Phase 2 — jobs.json Drift Detection (D2)

### Task 2.1: Add mtime check to harmony check

**File to modify:**
- `~/.hermes/scripts/drewgent_harmony_check.sh`

**Step 1: Add Layer 2.5 (jobs.json mtime vs gateway in-memory state)**

Insert after Layer 3 in harmony check:

```bash
# --- Layer 3.5: jobs.json mtime vs cron-runner.log "dispatchers run" mtime ---
emit ""
emit "## Layer 3.5: jobs.json mtime drift"
JOBS_MTIME=$(stat -f %m "$JOBS_JSON" 2>/dev/null || echo 0)
DISPATCHER_LOG="$DREW_HOME/logs/cron-runner.log"
if [ -f "$DISPATCHER_LOG" ]; then
  LAST_DISPATCH_MTIME=$(grep -E "dispatchers run" "$DISPATCHER_LOG" 2>/dev/null | tail -1 | grep -oE '\[[0-9-]+ [0-9:]+\]' | tr -d '[]' | xargs -I{} date -j -f "%Y-%m-%d %H:%M:%S" {} +%s 2>/dev/null || echo 0)
  if [ "$JOBS_MTIME" -gt "$LAST_DISPATCH_MTIME" ]; then
    emit "  ⚠ jobs.json modified at $(date -r $JOBS_MTIME) but last dispatcher tick was at $(date -r $LAST_DISPATCH_MTIME 2>/dev/null) — in-memory state may be stale"
  else
    emit "  ✓ jobs.json mtime aligns with dispatcher tick"
  fi
else
  emit "  ~ cron-runner.log not found (no dispatcher activity to compare)"
fi
```

**Step 2: Test by touching jobs.json and running harmony check**

```bash
touch ~/.drewgent/cron/jobs.json
bash ~/.hermes/scripts/drewgent_harmony_check.sh
# Should see drift warning
```

**Step 3: Re-test after dispatcher tick**

```bash
sleep 60  # wait for next tick
bash ~/.hermes/scripts/drewgent_harmony_check.sh
# Warning should disappear
```

---

## Phase 3 — Memory Single Source (D3)

### Task 3.1: Document memory canonical path

**File to modify:**
- `~/.drewgent/P6-prefrontal/incidents/launchd-mass-failure-20260610.md` (section 6.5.open)

**Step 1: Replace open item 2 with resolved status**

In section 6.5.open, change:
```
- Brain_monitor ... — log spam, not data loss (fallback works). Fix is in agent code; not part of 6/10 incident follow-ups.
```

Add new section 6.6:

```markdown
## 6.6. Memory canonical path (resolved 2026-06-10)

`~/.drewgent/P2-hippocampus/memories/MEMORY.md` is the **single source of truth** for agent memory.
- `~/.codex/memories/MEMORY.md` is a Codex-CLI artifact and is **ignored** by Drewgent.
- harmony_check.sh includes a "memory source comparison" check (Layer 4.5) that alerts if
  Codex's MEMORY.md is significantly newer than Drewgent's (indicating entries are being written
  to the wrong path).

This is a policy decision, not a code change. The Codex environment is a separate
session type (Codex CLI) and its memory is intentionally separate.
```

### Task 3.2: Add memory source comparison to harmony check

**File to modify:**
- `~/.hermes/scripts/drewgent_harmony_check.sh`

**Step 1: Add Layer 4.5**

```bash
# --- Layer 4.5: memory source comparison ---
emit ""
emit "## Layer 4.5: memory single source of truth"
DREW_MEMORY="$DREW_HOME/P2-hippocampus/memories/MEMORY.md"
CODEX_MEMORY="$HOME/.codex/memories/MEMORY.md"
if [ -f "$DREW_MEMORY" ] && [ -f "$CODEX_MEMORY" ]; then
  DREW_MTIME=$(stat -f %m "$DREW_MEMORY")
  CODEX_MTIME=$(stat -f %m "$CODEX_MEMORY")
  CODEX_NEWER_DAYS=$(( (CODEX_MTIME - DREW_MTIME) / 86400 ))
  if [ "$CODEX_MTIME" -gt $((DREW_MTIME + 86400)) ]; then
    emit "  ⚠ Codex MEMORY.md is $CODEX_NEWER_DAYS days newer than Drewgent memory"
    emit "     Drewgent does NOT sync to Codex path. This is informational only."
  else
    emit "  ✓ Drewgent memory is canonical (Codex ${CODEX_NEWER_DAYS}d older or aligned)"
  fi
elif [ -f "$DREW_MEMORY" ]; then
  emit "  ✓ Drewgent memory exists; Codex path absent (clean)"
fi
```

---

## Phase 4 — Neuron Auto-Trigger (D4)

### Task 4.1: Add keyword scan to agent loop

**Goal:** When the user's message contains a keyword matching a neuron title, inject a
one-line hint into the system prompt *stably* (i.e., once per conversation, not per turn).

**Files to investigate (read-only first):**
- `~/.drewgent/source/drewgent-agent/agent/prompt_builder.py` (where system prompt is built)
- `~/.drewgent/source/drewgent-agent/run_agent.py` (entry point)

**Step 1: Identify system prompt assembly line**

Use `grep` to find where `禁incident_aware` or `禁*.neuron` files are loaded.

**Step 2: Add a minimal trigger mechanism**

Add to prompt_builder.py (or wherever the stable prefix is built):

```python
def _drewgent_neuron_trigger_hint(user_message: str, session_state: dict) -> str:
    """Return a one-line hint if user_message matches a neuron title.

    Sticky: only fires ONCE per session (checked via session_state).
    """
    if session_state.get("neuron_hint_fired"):
        return ""  # already fired; no further hint
    keywords = {
        "禁incident_aware": ["상태 점검", "incident", "watchdog", "에이전트 점검", "agent health"],
        "禁filesystem_truth": ["filesystem", "file content", "ground truth"],
    }
    msg_lower = user_message.lower()
    for neuron, kws in keywords.items():
        if any(kw.lower() in msg_lower for kw in kws):
            session_state["neuron_hint_fired"] = True
            session_state["neuron_hint_active"] = neuron
            return f"\n[禁 neuron hint active: {neuron} — see ~/P6-prefrontal/incidents/launchd-mass-failure-20260610.md]\n"
    return ""
```

**Step 3: Inject into system prompt (sticky addition)**

The hint is appended to the system prompt on the first turn only. Subsequent turns
have the same prompt prefix (cache-safe).

**Step 4: Test**

```bash
# From a fresh session
hermes chat "에이전트 상태 점검해줘"
# First response should reference the 6/10 incident doc
```

---

## Phase 5 — Scheduler Unification (D5)

### Task 5.1: Add 3 jobs.json entries for board dispatchers

**File to modify:**
- `~/.drewgent/cron/jobs.json`

**Step 1: Read existing jobs.json structure**

Already have it. Each job has: id, name, prompt, script, schedule, repeat, etc.

**Step 2: Add 3 new entries (script-based, no LLM cost)**

```json
{
  "id": "kanban-dispatcher-default-script",
  "name": "kanban-dispatcher (default board)",
  "script": "/Users/drew/.drewgent/scripts/dispatch_once_default.py",
  "args": ["default"],
  "schedule": {
    "kind": "interval",
    "seconds": 60
  },
  "enabled": true,
  "state": "scheduled",
  "next_run_at": "2026-06-10T17:30:00+09:00",
  "deliver": "local"
}
```

Repeat for `content` and `integrations` boards with the corresponding script.

**Step 3: Verify gateway scheduler picks them up**

Restart gateway so the new entries load into in-memory state:
```bash
launchctl kickstart -k gui/$(id -u)/ai.drewgent.gateway
sleep 30  # wait for first tick
```

Check cron output dir:
```bash
ls -lt ~/.drewgent/cron/output/*/ | head -10
```

**Step 4: Test 24h**

Wait 24h with both cron-runner AND gateway scheduler running dispatchers. If duplicate
runs occur (a board gets dispatched twice per minute), there will be visible log
duplication. If clean, proceed to Task 5.2.

### Task 5.2: Disable ai.drewgent.cron-runner.plist

**Step 1: Move plist to disabled directory**

```bash
mkdir -p ~/Library/LaunchAgents/disabled
mv ~/Library/LaunchAgents/ai.drewgent.cron-runner.plist ~/Library/LaunchAgents/disabled/
launchctl bootout gui/$(id -u)/ai.drewgent.cron-runner 2>&1 || true
```

**Step 2: Verify gateway is the only dispatcher**

```bash
ps aux | grep -E 'dispatch_once|cron_runner' | grep -v grep
# Should only see gateway's tick; no separate cron-runner
```

**Step 3: 24h soak**

If after 24h no board was starved (no missed kanban work), proceed to Task 5.3.

### Task 5.3: Delete the disabled plist

```bash
rm ~/Library/LaunchAgents/disabled/ai.drewgent.cron-runner.plist
rmdir ~/Library/LaunchAgents/disabled/ 2>/dev/null  # if empty
```

---

## Phase 6 — Verification

### Task 6.1: Run full harmony check

```bash
bash ~/.hermes/scripts/drewgent_harmony_check.sh
```

Expected: 0 drift (after all phases complete).

### Task 6.2: Verify all customizations coexist

- `hermes cron list` shows "Gateway is running" (D1 ✓)
- jobs.json drift detection works (D2 ✓)
- Memory source comparison clean (D3 ✓)
- Neuron auto-trigger works on keyword (D4 ✓)
- Single scheduler (gateway only) handles all 6 jobs (D5 ✓)

### Task 6.3: Update incident doc section 6.5

Mark all 5 open items as resolved with the date and method.

---

## Files Likely to Change

**Created:**
- `~/.drewgent/customize/README.md`
- `~/.drewgent/customize/__init__.py`
- `~/.drewgent/customize/sitecustomize.py`
- `~/.drewgent/customize/hermes_cli/__init__.py`
- `~/.drewgent/customize/hermes_cli/gateway.py`
- `~/.drewgent/customize/hermes_cli/cron.py` (only if D1 Task 1.2 isn't enough)

**Modified:**
- `~/Library/LaunchAgents/ai.drewgent.gateway.plist` (add PYTHONPATH)
- `~/.hermes/scripts/drewgent_harmony_check.sh` (D2 + D3 layers)
- `~/.drewgent/cron/jobs.json` (3 new script entries for D5)
- `~/.drewgent/source/drewgent-agent/agent/prompt_builder.py` (D4 trigger)
- `~/.drewgent/P6-prefrontal/incidents/launchd-mass-failure-20260610.md` (section 6.5/6.6)

**Moved:**
- `~/Library/LaunchAgents/ai.drewgent.cron-runner.plist` → `~/Library/LaunchAgents/disabled/`
  (then deleted after 24h soak)

---

## Tests / Validation

| Phase | Test | Expected |
|---|---|---|
| 1.1 | `python3 -c "import sys; assert '/Users/drew/.drewgent/customize' in sys.path"` (with PYTHONPATH set) | passes |
| 1.2 | `hermes cron list 2>&1 \| grep -c "Gateway is not running"` | `0` |
| 1.2 | `from hermes_cli.gateway import get_launchd_label; print(get_launchd_label())` (with PYTHONPATH) | `ai.drewgent.gateway` |
| 2.1 | `touch jobs.json; bash harmony_check.sh \| grep "Layer 3.5"` | shows drift |
| 2.1 | `sleep 60; bash harmony_check.sh \| grep "Layer 3.5"` | shows ✓ |
| 3.1 | `bash harmony_check.sh \| grep "Layer 4.5"` | shows canonical path |
| 4.1 | `hermes chat "상태 점검"` (fresh session) | first response mentions 6/10 incident doc |
| 5.1 | `ls ~/.drewgent/cron/output/*/ | wc -l` after 24h | matches 6 jobs × 24h × 60 ticks (approximately) |
| 5.3 | `ps aux \| grep cron_runner` | empty |

---

## Risks & Tradeoffs

### R1: PYTHONPATH hijack is fragile
- **Risk:** If hermes's `hermes_cli/gateway.py` signature changes upstream, our
  override might break silently.
- **Mitigation:** Add an integration test that imports `get_launchd_label` and asserts
  the result. If hermes renames it, override fails loudly.

### R2: D5 migration could break tick timing
- **Risk:** gateway scheduler tick (60s) + 3 dispatcher scripts + 3 existing jobs = 6
  jobs per tick. If any script is slow, others slip.
- **Mitigation:** 24h soak (Task 5.1 step 4). Roll back by re-enabling cron-runner plist.

### R3: D4 trigger might fire spuriously
- **Risk:** "incident" appears in many contexts; firing on false positive injects the
  hint into irrelevant conversations.
- **Mitigation:** Keywords are tuned to user's typical phrasing ("상태 점검", "에이전트 점검").
  If false-positive is high, narrow keywords.

### R4: D1 PYTHONPATH must be set in EVERY shell that uses hermes
- **Risk:** If user opens a new terminal without `PYTHONPATH`, hermes falls back to its
  default label.
- **Mitigation:** Add `export PYTHONPATH=~/.drewgent/customize:$PYTHONPATH` to `.zshrc`
  (with comment explaining why). Or, put it in the hermes invocation script.

### R5: R1+R4 combined — D1 + R4
- **Risk:** Even with .zshrc, cron jobs (which don't inherit .zshrc) won't have
  PYTHONPATH.
- **Mitigation:** Set PYTHONPATH in the plist's `EnvironmentVariables` dict for the
  gateway. For `hermes cron list` invoked from terminal, .zshrc is enough.

---

## Execution Strategy

**Two execution paths after this plan is approved:**

### Path A: User says "go" — execute with subagent-driven-development
I dispatch 5 fresh subagents (one per phase), each with the full task text. Each
subagent does TDD, gets spec review, gets quality review, commits per task.
Estimated total: 13 tasks × ~5 min + reviews = ~75 min.

### Path B: User says "just plan" — stop here
Plan is saved. User can come back later and say "execute phase 1" or "execute all."

**Default if user is silent for 60s after this plan is saved:** Path B (plan only).
User must explicitly say "go" to start execution.

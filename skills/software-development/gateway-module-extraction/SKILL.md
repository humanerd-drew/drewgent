---
name: gateway-module-extraction
description: Extract modules from gateway/run.py (9,876 lines) into isolated files under gateway/. Covers stdlib name collision, runner circular reference, mock fixture sync order, and honest multi-session QA verdicts.
type: skill
space: outcome
tags: [skill, software-development, refactoring, drewgent-gateway]
created: 2026-06-01
updated: 2026-06-01
links:
  - "[[P0-brainstem/brain/rules]]"
  - "[[P4-cortex/plans/gateway_decomposition_plan]]"
---

# Skill: Extracting Modules from gateway/run.py

## When to Use
When splitting GatewayRunner methods into separate modules under `gateway/` and the extracted class needs to read/write the runner's many attributes (adapters, session_store, hooks, task_manager, _running_agents, etc.).

## Three Things That Bite

### 1. stdlib name collision
`Dispatcher` clashes with `asyncio.Dispatcher` (or any stdlib symbol you forgot). Symptom: mysterious AttributeError or TypeError at import time, not at use time. Fix: prefix with the domain — `MessageDispatcher`, `SessionLifecycle`, `CronRunner`. Always grep stdlib before naming:

```bash
python3 -c "import asyncio; print(hasattr(asyncio, 'Dispatcher'))"
```

### 2. circular reference via runner
Extracted class needs `self.adapters`, `self._running_agents_ts`, etc. Two ways to give it access:

a) Pass runner in `__init__` and store as `self.runner`:
```python
class SentinelGuard:
    def __init__(self, runner):
        self.runner = runner  # NOT self.run_agent
    def is_sentinel(self, session_key):
        return self.runner._running_agents.get(session_key) is _SENTINEL
```

b) Pass the specific attributes the class needs (cleaner but verbose).

For Drewgent gateway, use (a) — 51 attribute references would make (b) painful.

### 3. Mock fixture sync order
After extracting modules, tests fail because `mock_gateway_runner` (built via `object.__new__(GatewayRunner)`) doesn't have the new attributes. **Order matters**:

1. First: replace the original method body with `self._new_module.method()` delegate
2. Then: update mock fixture to set the new attributes
3. Then: run tests

Doing fixture first leaves the real runner working but the mock broken. Doing tests first leaves everything broken. Delegate-first means the wiring is provably correct before any test changes.

## Step-by-Step Procedure

1. **Pick the next extraction** by LOC + import surface. SentinelGuard (200 LOC, 3 attrs) before MessageDispatcher (580 LOC, 30+ attrs).
2. **Create skeleton module** with the class signature, leaving methods as `pass` or `raise NotImplementedError`.
3. **Copy method body verbatim** from run.py into the new module. Replace `self.foo` with `self.runner.foo`.
4. **Re-export at run.py module level** for backward compat if any test imports the old name:
   ```python
   # run.py
   from gateway.sentinel_guard import SentinelGuard  # noqa: F401
   ```
5. **Wire in `__init__`**: `self._sentinel_guard = SentinelGuard(self)` after the attrs it touches are set.
6. **AST parse check**:
   ```python
   import ast, glob
   for p in glob.glob('gateway/*.py'):
       try:
           ast.parse(open(p).read())
           print(f'OK  {p}')
       except SyntaxError as e:
           print(f'FAIL {p}: {e}')
   ```
7. **DO NOT replace the call site yet** — keep both the inline method and the new module working in parallel. This is a feature flag without the flag.
8. **Update plan doc** with what was extracted, what remains, exact LOC counts.

## Order of Extraction (Recommended)

| Priority | Module | LOC | Difficulty | Why first/later |
|----------|--------|-----|------------|-----------------|
| 1 | sentinel_guard | ~200 | LOW | Small, isolated, has its own tests |
| 2 | adapters | ~400 | LOW | Mostly config loading |
| 3 | stream_consumer | ~300 | LOW | Self-contained async iterator |
| 4 | session + session_manager | ~900 | MEDIUM | Many attrs touched |
| 5 | hooks | ~300 | LOW | Already a self-contained class |
| 6 | delivery | ~300 | LOW | Single-method router |
| 7 | task_manager | ~200 | LOW | Background task tracking |
| 8 | pairing | ~400 | MEDIUM | Touches session_store |
| 9 | channel_directory | ~300 | LOW | Read-mostly |
| 10 | cron_runner | ~400 | MEDIUM | Lifecycle + retry |
| 11 | **MessageDispatcher (was Dispatcher)** | ~580 | HIGH | Last — biggest, touches everything |

Save MessageDispatcher for last. It needs all the other modules in place to delegate to.

## Verification After Each Extraction

```bash
# 1. AST parse all touched files
python3 -c "
import ast, glob
for p in glob.glob('gateway/*.py'):
    try:
        ast.parse(open(p).read())
        print(f'OK  {p}')
    except SyntaxError as e:
        print(f'FAIL {p}: {e}')
"

# 2. Import check
cd /Users/drew/.drewgent/source/drewgent-agent
python3 -c "from gateway.sentinel_guard import SentinelGuard; print('SentinelGuard importable')"

# 3. Existing tests still pass (regression)
pytest tests/gateway/ -x -q 2>&1 | tail -20
```

## QA Evidence: Honest Verdict

When the work spans multiple sessions and you can't finish C3-C7 in one go, **write full-qa.json with `all_criteria_met: false`** and explain which criteria are deferred. Don't lie to pass the gate. `禁task_qa_gate` blocks delivery on false verdicts, but the alternative is shipping a partial refactor that looks complete in evidence.

Structure:
```json
{
  "criteria": [
    {"id": "C1", "met": true, "evidence": "..."},
    {"id": "C3", "met": false, "evidence": "Deferred to next session because..."}
  ],
  "met_count": 2,
  "total_criteria": 7,
  "verdict": "PARTIAL — X/Y met. Z blocked on ...",
  "blockers_for_delivery": ["C3 must complete before..."],
  "next_session_first_action": "Read gateway/run.py lines 1868-2447, copy verbatim into dispatcher.py..."
}
```

## Anti-Patterns

- Naming a class `Dispatcher`, `Loader`, `Manager`, `Handler` without checking stdlib
- Adding `from gateway.X import Y` at the top of run.py before the class is defined (circular import)
- Replacing the call site before the new module is wired in `__init__` (AttributeError at first request)
- Writing `all_criteria_met: true` when 5/7 criteria are deferred (the QA gate will catch it, but writing it dishonestly is worse)
- Using `mcp_patch` for ~580 LOC body replacement — even with `1` limit, the diff is too large. Use `read_file` + `write_file` for the whole method, or do it in chunks of ~50 LOC per patch

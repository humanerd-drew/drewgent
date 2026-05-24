---
title: Drewgent Refactor Plan — Step 1: run_agent.py 분해
type: document
space: concept
tags: [concept]
created: 2026-05-24
updated: 2026-05-24
links:
  - "[[P0-brainstem/brain/rules]]"
  - "[[P4-cortex/growth/INTEGRATION_PROTOCOL]]"
  - "[[P5-ego/SELF_MODEL]]"
---
# Drewgent Refactor Plan — Step 1: run_agent.py 분해

**Date**: 2026-05-24
**Status**: Planning
**Goal**: `run_agent.py` (11,604줄) → 6개 이하의 분리된 모듈

---

## 문제

```
run_agent.py  11,604줄  — 1개 class (AIAgent), 119개 method
gateway/run.py  8,684줄  — 1개 class (GatewayRunner), 90개 method
cli.py           8,724줄  — CLI
```

AIAgent class가 모든 것을 품고 있다:
- Provider/client lifecycle (OpenAI, Anthropic, Codex)
- Tool execution loop
- Session management
- Memory management
- Brain signals
- Streaming logic
- Budget tracking
- QA gate logic

---

## 분해 전략: 6개 모듈

### Module 1: `drewgent/runtime/agent.py` (~300줄) — Thin AIAgent

AIAgent class의对外 接口만 유지.

```python
# drewgent/runtime/agent.py
class AIAgent:
    def __init__(self, ...):  # 위임
    def run_conversation(self, ...):  # 위임
    def chat(self, ...):  # 위임
    def interrupt(self, ...):  # 위임
    def switch_model(self, ...):  # 위임
    def reset_session_state(self, ...):  # 위임
```

모든 내부 method는 위임(delegate)만 하고, 실제 구현은 다른 모듈에서.

### Module 2: `drewgent/runtime/core.py` (~2500줄) — AIAgent Internals

AIAgent의 internal method들. 순서대로 grouping:

| Group | Methods | Approx lines |
|-------|---------|-------------|
| Provider lifecycle | `_create_openai_client`, `_close_openai_client`, `_ensure_primary_openai_client`, `_create_request_openai_client`, `_openai_client_lock` | 400 |
| API calls | `_anthropic_messages_create`, `_interruptible_api_call`, `_interruptible_streaming_api_call`, `_run_codex_stream` | 600 |
| Tool execution | `_execute_tool_calls`, `_execute_tool_calls_concurrent`, `_execute_tool_calls_sequential`, `_invoke_tool` | 500 |
| Message handling | `_sanitize_api_messages`, `_format_tools_for_system_message`, `_convert_to_trajectory_format` | 200 |
| Budget/recovery | `_recover_with_credential_pool`, `_try_activate_fallback`, `_restore_primary_runtime` | 300 |
| Session/DB | `_persist_session`, `_flush_messages_to_session_db`, `_get_messages_up_to_last_assistant` | 200 |
| Trajectory | `_save_trajectory`, `_summarize_api_error`, `_dump_api_request_debug` | 200 |

### Module 3: `drewgent/runtime/streaming.py` (~600줄) — Streaming Logic

Streaming-specific logic (nested functions extracted):
- `_fire_stream_delta`, `_fire_reasoning_delta`, `_fire_tool_gen_started`
- `_has_stream_consumers`
- All nested functions inside `_interruptible_streaming_api_call`

### Module 4: `drewgent/runtime/prompt.py` (~600줄) — System Prompt

- `_build_system_prompt`
- `_invalidate_system_prompt`
- `_apply_persist_user_message_override`
- Dream loading (`load_dreams`)
- QA guidance template injection

### Module 5: `drewgent/brain/rules.py` + `drewgent/brain/subsystem.py` — Drewgent P-layer

Python에서 구현하는 Drewgent brain layer:

```python
# drewgent/brain/subsystem.py
class DrewgentBrain:
    """Drewgent 7-layer subsumption architecture — Python implementation."""
    P0_BRAINSTEM = "CRITICAL"   # never-do rules
    P1_LIMBIC = "values"        # tone/persona
    P2_HIPPOCAMPUS = "memory"   # context persistence
    P3_SENSORS = "input"        # tool routing
    P4_CORTEX = "growth"        # learning
    P5_EGO = "identity"        # self-model
    P6_PREFRONTAL = "strategy"  # long-term planning

    def __init__(self, vault_path: str):
        self._vault = vault_path
        self._p0_rules = self._load_brain_rules()
        self._p5_self_model = self._load_self_model()

    def enforce(self, layer: str, context: dict) -> EnforcementResult:
        ...

    def check_violation(self, action: str, context: dict) -> Violation | None:
        ...

# drewgent/brain/rules.py
# P0 brainstem rules in Python (not just .neuron vault files)
FORBIDDEN_PATTERNS = [
    (r"rm\s+-rf\s+/\s*\*", "禁rm_rf_root", "CRITICAL"),
    (r"eval\s*\(", "禁dangerous_eval", "CRITICAL"),
    ...
]
```

### Module 6: `drewgent/providers/` — Provider Abstraction

OpenAI, Anthropic, Codex, NousClient 등 client creation/refresh/close 로직 추출:

```
drewgent/providers/
├── __init__.py
├── base.py        — ProviderClient base class
├── openai.py      — _create_openai_client, _close_openai_client
├── anthropic.py   — _anthropic_messages_create, credential refresh
├── codex.py      — _run_codex_stream, credential refresh
└── nous.py        — Nous API credential refresh
```

---

## 파일 생성 순서

```
Step 1: drewgent/brain/subsystem.py + rules.py
Step 2: drewgent/providers/ (base + openai + anthropic)
Step 3: drewgent/runtime/core.py (method grouping)
Step 4: drewgent/runtime/streaming.py
Step 5: drewgent/runtime/prompt.py
Step 6: drewgent/runtime/agent.py (thin wrapper + 위임)
Step 7: run_agent.py → shrinked stub (imports 위임, backwards compat)
```

---

## backwards compatibility

`run_agent.py`는 사라지지 않고, 새 모듈들을 import하는 thin wrapper로 남는다:

```python
# run_agent.py — backwards compat stub
"""run_agent.py — DEPRECATED: import from drewgent.runtime.agent instead"""
from drewgent.runtime.agent import AIAgent
from drewgent.runtime.core import *
from drewgent.runtime.streaming import *
from drewgent.runtime.prompt import *
from drewgent.brain.subsystem import DrewgentBrain

def main():
    from drewgent.runtime.agent import main as _main
    return _main()

if __name__ == "__main__":
    main()
```

이렇게 하면:
- 기존 `python run_agent.py` 여전히 작동
- 에러 메시지로 새 경로 제시
- 점진적 마이그레이션 가능

---

## 검증

```bash
# 모듈 import 확인
python3 -c "from drewgent.runtime.agent import AIAgent; print('OK')"

# 원래랑 똑같이 작동하는지
python3 run_agent.py --help | head -5

# 테스트 실행
cd ~/.drewgent/source/drewgent-agent && python3 -m pytest tests/test_run_agent.py -v -x 2>&1 | head -20
```

---

## 예상 라인 수 감소

| 파일 | Before | After |
|------|--------|-------|
| run_agent.py | 11,604 | ~400 (stub) |
| drewgent/runtime/agent.py | — | ~300 |
| drewgent/runtime/core.py | — | ~2,500 |
| drewgent/runtime/streaming.py | — | ~600 |
| drewgent/runtime/prompt.py | — | ~600 |
| drewgent/brain/subsystem.py | — | ~400 |
| drewgent/brain/rules.py | — | ~200 |
| drewgent/providers/*.py | — | ~1,000 |
| **Total** | **11,604** | **~6,000** |

一半 가까이 감소. gateway/run.py는 다음 단계.

---

## Related

- [[P5-ego/SELF_MODEL]] — Drewgent identity (Python 구현과 동기화)
- [[P0-brainstem/brain/rules]] — P0 rules (Python rules.py와 중복不许)
- [[P4-cortex/growth/INTEGRATION_PROTOCOL]] — integration protocol

---
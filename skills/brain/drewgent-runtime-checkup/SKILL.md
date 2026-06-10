---
name: drewgent-runtime-checkup
title: Drewgent Runtime Checkup
description: Drewgent 코어 시스템 (AIAgent, signal, kanban, dispatcher, worker) 의 기본기를 점검하는 6-Phase 절차. docs와 reality mismatch 발견 시 methodology.
type: skill
space: growth
tags: [skill, checkup, runtime, verification]
created: 2026-06-01
updated: 2026-06-01
links:
  - "[[P3-sensors/gateway/drewgent-architecture-dataflow]]"
  - "[[P2-hippocampus/kanban/KANBAN_INDEX]]"
  - "[[P0-brainstem/brain/Drewgent-brain/P0-brainstem/禁/禁filesystem_truth]]"
---

# Drewgent Runtime Checkup

Drewgent 코어 시스템의 "기본기"를 점검할 때 사용하는 표준 절차. 핵심 철학: **"docs에서 Done이라고 한 것 ≠ 실제 구현"**. 항상 filesystem ground truth 로 verify.

## When to Use

- "기본기 점검해줘", "코어 시스템 확인", "이거 진짜 작동해?" 류 요청
- P0/P1 review 문서가 "✅ Done"이라 한 항목 의심될 때
- Cron job / dispatcher / worker 가 silent failure 중인지 확인할 때
- Major refactor 후 회귀 점검
- 새 모델 / 새 환경에서 Drewgent 설치 직후 sanity check

## 6-Phase Checkup (in order)

### Workdir 주의
터미널 workdir 는 turn 사이에서 휘발됨. 모든 명령은 `cd ~/.drewgent/source/drewgent-agent &&` prefix 필수. 절대 workdir 에 의존하지 말 것.

### Phase 1 — Core Imports (1분)
AIAgent, signal_processor, context_compressor, brain_signals, event_bus 모두 import. **import 실패 = P0 즉시 보고**.

```bash
cd ~/.drewgent/source/drewgent-agent && source .venv/bin/activate
python3 -c "
from run_agent import AIAgent
from agent.signal_processor import get_signal_processor
from agent.context_compressor import ContextCompressor
from agent.brain_signals import get_signal_emitter
print('OK')
"
```

### Phase 2 — Persistent State Health (1분)
SQLite DB 무결성. FK ON. Status 분포.

```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('P2-hippocampus/kanban/state/drewgent_tasks.db')
conn.execute('PRAGMA foreign_keys = ON')
for r in conn.execute('SELECT status, COUNT(*) FROM tasks GROUP BY status'):
    print(r)
print('integrity:', conn.execute('PRAGMA integrity_check').fetchone())
"
```

기대값: FK violations 0, status 7종 (todo/ready/in_progress/blocked/completed/cancelled).

### Phase 3 — Brain Signal Accumulation (1분)
`signal_processor` 인스턴스 state 확인.

```bash
python3 -c "
from agent.signal_processor import get_signal_processor
sp = get_signal_processor()
print('violations:', len(sp._violation_history))
print('dangerous_ops:', len(sp._dangerous_ops_history))
print('workflows:', len(sp._workflow_history))
"
```

기대값: violation ≥ 1, dangerous_ops ≥ 0 (사용 패턴에 따라 다름). 0/0/0이면 signal event bus wiring 끊긴 것.

### Phase 4 — Dispatcher End-to-End (1분)
Cron이 1분마다 도는 dispatcher 직접 실행. ready task 없으면 0/0/0/0 정상.

```bash
python3 ~/.drewgent/scripts/dispatch_once_default.py
# 기대: "reclaimed=0 | claimed=0 | spawned=0 | skipped=0"
```

### Phase 5 — Tool Surface Verification (1분)
`toolsets.py`에 등록된 toolset과 실제 handler 연결.

```bash
python3 -c "
import toolsets
all_toolsets = toolsets.get_all_toolsets()
print('toolsets:', list(all_toolsets.keys()))
print('kanban tools:', all_toolsets.get('kanban', {}).get('tools', []))
"
```

기대값: kanban toolset 안에 13개 function (create/complete/block/unblock/claim/heartbeat/list/get/link/add_comment/get_events 등).

### Phase 5b — Token Estimation Spot-Check (1분, 필요시)
ContextCompressor / chunker 가 silent off-by-N 버그 만들기 쉬운 영역. 변경 후 반드시:

```bash
# 1. Token estimation 정확도 — dict/list 직렬화 + 빈 청크도 인덱싱
python3 -m pytest tests/test_context_compressor.py::TestTokenEstimation -v --no-header
# 기대: 4/4 pass. dict → json.dumps, list → len+str, str → len/4
# 0 pass면: source/drewgent-agent/agent/context_compressor.py 의 _estimate_tokens() 확인

# 2. Compression chunking — 1k 윈도우 세션 + DM topic 전환
python3 -m pytest tests/test_context_compressor.py -k "chunk or topic or window" -v --no-header
# 기대: 4/4 pass. CharacterTextSplitter 의 empty chunk 가 인덱싱되는지 확인

# 3. 주변 test file 도 함께 — fixture gap 발견용
python3 -m pytest tests/gateway/test_session_hygiene.py --no-header -q
# 한두 개 fail 이어도 AttributeError 면 fixture 문제 (다음 항목 참고), 진짜 버그 아님
```

**3-tier chunking 버그 이력** (2026-06-01):
- dict 를 str(dict) 로 estimate → 1000x overcount
- list 를 str(list) 로 estimate → 같은 문제
- CharacterTextSplitter 가 빈 청크를 skip → 인덱싱 누락

수정 패턴:
```python
def _estimate_tokens(text: str) -> int:
    if isinstance(text, dict): return _estimate_tokens(json.dumps(text))
    if isinstance(text, list): return sum(_estimate_tokens(t) for t in text) if text else 0
    return len(text) // 4
```

## CRITICAL DIAGNOSTIC — AttributeError = Fixture Gap, Not Real Bug

`object.__new__(GatewayRunner)` 기반 mock runner 테스트에서 자주 등장:

```python
AttributeError: 'GatewayRunner' object has no attribute '_session_manager'
AttributeError: 'GatewayRunner' object has no attribute '_dispatcher'
AttributeError: 'GatewayRunner' object has no attribute '_sentinel_guard'
```

**이건 진짜 버그가 아니라 mock-fixture 갭**. GatewayRunner 가 decomp 됨에 따라 mock 이 새 속성을 모름.

**대응**:
1. 이 AttributeError 는 fix 시도하지 말 것 (checkup 중 code change 금지)
2. Active task list 에 fixture 갭이 추적되고 있는지 확인 (예: task #14 = mock fixture 갭)
3. 보고에 P1 으로 "fixture 갭 N개" 만 적고 defer
4. 잘못 고치면 mock 이 실제 prod 코드를 가리키게 되어 false positive 발생

**진짜 버그 vs fixture 갭 구분**:

| 증상 | 진단 |
|------|------|
| `AttributeError: 'X' object has no attribute '_Y'` | fixture 갭 (defer) |
| `assert X == Y` 실패 | 진짜 버그 (조사 필요) |
| `KeyError: 'Z'` | 진짜 버그 (DB/mapping 문제) |
| `TypeError: ... argument ...` | 진짜 버그 (signature mismatch) |
| Timeout / hang | 환경/순환 문제 (별도) |

### Phase 4b — Cron-Runner Wrapper Registration (필요시)

**Symptoms (문서엔 Done, 실제로는 silent)**:
- `jobs.json`에 cron job entry가 없는데 script는 `~/.drewgent/scripts/`에 존재
- `last_run_at`이 며칠 전에서 멈춤
- `launchctl list | grep drewgent` → ai.drewgent.gateway 또는 ai.drewgent.cron-runner plist 없음
- 3개 board dispatcher (default/content/integrations) 중 일부만 jobs.json에 등록됨

**원인**: jobs.json은 declarative한 record 일 뿐, 실제 실행은 **launchd plist + Python wrapper** 가 담당. jobs.json에 등록 안 된 script는 절대 자동 실행 안 됨.

**해결 패턴 — Cron-Runner Wrapper + Single LaunchAgent**:

```bash
# 1. Wrapper script 작성
cat > ~/.drewgent/scripts/cron_runner.py << 'EOF'
#!/usr/bin/env python3
"""Drewgent cron-runner — runs all 3 board dispatchers in sequence."""
import subprocess, sys
from pathlib import Path
DREW = Path.home() / '.drewgent'
BOARDS = ['default', 'content', 'integrations']
for b in BOARDS:
    s = DREW / 'scripts' / f'dispatch_once_{b}.py'
    if s.exists():
        subprocess.run([sys.executable, str(s)], check=False)
EOF
chmod +x ~/.drewgent/scripts/cron_runner.py

# 2. LaunchAgent plist
cat > ~/Library/LaunchAgents/ai.drewgent.cron-runner.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.drewgent.cron-runner</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/drew/.drewgent/source/drewgent-agent/.venv/bin/python</string>
    <string>/Users/drew/.drewgent/scripts/cron_runner.py</string>
  </array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>/Users/drew/.drewgent/logs/cron-runner.log</string>
  <key>StandardErrorPath</key><string>/Users/drew/.drewgent/logs/cron-runner.err</string>
</dict>
</plist>
EOF

# 3. Load
launchctl load ~/Library/LaunchAgents/ai.drewgent.cron-runner.plist
launchctl start ai.drewgent.cron-runner
```

**검증**:
```bash
launchctl list | grep drewgent  # ai.drewgent.cron-runner PID 있어야
ls -lt ~/.drewgent/logs/cron-runner.log  # 1분마다 append
tail ~/.drewgent/logs/cron-runner.log  # 3 dispatcher 모두 spawn 시도
```

**핵심**: jobs.json은 "declarative record" (UI/관리용), 실제 실행은 launchd → Python wrapper → 3 dispatcher. jobs.json만 믿지 말 것. launchctl list가 single source of truth.

**False positive 패턴** (2026-06-01 발견):
- `last_status=ok` 가 jobs.json에 있더라도 last_run_at이 stale 가능. 진짜 신호는 launchd PID + 로그 append.
- `enabled=true` 필드는 사람이 봤을 때 활성 표시일 뿐, 실행 보장 아님.

### Phase 4c — Dead Worker Board 격리 (필요시)

**증상**: "task stuck in in_progress forever" / "t_dead_worker_autotest 같은 task가 reclaim 안 됨"

**진단**:
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('~/.drewgent/P2-hippocampus/kanban/state/drewgent_tasks.db')
for r in conn.execute('SELECT id, title, board, worker_pid FROM tasks WHERE status=\"in_progress\"'):
    print(r)
"
```

**판단 기준**:
- worker_pid != NULL + ETIME < 30s → 정상 (worker 처리 중)
- worker_pid != NULL + ETIME > 1h + ps에서 PID 없음 → DEAD, reclaim 필요
- board = "test" 또는 board = "default"이고 title = "Test ..." → **false alarm**: 격리된 test board, dispatcher 무관

**False positive 회피**:
- test/* board에 dead worker 격리돼 있으면 default dispatcher가 안 보는 게 정상
- board scope hardening v0.8.5 적용 후 cross-board reclaim 시도 없음
- 진짜 bug: production task (board=default/content/integrations) + real worker_pid + TTL expired

### Phase 6 — Integration Path Spot-Check (1분)
**"✅ Done이라 doc에 적혀 있지만 진짜?"** — 이 phase가 가장 자주 함정 있음.

```bash
# 1. dispatcher가 spawn할 때 어떤 env를 worker에 넘기는지
grep -A 10 "subprocess.Popen" ~/.drewgent/scripts/dispatch_once_default.py
# 2. worker가 그 env를 어떻게 read하는지
grep -E "os\.environ\[" ~/.drewgent/scripts/run_kanban_worker.py
# 3. Python tool 코드에서 그 env를 참조하는지
grep -rln "KANBAN_WORKER_MODE" ~/.drewgent/source/drewgent-agent --include="*.py"
```

## CRITICAL PITFALL — Shell Env Var Pattern

Drewgent는 자주 **shell env로 subprocess에 mode를 전달**한다. Python 코드에 그 env var가 안 보일 수 있다. 이게 **"미구현"이 아니라 "정상 구현"** 인 경우:

| Env var | Where set | Where read | Why |
|---------|-----------|------------|-----|
| `KANBAN_TASK_ID` | `dispatch_once_default.py` (subprocess.Popen env=) | `run_kanban_worker.py` (os.environ) | dispatcher가 어떤 task 줄지 worker에게 알림 |
| `KANBAN_WORKER_MODE=1` | `dispatch_once_default.py` | `run_kanban_worker.py` | worker가 mode=1 감지하면 LLM bypass, 직접 sqlite3 |
| `KANBAN_BOARD` | dispatcher | worker | multi-board 라우팅 |
| `DREW_HOME` | parent process | worker | 경로 override |
| `KANBAN_WORKER_PID` | dispatcher | worker | spawn tracker |

**Rule of thumb**: "Python code에 안 보임" → "Not implemented" 결론 **금지**. 항상:
1. `~/.drewgent/scripts/*.py` 확인 (dispatcher / worker script는 `source/drewgent-agent` 밖)
2. `subprocess.Popen(env=...)` 의 `env.update({...})` dict 확인
3. `os.environ.get("VAR")` 호출 위치 확인
4. 그래도 0건이면 그때 "미구현" 결론

## Verification Examples

### Good (실제 구현 검증)
- dispatch_once_default.py: `env.update({'KANBAN_TASK_ID': task_id, 'KANBAN_WORKER_MODE': '1', ...})` ✓
- run_kanban_worker.py: `task_id = os.environ.get("KANBAN_TASK_ID")` ✓
- → 두 파일이 shell env로 연결됨. **정상 동작**

### Bad (false negative)
- Python 코드에 `KANBAN_WORKER_MODE` 없음 → "구현 안 됨" 보고
- (실제로는 shell env로 전달 중)
- → **잘못된 진단**

## Output Format

점검 끝나면 다음 형식으로 보고:

```
[P0] Core imports: OK
[P0] DB health: FK ok, status 분포 정상 (7종)
[P0] Brain signal: violations=N, dangerous_ops=M
[P0] Dispatcher: claimed=0 spawned=0 (정상, ready task 없음)
[P0] Tool surface: kanban toolset 13개 등록됨
[P1] X 발견: stdout=PIPE pipe full 위험
[P1] Y 발견: watchdog 부재 (TTL=1h lag)
[P2] Z 권장: docs 표현 정정
```

P0는 critical (한 줄로 fix 필요), P1은 단기 개선, P2는 장기 검토.

## Files Likely Worth Touching

점검 후 발견되는 P1 issue는 보통:
- `~/.drewgent/scripts/dispatch_once_*.py` (3개 board) — stdout=PIPE → DEVNULL 변경
- `~/.drewgent/scripts/run_kanban_worker.py` — watchdog 추가
- `P4-cortex/growth/KANBAN-REVIEW-20260520.md` — docs 표현 정정 ("Python mode 분기" → "shell env flag")

## Related

- [[P3-sensors/gateway/drewgent-architecture-dataflow]] — 전체 데이터 흐름
- [[P2-hippocampus/kanban/KANBAN_INDEX]] — kanban 시스템 개요
- [[P0-brainstem/brain/Drewgent-brain/P0-brainstem/禁/禁filesystem_truth]] — "files are truth" 원칙

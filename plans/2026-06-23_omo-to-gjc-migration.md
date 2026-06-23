---
title: OMO → GJC Migration
trigger: "OMO (opencode subagent system) keeps stopping; replace with Gajae-Code Coordinator MCP"
provenance:
  session: "2026-06-23 gajae-code-evaluation"
  decision: "GJC worktree isolation + tmux parallelism + structured workflow solves OMO freezing without shared-process LLM fragility"
created: 2026-06-23
---

# 대전제

OMO (opencode native subagent system = delegate.ts + agents/*.md)를 제거하고 GJC (Gajae-Code Coordinator MCP)로 대체한다.

---

## 삭제 대상 (OMO)

| # | 파일 | 용도 | 삭제? |
|---|------|------|-------|
| 1 | `~/.config/opencode/tools/delegate.ts` | opencode run --agent 래퍼 (모델 라우팅) | **삭제** |
| 2 | `~/.config/opencode/agents/*.md` (15개) | subagent 프로필 + ARD 메타데이터 | **삭제** |
| 3 | `AGENTS.md` "Native Agent System", "Agent Office" 섹션 | 문서화 | **재작성** |
| 4 | Skills 내 delegate/subagent_type 참조 | kanban-orchestrator, subagent-driven-dev 등 | **패치** |

## 추가 대상 (GJC)

| # | 파일 | 용도 |
|---|------|------|
| 1 | `~/.config/opencode/opencode.jsonc` | GJC Coordinator MCP 서버 등록 |
| 2 | GJC 설치 | `bun install -g gajae-code` |
| 3 | `AGENTS.md` GJC 섹션 | 새 아키텍처 문서화 |
| 4 | Skill: gajae-code-integration (선택) | GJC 호출 패턴 문서화 |

## 영향 분석

### 유지되는 기능
- `task(subagent_type="...")` — **opencode 내장**. agents/*.md 없이도 작동. agent 타입은 opencode built-in.
- Discord bot, cron, launchd — 변경 없음. 단, `opencode run --agent` → `opencode run`으로 대체.

### 변경되는 기능
- **Cron: wiki-lint** (`explorer agent`) → `opencode run --skill wiki-lint` or GJC task
- **Cron: wiki-compile** (`archiver agent`) → `opencode run --skill wiki-compile` or GJC task
- **Office autopilot** (`orchestrator agent`) → GJC Coordinator MCP or plain opencode run
- **ARD catalog** → agents/*.md 삭제로 ARD publish 무효화. `ai-catalog.json` 업데이트 필요

### 위험
- `opencode run --agent <name>` 완전히 사라짐. cron에서 쓰는 agent 호출 방식 변경 필요.
- ARD catalog에서 agent profile 참조 깨짐. `ai-catalog.json` 패치 필요.
- GJC 미설치 시 Coordinator MCP 실패. 설치 단계 필수.

---

## Step-by-step

### Step 1: GJC 설치
```bash
bun install -g gajae-code
gjc --version  # v0.7.0 확인
gjc --smoke-test  # 정상 작동 확인
```

### Step 2: opencode.jsonc에 GJC Coordinator MCP 등록
```jsonc
{
  "mcp": {
    "gajae-code": {
      "type": "local",
      "command": ["gjc", "mcp-serve", "coordinator"]
    },
    // 기존 discord, wordpress 유지
  }
}
```

### Step 3: delegate.ts 삭제
```bash
rm ~/.config/opencode/tools/delegate.ts
```
→ `delegate("implementer", prompt="...")` 툴 사라짐. 앞으로는 GJC Coordinator MCP 툴을 쓰거나 직접 작업.

### Step 4: agents/*.md 삭제 (15개)
```bash
rm ~/.config/opencode/agents/*.md
```

### Step 5: Cron jobs patch
`AGENTS.md` cron 테이블에서 agent 호출 방식 변경:
- `opencode run --agent explorer` → `opencode run` (plain)
- `opencode run --agent archiver` → `opencode run` (plain)
- `opencode run --agent orchestrator` → GJC delegate or plain opencode run

실제 cron dispatcher 스크립트에서 agent 플래그 사용하는 부분 확인 및 패치.

### Step 6: ARD catalog 업데이트
`~/.drewgent/.well-known/ai-catalog.json`에서 agents/*.md 기반 entry 제거.
GJC Coordinator MCP를 ARD에 등록할지 결정 (차후 과제).

### Step 7: AGENTS.md 재작성
- "Native Agent System" → "GJC Agent System"으로 대체
- "Agent Office" 섹션 제거
- delegate.ts 참조 제거
- GJC Coordinator MCP + task() 조합으로 새 워크플로 문서화
- cron 테이블 업데이트

### Step 8: Skills 패치
영향받는 skills:
- `kanban-orchestrator` — delegate/subagent_type 참조 → GJC Coordinator MCP 참조
- `kanban-worker` — subagent_type 참조 유지 (task는 살아있음)하지만 delegate 참조 제거
- `subagent-driven-development` — delegate 참조 → GJC 참조
- `subagent-profiles` — 사실상 무효화. GJC skill로 대체할지 결정

---

## 사후 검증

- [ ] `gjc --version` 정상 출력
- [ ] `opencode.jsonc`에서 GJC MCP 서버 로드 확인
- [ ] Crontab 스크립트 정상 실행 (wiki-lint, wiki-compile)
- [ ] `delegate()` 툴 더 이상 안 보임
- [ ] `task()` 정상 작동
- [ ] GJC Coordinator MCP 툴 (`gjc_delegate_*`) 사용 가능

---
name: agent-dashboard-cf
title: Agent Dashboard (Cloudflare Workers)
description: "Drewgent 에이전트 상태를 Cloudflare Workers + KV로 호스팅, 5분마다 로컬 pusher가 업데이트. 16:9 3열 레이아웃, Health Bar, vis-network 그래프 뷰 포함"
trigger: "Drewgent 에이전트 상태를 Cloudflare Workers에 호스팅된 대시보드로 실시간 확인. 유저가 '에이전트 대시보드'를 요청할 때"
provenance:
  session: "2026-06-15 agent-dashboard"
  decision: "CF Worker + 정적 assets + KV + 로컬 pusher 조합. Worker는 API 전용, HTML은 정적 파일. Pusher는 Python 스크립트로 5분마다 hermes CLI 출력 파싱 후 POST. Health Bar는 항상 상단 고정, 16:9 3열 그리드."
created: 2026-06-15
updated: 2026-06-15
---

# Agent Dashboard (Cloudflare Workers)

로컬 Drewgent 상태를 Cloudflare Workers에 호스팅된 대시보드로 5분마다 업데이트.
**16:9 3열 그리드 레이아웃**이 기본. Health Bar 상단 고정.

## 아키텍처

```
로컬 Mac                          Cloudflare
──────────                       ──────────
pusher script (5분마다) ──POST──→ Worker API (/api/push)
  ├─ system stats  (uptime,load,disk,mem)    │ KV storage
  ├─ launchctl list (ai.drewgent.* 서비스)    │   ├── latest (현재 상태)
  ├─ hermes kanban list                      │   ├── history:YYYY-MM-DD (일별)
  ├─ hermes cron list                        │   └── health/errors/graph...
  ├─ listening ports                         │
  ├─ vault P-layer sizes                     │  Browser ──GET──→ / (HTML dashboard)
  ├─ vault wikilink graph (196+ nodes)       │              └─→ /api/status (JSON)
  ├─ recent error logs (errors.log + agent.log)
  └─ health summary (compute_health_status)
```

## 구성 요소

### 1. Cloudflare Worker (\`~/Sites/agent-dashboard/\`)
- \`src/index.js\` — API 핸들러 (POST push, GET status, GET history)
- \`public/index.html\` — 대시보드 UI (아래 레이아웃 참조)
- \`wrangler.toml\` — KV 바인딩 + 정적 assets 설정

### 2. 로컬 Pusher 스크립트 (\`~/.drewgent/scripts/agent_dashboard_push.py\`)
- no-agent cron job (script-only 모드)
- 환경변수: \`AGENT_DASHBOARD_URL\`, \`AGENT_DASHBOARD_SECRET\`
- \`--dry-run\` 으로 수집만 하고 POST 안 함
- **주의: PATH 설정 필수** — cron(no-agent) 실행 시 \`hermes\` CLI를 못 찾음.
  \`_EXTRA_PATH\`에 \`~/.local/bin\`, \`~/.hermes/hermes-agent/.venv/bin\`, \`/opt/homebrew/bin\` 포함해야 함.
  \`run()\` 함수에 \`env=env\` 전달 필수.

### 3. Cron Job
- 이름: \`agent-dashboard-push\` (job_id: \`31b99fb1d65e\`)
- 주기: every 5m, no_agent: true

## 수집 데이터 전체 목록

| 수집기 | 출력 | 비고 |
|--------|------|------|
| \`collect_system()\` | uptime, load, disk, memory, os, python, hermes | \`uptime\`, \`df\`, \`vm_stat\`, \`sw_vers\` |
| \`collect_launchd()\` | ai.drewgent.* 서비스 목록 + PID | \`launchctl list\` 파싱 |
| \`collect_kanban()\` | tasks, blocked/ready/todo 카운트 | \`hermes kanban list\` 파싱. 유니코드 아이콘(⊘=blocked, ◻=todo, ▶=ready) |
| \`collect_cron()\` | active/errors/paused jobs | \`hermes cron list\` 박스드로잉 포맷 파싱. 'error:' 접두사 처리 |
| \`collect_network()\` | known ports listening/down | \`lsof -iTCP -sTCP:LISTEN\` |
| \`collect_vault()\` | P-layer별 디렉토리 크기 | \`du -sh\` |
| \`collect_sessions()\` | 최근 세션 미리보기 | \`hermes sessions list\` |
| \`collect_git()\` | uncommitted/unpushed 카운트 | \`git status --porcelain \| wc -l\` |
| \`collect_brew()\` | outdated 패키지 수 | \`brew outdated \| wc -l\` |
| \`collect_docker()\` | 실행 중인 컨테이너 | \`docker ps\` |
| \`collect_thermal()\` | 온도/배터리 상태 | \`pmset -g therm\`, \`pmset -g batt\` |
| \`collect_graph()\` | vault wikilink 그래프 (nodes+edges) | \`[[wikilink]]\\)\\) 정규식 스캔. P2 제외. 최대 300노드 |
| \`collect_recent_errors()\` | 최근 8개 에러 로그 | errors.log + agent.log tail 200KB, summary= 필드 추출 |
| \`compute_health_status()\` | 전체 건강 요약 (level/critical/warning/issues) | disk+cron+errors 종합 |

## 대시보드 HTML 레이아웃

```
┌──────────────────────────────────────────────────────────────┐
│ 🟢/🟡/🔴 HEALTH BAR  (sticky, 0 critical · N warnings)       │  ← compute_health_status()
├──────────────────────────────────────────────────────────────┤
│ Header · Status Bar (6 cards: Uptime/CPU/Disk/Svc/Kanban/Cron)│
├──────────────────────────────────────────────────────────────┤
│ Alerts (현재 문제 있을 때만 표시)                              │
├─────────────────┬────────────────────┬───────────────────────┤
│ COL 1 (25%)     │ COL 2 (37%)        │ COL 3 (38%)           │
│ 🖥️  System      │ 📋 Kanban Board    │ ⏰ Cron Jobs          │
│ 🔌 Gateway      │ 🌐 Network         │ 💬 Sessions           │
│ ⚙️  Services    │ 🐳 Docker          │                       │
│ 📁 Vault        │ 🧊 Thermal         │                       │
│ 📄 Git Status   │ ❌ Recent Errors   │                       │
│ 🍺 Brew         │                    │                       │
├─────────────────┴────────────────────┴───────────────────────┤
│ 📊 Vault Graph (vis-network, forceAtlas2Based, drag/zoom)     │
└──────────────────────────────────────────────────────────────┘
```

### Health Bar 디자인
- \`position: sticky; top: 0; z-index: 100\`
- 색상: \`healthy\`=초록, \`warning\`=노랑, \`critical\`=빨강
- 내용: 아이콘 + 상태문구 + critical/warning 카운트 + 이슈 요약 + 최근 에러 수

## 주의사항

### 크론 파서 (\`collect_cron()\`)
- \`hermes cron list\` 출력 포맷에 의존. 포맷 변경 시 수정 필요.
- 박스드로잉 문자(┌└│─) 스킵, \`<job_id> [active]\` 패턴으로 job 감지
- \`Last run:\` 줄에서 \`error:\` 접두사 처리 (\`startswith("error")\`로 체크, \`== "error"\` 아님)

### Kanban 파서 (\`collect_kanban()\`)
- 유니코드 아이콘 기반: ⊘=blocked, ◻=todo, ▶=ready, ●=running
- \`parts[0]\`=아이콘, \`parts[1]\`=ID, \`parts[2]\`=status문자열(무시), \`parts[3]\`=assignee, \`parts[4:]\`=title
- status는 아이콘에서 추론, \`parts[2]\`는 텍스트 상태값 (의미 없음)

### PATH 문제 (cron no-agent 모드)
- pusher 스크립트가 cron(no-agent)으로 실행될 때 \`hermes\` CLI를 못 찾음
- 해결: \`_EXTRA_PATH\`에 \`~/.local/bin\`, \`~/.hermes/hermes-agent/.venv/bin\`, \`/opt/homebrew/bin\` 추가
- \`run()\` 함수에 \`env = {**_EXTRA_ENV, **os.environ}\` 전달

### Cloudflare WAF
- Python \`urllib\` 기본 User-Agent가 Cloudflare WAF에 차단됨 (HTTP 403 error code 1010)
- 해결: \`User-Agent: Mozilla/5.0 ...\` 헤더 추가

### Vault 그래프 (\`collect_graph()\`)
- \`[[wikilink]]\\)\\) 패턴 정규식으로 추출
- P2-hippocampus(11GB) 제외, 각 레이어당 60파일 제한, 총 300노드 제한
- 파일당 20개 링크 제한, 100KB 초과 파일 스킵
- vis-network CDN(\`https://unpkg.com/vis-network/standalone/umd/vis-network.min.js\`) 필요
- physics: forceAtlas2Based (gravitationalConstant: -30~-40, springLength: 100~120)

### 에러 로그 수집 (\`collect_recent_errors()\`)
- errors.log + agent.log tail 200KB 읽기
- 정규식: \`(날짜) (ERROR|WARNING|CRITICAL).*?(?:summary=|error=)(메시지)\`
- 최대 8개, 메시지 앞 60자로 중복 제거

## 배포

\`\`\`bash
cd ~/Sites/agent-dashboard
wrangler kv namespace create AGENT_DASHBOARD  # 최초 1회
wrangler deploy
\`\`\`

## pusher 직접 실행

\`\`\`bash
# dry-run (POST 안 함)
python3 ~/.drewgent/scripts/agent_dashboard_push.py --dry-run

# 실제 push
python3 ~/.drewgent/scripts/agent_dashboard_push.py
\`\`\`

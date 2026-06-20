#!/bin/bash
# drewgent_launchd_watchdog.sh
# 5분마다 launchd 서비스 상태를 검사하고, 1개 이상 PID=-면 Discord webhook으로 알림.
# P0-1 watchdog (2026-06-10 incident). no_agent=True cron job과 함께 등록.
#
# Hard evidence: launchctl list | grep -E "drewgent|hermes" 의 PID 컬럼.
# PID가 '-' (none) 이고 last exit code가 0이 아니면 비정상.
#
# 출력 형식:
#   ✅ all ok: 5/5 services running
#   ⚠ alerts: <서비스명> PID=- exit=N
# Discord로 webhook POST (HERMES_DISCORD_WEBHOOK 환경변수에서 읽음).

set -euo pipefail

WATCHED_LABELS=(
  "ai.drewgent.cron"
  "ai.drewgent.discord-bot"
  "ai.drewgent.opencode"
)

# gateway는 launchctl에 등록돼있어도 process가 detached일 수 있음 (cron-runner의
# D2 패턴). PID=- 가 soft evidence일 뿐, 진짜 비정상 여부는 PID + exit code + uptime
# 종합으로 판단. 단순 watchdog는 PID 컬럼만 본다.

alerts=()
ok_count=0

for label in "${WATCHED_LABELS[@]}"; do
  # launchctl list 출력: "<pid>\t<last exit code>\t<label>"
  line=$(launchctl list 2>/dev/null | awk -v lbl="$label" '$3 == lbl { print; exit }')
  if [ -z "$line" ]; then
    alerts+=("❌ $label: not registered")
    continue
  fi
  pid=$(echo "$line" | awk '{print $1}')
  exit_code=$(echo "$line" | awk '{print $2}')
  if [ "$pid" = "-" ]; then
    if [ "$exit_code" = "0" ]; then
      # PID=- 이지만 exit=0 이면 graceful stop — 경고 아님
      ok_count=$((ok_count+1))
    else
      alerts+=("⚠ $label: PID=- exit=$exit_code")
    fi
  else
    ok_count=$((ok_count+1))
  fi
done

total=${#WATCHED_LABELS[@]}
timestamp=$(date '+%Y-%m-%d %H:%M:%S %Z')

if [ ${#alerts[@]} -eq 0 ]; then
  # 전부 ok — 침묵. (no_agent=True 패턴: empty stdout = silent)
  exit 0
fi

# 알림 전송
message="🚨 **Drewgent launchd watchdog** @ $timestamp
$ok_count/$total services running

${alerts[*]}

Fix:
- launchctl kickstart -k gui/\$(id -u)/<label>
- 또는: launchctl unload ~/Library/LaunchAgents/<label>.plist && launchctl load ~/Library/LaunchAgents/<label>.plist
- 자동 재시작이 안 되면 plist의 KeepAlive 누락 — P1-4 작업 확인"

# HERMES_DISCORD_WEBHOOK 환경변수가 있을 때만 Discord로 POST
if [ -n "${HERMES_DISCORD_WEBHOOK:-}" ]; then
  curl -s -X POST -H "Content-Type: application/json" \
    -d "$(jq -n --arg c "$message" '{content: $c}')" \
    "$HERMES_DISCORD_WEBHOOK" >/dev/null
fi

# stdout 출력 (cron job이 캡처해서 owner에게 전달)
printf '%s\n' "$message"

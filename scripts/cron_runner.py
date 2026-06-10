#!/usr/bin/env python3
"""
Cron Runner — launchd StartInterval 60s로 실행됨.
jobs.json의 dispatcher entry들에 대응하는 결정론적 shell script들을 순차 실행.

Why this exists (2026-06-01):
- ai.drewgent.cron-runner plist 부재 → 5/30 21:55부터 5개 cron job이 dormant.
- ai.drewgent.gateway.plist 파일명은 살아있지만 Label이 ai.custom-agent.gateway로 rename → conflict로 load 안 됨.
- dispatcher script는 LLM 호출 없는 결정론적 sqlite3 script라서 외부 plist로 충분히 실행 가능.
- jobs.json entry는 declarative record로 남겨둠 (Drewgent가 자기 task queue를 인식).

Output: logs/cron-runner/YYYY-MM-DD.log
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DREW_HOME = Path(os.environ.get("DREW_HOME", str(Path.home() / ".drewgent")))
LOG_DIR = DREW_HOME / "logs" / "cron-runner"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# dispatcher scripts — board별로 1개씩, jobs.json의 name과 매핑
DISPATCHERS = [
    ("default", "dispatch_once_default.py"),
    ("content", "dispatch_once_content.py"),
    ("integrations", "dispatch_once_integrations.py"),
]

VENV_PY = DREW_HOME / "source" / "drewgent-agent" / ".venv" / "bin" / "python"
CWD = DREW_HOME / "source" / "drewgent-agent"

ts = datetime.now(timezone.utc).isoformat()
log_file = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"

results = []
for board, script_name in DISPATCHERS:
    script_path = DREW_HOME / "scripts" / script_name
    if not script_path.exists():
        results.append(f"[{board}] {script_name}: SKIP (file not found)")
        continue
    try:
        r = subprocess.run(
            [str(VENV_PY), str(script_path)],
            capture_output=True,
            text=True,
            timeout=50,  # 다음 tick 10초 여유
            cwd=str(CWD),
            env={**os.environ, "DREW_HOME": str(DREW_HOME)},
        )
        # stdout 마지막 5줄 추출 (요약 정보)
        out_lines = [l for l in r.stdout.strip().splitlines() if l.strip()][-5:]
        out_summary = " | ".join(out_lines) if out_lines else "(no output)"
        err_summary = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
        results.append(
            f"[{board}] {script_name}: exit={r.returncode} | {out_summary}"
            + (f" | stderr={err_summary}" if err_summary else "")
        )
    except subprocess.TimeoutExpired:
        results.append(f"[{board}] {script_name}: TIMEOUT (50s)")
    except Exception as e:
        results.append(f"[{board}] {script_name}: ERROR {type(e).__name__}: {e}")

# Write to daily log
with open(log_file, "a") as f:
    f.write(f"\n=== {ts} ===\n")
    for r in results:
        f.write(f"  {r}\n")

# stdout: brief summary for launchd log
print(f"[{ts}] cron_runner: {len(results)} dispatchers run")
for r in results:
    print(f"  {r}")

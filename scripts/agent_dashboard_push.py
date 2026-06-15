#!/usr/bin/env python3
"""
agent_dashboard_push.py — Collect local agent status and push to Cloudflare dashboard.

Runs every 5 minutes via cron. Gathers:
  - System stats (uptime, load, disk, memory)
  - Launchd services (ai.drewgent.* + ai.hermes.*)
  - Kanban board
  - Cron jobs
  - Network services
  - Vault P-layer sizes
  - Recent sessions

Usage:
  python3 agent_dashboard_push.py [--dry-run] [--endpoint URL] [--secret TOKEN]
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

HOME = os.path.expanduser("~")
DREWGENT = os.path.join(HOME, ".drewgent")
HERMES = os.path.join(HOME, ".hermes")

# Ensure hermes CLI is findable when run from cron (no-agent mode)
_EXTRA_PATH = os.pathsep.join([
    os.path.join(HOME, ".local", "bin"),
    os.path.join(HOME, ".hermes", "hermes-agent", ".venv", "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
])
_EXTRA_ENV = {"PATH": _EXTRA_PATH + os.pathsep + os.environ.get("PATH", "")}

# Defaults — override via env or CLI
ENDPOINT = os.environ.get("AGENT_DASHBOARD_URL", "https://agent-dashboard.humanerd-me.workers.dev")
PUSH_SECRET = os.environ.get("AGENT_DASHBOARD_SECRET", "")


def run(cmd, timeout=15):
    """Run a shell command, return (stdout, stderr, exit_code)."""
    try:
        env = {**_EXTRA_ENV, **os.environ}
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, shell=True, env=env
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except Exception as e:
        return "", str(e), -1


def collect_system():
    """Gather system-level stats."""
    out, _, _ = run("uptime")
    uptime = out.replace(",", "").strip() if out else "?"

    out, _, _ = run("sysctl -n vm.loadavg")
    load = out.strip() if out else "?"

    out, _, _ = run(r"df -h /System/Volumes/Data 2>/dev/null | tail -1")
    disk = out.split() if out else []
    disk_used = disk[2] if len(disk) > 2 else "?"
    disk_total = disk[1] if len(disk) > 1 else "?"
    disk_pct = disk[4] if len(disk) > 4 else "?"
    if disk_pct.endswith("%"):
        disk_pct = disk_pct[:-1]

    out, _, _ = run("vm_stat 2>/dev/null | head -10")
    mem_lines = out.split("\n") if out else []
    pages_active = "?"
    pages_free = "?"
    for line in mem_lines:
        if "Pages active" in line:
            pages_active = line.split(":")[-1].strip().rstrip(".")
        if "Pages free" in line:
            pages_free = line.split(":")[-1].strip().rstrip(".")

    out, _, _ = run("sw_vers -productVersion 2>/dev/null")
    os_version = out.strip() or "?"

    out, _, _ = run("uname -r")
    kernel = out.strip() or "?"

    out, _, _ = run("python3 --version 2>/dev/null")
    python = out.strip() or "?"

    out, _, _ = run(HERMES + "/hermes-agent/.venv/bin/python -c \"import hermes; print(hermes.__version__)\" 2>/dev/null || echo '?'")
    hermes_ver = out.strip() or "?"

    return {
        "uptime": uptime,
        "load": load,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_used_pct": disk_pct,
        "memory": f"active: {pages_active}, free: {pages_free}",
        "os_version": os_version,
        "kernel": kernel,
        "python": python,
        "hermes_version": hermes_ver,
    }


def collect_launchd():
    """Collect ai.drewgent.* and ai.hermes.* launchd services."""
    out, _, _ = run("launchctl list 2>/dev/null")
    services = []
    for line in (out.split("\n") if out else []):
        parts = line.strip().split()
        if len(parts) >= 3 and ("ai.drewgent." in parts[2] or "ai.hermes." in parts[2]):
            pid_str = parts[0]
            exit_str = parts[1]
            label = parts[2]
            try:
                pid = int(pid_str) if pid_str != "-" else -1
            except ValueError:
                pid = -1
            try:
                exit_code = int(exit_str) if exit_str != "-" else None
            except ValueError:
                exit_code = None
            services.append({
                "label": label,
                "pid": pid,
                "exit_code": exit_code,
            })
    return services


def collect_kanban():
    """Parse hermes kanban list output."""
    out, _, rc = run("hermes kanban list 2>/dev/null")
    tasks = []
    if out:
        for line in out.split("\n"):
            line = line.strip()
            if not line or "───" in line or "┌" in line or "└" in line or "│" in line or "─" in line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            # Parse status icon
            icon = parts[0]
            status_map = {"\u2298": "blocked", "\u25ef": "todo", "\u25b6": "ready",
                          "\u25cf": "running", "\u2713": "done", "\u25fc": "done"}
            status = status_map.get(icon, "?")

            if len(parts) >= 5:
                task_id = parts[1]
                assignee = parts[3]
                title = " ".join(parts[4:])
            elif len(parts) >= 4:
                task_id = parts[1]
                assignee = parts[3]
                title = ""
            elif len(parts) >= 2:
                task_id = parts[1]
                assignee = ""
                title = ""
            else:
                continue
            tasks.append({
                "id": task_id,
                "title": title,
                "status": status,
                "assignee": assignee,
            })

    blocked = sum(1 for t in tasks if t["status"] == "blocked")
    ready = sum(1 for t in tasks if t["status"] == "ready")
    todo_count = sum(1 for t in tasks if t["status"] == "todo")
    running = sum(1 for t in tasks if t["status"] == "running")

    return {
        "total": len(tasks),
        "blocked": blocked,
        "ready": ready,
        "todo": todo_count,
        "running": running,
        "tasks": tasks,
    }


def collect_cron():
    """Parse hermes cron list output — handles the box-drawing format."""
    out, _, _ = run("hermes cron list 2>/dev/null")
    active = []
    errors = []
    paused = []

    if not out:
        return {"active": [], "errors": [], "paused": []}

    lines = out.split("\n")
    current = None

    for line in lines:
        raw = line
        stripped = line.strip()

        # Skip decorative lines
        if not stripped or stripped.startswith("\u2500") or stripped.startswith("\u250c") or \
           stripped.startswith("\u2514") or stripped.startswith("\u2502"):
            continue

        # Detect job header: "  <job_id> [state]"
        if stripped.endswith("]") and "[" in stripped:
            # Save previous job
            if current:
                _cron_classify(current, active, errors, paused)

            bracket_idx = stripped.index("[")
            jid = stripped[:bracket_idx].strip()
            state = stripped[bracket_idx + 1:-1]
            current = {
                "job_id": jid,
                "state": state,
                "name": "",
                "schedule": "",
                "last_status": "",
                "last_run_at": "",
            }
            continue

        # Parse key-value pairs
        if current and ":" in stripped:
            colon_idx = stripped.index(":")
            key = stripped[:colon_idx].strip().lower().replace(" ", "_")
            val = stripped[colon_idx + 1:].strip()

            if key == "name":
                current["name"] = val
            elif key == "schedule":
                current["schedule"] = val
            elif key == "last_run":
                # "Last run" line: "2026-06-15T12:00:54.446657+09:00  ok"
                # or "2026-06-15T09:00:17.912653+09:00  error: Script exit 1"
                parts = val.split()
                if parts:
                    current["last_run_at"] = parts[0]
                if len(parts) >= 2:
                    full_status = parts[1]
                    if full_status == "ok":
                        current["last_status"] = "ok"
                    elif full_status.startswith("error"):
                        current["last_status"] = "error"
                    else:
                        current["last_status"] = full_status

    # Save last job
    if current:
        _cron_classify(current, active, errors, paused)

    return {
        "active": active,
        "errors": errors,
        "paused": paused,
    }


def _cron_classify(job, active, errors, paused):
    """Sort a parsed cron job into the right list."""
    last_status = job.get("last_status", "")
    state = job.get("state", "active")

    if last_status == "error":
        errors.append(job)
    elif state == "paused":
        paused.append(job)
    else:
        active.append(job)


def collect_network():
    """Collect listening ports for known services."""
    out, _, _ = run(
        r"lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | awk 'NR>1{print $1, $9}' | sort -u"
    )
    known_services = {
        "8642": "Hermes Gateway",
        "8765": "Kanban Dashboard",
        "11434": "Ollama",
        "8787": "Workerd (CF Workers)",
        "9229": "Workerd (Debug)",
        "3307": "SSH Tunnel (Lima DB)",
        "8080": "SSH Tunnel (Lima Web)",
        "5000": "AirPlay",
        "7000": "AirPlay",
    }
    listening = set()
    if out:
        for line in out.split("\n"):
            parts = line.strip().split()
            if len(parts) >= 2:
                port = parts[-1].split(":")[-1]
                listening.add(port)

    result = []
    for port, name in sorted(known_services.items()):
        result.append({
            "service": name,
            "port": port,
            "status": "listening" if port in listening else "down",
        })
    return result


def collect_git_status():
    """Check git status of the drewgent vault."""
    out, _, _ = run("cd " + DREWGENT + " && git status --porcelain 2>/dev/null | wc -l", timeout=5)
    uncommitted = out.strip()
    out, _, _ = run("cd " + DREWGENT + " && git log @{u}..HEAD 2>/dev/null | wc -l", timeout=5)
    unpushed = out.strip()
    return {
        "uncommitted_files": int(uncommitted) if uncommitted and uncommitted.isdigit() else 0,
        "unpushed_commits": int(unpushed) if unpushed and unpushed.isdigit() else 0,
    }


def collect_brew_updates():
    """Count outdated brew packages."""
    out, _, _ = run("brew outdated 2>/dev/null | wc -l", timeout=15)
    count = out.strip()
    return int(count) if count and count.isdigit() else "?"


def collect_docker():
    """List running docker containers summary."""
    out, _, rc = run("docker ps --format '{{.Names}}|{{.Status}}' 2>/dev/null", timeout=10)
    containers = []
    if out:
        for line in out.split("\n"):
            if "|" in line:
                name, status = line.split("|", 1)
                containers.append({"name": name.strip(), "status": status.strip()})
    return containers


def collect_thermal():
    """Check thermal/power state."""
    out, _, _ = run("pmset -g therm 2>/dev/null | head -5", timeout=5)
    thermal = out.strip() if out else "?"
    out2, _, _ = run("pmset -g batt 2>/dev/null | head -3", timeout=5)
    battery = out2.strip() if out2 else "?"
    return {
        "thermal": thermal,
        "battery": battery,
    }


def collect_graph():
    """Scan vault markdown files and extract wikilink graph.
    Returns {nodes: [{id, label, layer}], edges: [{source, target}]}
    Skips P2-hippocampus (too large) and binary/DB files.
    """
    import glob
    import re

    layers_to_scan = [
        ("", "root"),  # top-level .md files
        ("P0-brainstem", "P0"),
        ("P1-limbic", "P1"),
        ("P3-sensors", "P3"),
        ("P4-cortex", "P4"),
        ("P5-ego", "P5"),
        ("P6-prefrontal", "P6"),
        ("skills", "skill"),
    ]

    wiki_re = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]')
    frontmatter_title_re = re.compile(r'^---\s*\n.*?^title:\s*(.+)\s*$.*?\n---', re.MULTILINE | re.DOTALL)

    nodes = {}
    edges_raw = []

    MAX_NODES = 300
    MAX_FILE_SIZE = 100 * 1024  # 100KB

    for subdir, layer_tag in layers_to_scan:
        scan_dir = os.path.join(DREWGENT, subdir) if subdir else DREWGENT
        if not os.path.isdir(scan_dir):
            continue

        # Get .md files (recursive for most, but limit depth)
        if subdir == "skills":
            pattern = "**/SKILL.md"
        else:
            pattern = "**/*.md"
        md_files = glob.glob(os.path.join(scan_dir, pattern), recursive=True)

        # Limit per layer to avoid explosion
        layer_count = 0
        for fpath in md_files:
            rel = os.path.relpath(fpath, DREWGENT)
            # Skip hidden dirs, node_modules, .trash, P2
            if any(p in rel for p in ("/.", "/node_modules/", ".trash", "P2-hippocampus",
                                       "__pycache__", ".git/", "venv/", ".venv/")):
                continue

            if len(nodes) >= MAX_NODES:
                break
            if layer_count >= 60:
                break

            # Skip large files
            try:
                if os.path.getsize(fpath) > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(30000)  # read first 30KB for links
            except Exception:
                continue

            # Title from frontmatter or filename
            title_match = frontmatter_title_re.search(content)
            if title_match:
                label = title_match.group(1).strip()
            else:
                basename = os.path.basename(fpath)
                label = basename.replace(".md", "").replace("-", " ").title()

            node_id = rel.replace(".md", "").replace("/", ":")
            node = {
                "id": node_id,
                "label": label[:40],
                "layer": layer_tag,
                "links": [],
            }

            # Extract wikilinks
            links = wiki_re.findall(content)
            for link in links[:20]:  # max 20 links per file
                node["links"].append(link.strip())

            nodes[node_id] = node
            layer_count += 1

    # Build edges (resolve wikilinks to node ids)
    # Create a lookup: normalized filename -> node id
    lookup = {}
    for nid, nd in nodes.items():
        # Add by full path
        lookup[nd["label"].lower()] = nid
        # Add by filename stem
        stem = nid.split(":")[-1].lower()
        lookup[stem] = nid
        # Add by short path
        short = nid.replace(":", "/").lower()
        lookup[short] = nid

    edges = []
    seen_edges = set()
    for src_id, nd in nodes.items():
        for target_label in nd.get("links", []):
            norm = target_label.lower().strip()
            # Direct lookup
            matched = lookup.get(norm) or lookup.get(norm.replace(" ", "-")) or \
                      lookup.get(norm.replace(" ", "_"))
            if not matched:
                # Fuzzy: find any node whose label or id contains the target
                for nid2, nd2 in nodes.items():
                    if norm in nd2["label"].lower() or norm in nid2.lower():
                        matched = nid2
                        break
            if matched and matched != src_id:
                edge_key = src_id + "->" + matched if src_id < matched else matched + "->" + src_id
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append({"source": src_id, "target": matched})

    # Build minimal node list (only nodes that are in edges, plus their layer info)
    connected = set()
    for e in edges:
        connected.add(e["source"])
        connected.add(e["target"])

    node_list = []
    for nid, nd in nodes.items():
        if nid in connected or len(connected) < 50:
            node_list.append({"id": nid, "label": nd["label"], "layer": nd["layer"]})

    return {
        "nodes": node_list,
        "edges": edges,
        "stats": {
            "total_files_scanned": len(nodes),
            "connected_nodes": len(node_list),
            "edges_count": len(edges),
        },
    }


def collect_recent_errors():
    """Parse last 24h of agent log for ERROR/WARNING lines."""
    log_paths = [
        os.path.join(DREWGENT, "logs", "errors.log"),
        os.path.join(DREWGENT, "logs", "agent.log"),
    ]
    import re

    errors = []
    seen = set()
    error_pat = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
        r"(ERROR|WARNING|CRITICAL).*?"
        r"(?:summary=|error=)([^\n]+)",
        re.DOTALL,
    )

    for log_path in log_paths:
        if not os.path.isfile(log_path):
            continue
        try:
            # Read last 200KB
            with open(log_path, "r", errors="ignore") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 200 * 1024))
                content = f.read()
        except Exception:
            continue

        for match in error_pat.finditer(content):
            ts = match.group(1)
            level = match.group(2)
            msg = match.group(3).strip()[:120]
            dedup = msg[:60]
            if dedup not in seen and len(errors) < 10:
                seen.add(dedup)
                errors.append({"time": ts, "level": level, "message": msg})

    return errors[:8]  # max 8 recent unique errors


def compute_health_status(system, services, cron_data, errors):
    """Aggregate health: returns {level, critical, warning, info, message}."""
    critical = 0
    warning = 0
    info = 0
    issues = []

    try:
        dp = int(system.get("disk_used_pct", 0) or 0)
        if dp > 85:
            critical += 1
            issues.append("disk >85%")
        elif dp > 65:
            warning += 1
            issues.append(f"disk {dp}%")
    except (ValueError, TypeError):
        pass

    if cron_data.get("errors"):
        warning += len(cron_data["errors"])
        issues.append(f"{len(cron_data['errors'])} cron errors")

    if errors:
        critical_errors = [e for e in errors if e["level"] == "CRITICAL"]
        warning_errors = [e for e in errors if e["level"] in ("ERROR", "WARNING")]
        critical += len(critical_errors)
        warning += len(warning_errors)

    if critical > 0:
        level = "critical"
    elif warning > 0:
        level = "warning"
    else:
        level = "healthy"

    return {
        "level": level,
        "critical": critical,
        "warning": warning,
        "issues": issues[:3],
    }


def collect_vault():
    """Check sizes of P-layer directories."""
    layers = [
        ("P0-brainstem", "Rules, neurons"),
        ("P1-limbic", "Persona, voice"),
        ("P2-hippocampus", "Memory, knowledge"),
        ("P3-sensors", "Tools, gateway"),
        ("P4-cortex", "Skills, growth"),
        ("P5-ego", "Self-model, config"),
        ("P6-prefrontal", "Incidents, retro"),
    ]
    result = []
    total_human = "?"
    total_bytes = 0
    for dirname, desc in layers:
        path = os.path.join(DREWGENT, dirname)
        if os.path.isdir(path):
            out, _, _ = run(f"du -sh '{path}' 2>/dev/null | cut -f1")
            size = out.strip() if out else "?"
            result.append({"name": dirname, "size": size, "desc": desc})
            # Parse size bytes
            out2, _, _ = run(f"du -s '{path}' 2>/dev/null | cut -f1")
            if out2:
                try:
                    total_bytes += int(out2.strip()) * 512
                except ValueError:
                    pass

    # Human-readable total
    if total_bytes > 0:
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if total_bytes < 1024:
                total_human = f"{total_bytes:.0f}{unit}"
                break
            total_bytes /= 1024
        else:
            total_human = f"{total_bytes:.1f}TB"

    result.append({"name": "Total", "size": total_human, "desc": ""})
    return result


def collect_sessions():
    """Get recent session info from session_search equivalent."""
    out, _, _ = run("hermes sessions list --limit 3 2>/dev/null")
    sessions = []
    if out:
        for line in out.split("\n"):
            parts = line.strip().split()
            if len(parts) >= 3:
                sessions.append({
                    "id": parts[0] if len(parts) > 0 else "",
                    "source": parts[1] if len(parts) > 1 else "",
                    "message_count": parts[2] if len(parts) > 2 else "",
                    "preview": " ".join(parts[3:]) if len(parts) > 3 else "",
                })
    return sessions


def collect_alerts(system, services, cron_data):
    """Generate alert items based on collected data."""
    alerts = []

    # Disk
    try:
        dp = int(system.get("disk_used_pct", 0) or 0)
        if dp > 80:
            alerts.append({"severity": "error", "message": f"Disk at {dp}% — running low on space"})
        elif dp > 65:
            alerts.append({"severity": "warn", "message": f"Disk at {dp}% — consider cleanup"})
    except (ValueError, TypeError):
        pass

    # Gateway watchdog — check the cron job, not the launchd service
    # launchd service is OnDemand (exits immediately), actual watchdog is
    # the "Drewgent launchd watchdog" cron job running every 5m
    watchdog_cron = [j for j in cron_data.get("active", [])
                     if "launchd watchdog" in j.get("name", "").lower()]
    watchdog_errors = [j for j in cron_data.get("errors", [])
                       if "launchd watchdog" in j.get("name", "").lower()]
    if watchdog_errors:
        alerts.append({
            "severity": "error",
            "message": "Gateway watchdog cron job in error state"
        })
    elif not watchdog_cron and not watchdog_errors:
        alerts.append({
            "severity": "warn",
            "message": "Gateway watchdog cron job not found"
        })

    # Error cron jobs (excluding watchdog itself to avoid double-reporting)
    other_errors = [j for j in cron_data.get("errors", [])
                    if "launchd watchdog" not in j.get("name", "").lower()]
    if other_errors:
        names = ", ".join(j.get("name", "?")[:30] for j in other_errors)
        alerts.append({
            "severity": "warn",
            "message": f"{len(other_errors)} cron job(s) in error state: {names}"
        })

    return alerts


def push(data, endpoint, secret):
    """POST JSON data to the dashboard endpoint."""
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        endpoint + "/api/push",
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        },
        method="POST",
    )
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"}
    except urllib.error.URLError as e:
        return {"error": f"Connection failed: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def main():
    dry_run = "--dry-run" in sys.argv
    for flag in ("--endpoint", "--secret"):
        if flag in sys.argv:
            idx = sys.argv.index(flag)
            if idx + 1 < len(sys.argv):
                val = sys.argv[idx + 1]
                if flag == "--endpoint":
                    global ENDPOINT
                    ENDPOINT = val
                elif flag == "--secret":
                    global PUSH_SECRET
                    PUSH_SECRET = val

    ts = time.strftime("%Y-%m-%d %H:%M:%S KST", time.localtime())
    print(f"[{ts}] Collecting agent status...")

    system = collect_system()
    print(f"  System: OK (load={system['load']}, disk={system['disk_used_pct']}%)")

    launchd = collect_launchd()
    running = sum(1 for s in launchd if s["pid"] > 0)
    print(f"  Launchd: {len(launchd)} services ({running} running)")

    kanban = collect_kanban()
    print(f"  Kanban: {kanban['total']} tasks ({kanban['blocked']} blocked, {kanban['ready']} ready)")

    cron = collect_cron()
    print(f"  Cron: {len(cron['active'])} active, {len(cron['errors'])} errors, {len(cron['paused'])} paused")

    network = collect_network()
    listening = sum(1 for s in network if s["status"] == "listening")
    print(f"  Network: {listening}/{len(network)} services listening")

    vault = collect_vault()
    print(f"  Vault: {vault[-1]['size']} total across {len(vault)-1} P-layers")

    sessions = collect_sessions()
    print(f"  Sessions: {len(sessions)} recent")

    alerts = collect_alerts(system, launchd, cron)
    if alerts:
        print(f"  Alerts: {len(alerts)} ({', '.join(a['message'][:40] for a in alerts)})")

    git = collect_git_status()
    print(f"  Git: {git['uncommitted_files']} uncommitted, {git['unpushed_commits']} unpushed")

    brew = collect_brew_updates()
    print(f"  Brew: {brew} outdated")

    docker = collect_docker()
    print(f"  Docker: {len(docker)} containers")

    thermal = collect_thermal()
    print(f"  Thermal: {thermal['thermal'][:50] if thermal['thermal'] else '?'}")

    print("  Scanning vault wikilink graph...", end=" ", flush=True)
    graph = collect_graph()
    print(f"{graph['stats']['connected_nodes']} nodes, {graph['stats']['edges_count']} edges")

    recent_errors = collect_recent_errors()
    print(f"  Recent errors: {len(recent_errors)} found")

    health = compute_health_status(system, launchd, cron, recent_errors)
    print(f"  Health: {health['level']} ({health['critical']} critical, {health['warning']} warning)")

    payload = {
        "pushed_at": ts,
        "system": system,
        "launchd": launchd,
        "kanban": kanban,
        "cron": cron,
        "network": network,
        "vault": vault,
        "sessions": sessions,
        "alerts": alerts,
        "git": git,
        "brew": brew,
        "docker": docker,
        "thermal": thermal,
        "graph": graph,
        "recent_errors": recent_errors,
        "health": health,
    }

    if dry_run:
        print(f"\n[Dry run] Payload ({len(json.dumps(payload))} bytes):")
        print(json.dumps(payload, indent=2, ensure_ascii=False)[:2000])
        print("... (truncated)")
        return

    print(f"\n  Pushing to {ENDPOINT}/api/push...")
    result = push(payload, ENDPOINT, PUSH_SECRET)

    if result.get("ok"):
        print(f"  Done! pushed_at={result.get('pushed_at', '?')}")
    else:
        print(f"  FAILED: {result.get('error', 'unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()

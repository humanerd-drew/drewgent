#!/usr/bin/env python3
"""Local HTTP server for agent dashboard. Replaces CF Worker + KV.

Supports the same API as the original CF Worker:
  POST /api/push  → stores latest + history (in memory + JSON file)
  GET  /api/status → returns latest JSON payload
  GET  /api/history?date=YYYY-MM-DD → returns history entries
  GET  /*          → serves static files from ~/Sites/agent-dashboard/public/

Runs on port 8766. Starts from launchd (ai.drewgent.agent-dashboard).
"""

import json
import os
import time
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler

HOME = os.path.expanduser("~")
DREWGENT = os.path.join(HOME, ".drewgent")
STATE_FILE = os.path.join(DREWGENT, "agent-dashboard-state.json")
STATIC_DIR = os.path.join(HOME, "Sites", "agent-dashboard", "public")
HOST = "0.0.0.0"
PORT = 8766

def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/status":
            self._json_response(self.server.latest or {"status": "no_data", "message": "No data pushed yet."})
        elif parsed.path == "/api/history":
            date = params.get("date", [time.strftime("%Y-%m-%d")])[0]
            data = self.server.history.get(date, [])
            self._json_response(data)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path != "/api/push":
            self._json_response({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            body = json.loads(raw)
            now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            body["_pushed_at"] = now
            body["_pushed_at_local"] = time.strftime("%Y-%m-%d %H:%M:%S")

            self.server.latest = body

            date_key = now[:10]
            summary = self._extract_summary(body)
            self.server.history.setdefault(date_key, []).append({"time": now, "summary": summary})
            if len(self.server.history[date_key]) > 288:
                self.server.history[date_key] = self.server.history[date_key][-288:]

            save_state(self.server)
            self._json_response({"ok": True, "pushed_at": now})
        except json.JSONDecodeError as e:
            self._json_response({"error": f"invalid JSON: {e}"}, 400)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))

    def _extract_summary(self, body):
        svc = sum(1 for s in (body.get("launchd") or []) if s.get("pid", 0) > 0)
        sys = body.get("system") or {}
        kanban = body.get("kanban") or {}
        cron = body.get("cron") or {}
        return {
            "uptime": sys.get("uptime", "?"),
            "load": sys.get("load", "?"),
            "disk_used_pct": sys.get("disk_used_pct", "?"),
            "services_running": svc,
            "kanban_total": kanban.get("total", 0),
            "cron_active": len(cron.get("active", [])),
            "cron_errors": len(cron.get("errors", [])),
        }

    def log_message(self, format, *args):
        msg = format % args
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")

class DashboardServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.latest = None
        self.history = {}
        super().__init__(*args, **kwargs)

_save_lock = False

def save_state(server):
    global _save_lock
    if _save_lock:
        return
    _save_lock = True
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"latest": server.latest, "history": server.history}, f)
    except OSError as e:
        print(f"[-] Failed to save state: {e}")
    finally:
        _save_lock = False

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            return state.get("latest"), state.get("history", {})
        except (json.JSONDecodeError, OSError):
            pass
    return None, {}

def main():
    latest, history = load_state()
    server = DashboardServer((HOST, PORT), DashboardHandler)
    server.latest = latest
    server.history = history
    print(f"[+] Agent dashboard server starting on http://{HOST}:{PORT}")
    print(f"[+] Static dir: {STATIC_DIR}")
    print(f"[+] State file: {STATE_FILE}")
    print(f"[+] State: {'loaded' if latest else 'clean (no prior data)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[.] Shutting down...")
        save_state(server)
        server.server_close()

if __name__ == "__main__":
    main()

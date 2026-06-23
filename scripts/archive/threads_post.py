#!/usr/bin/env python3
"""Threads API client — post, reply, delete, search, mentions, replies, insights.

Usage:
  # Auth / token management
  ./threads_post.py --auth              # OAuth URL
  ./threads_post.py --exchange CODE     # Exchange code → long-lived token
  ./threads_post.py --refresh           # Refresh token
  ./threads_post.py --me                # My profile info

  # Content
  ./threads_post.py --text "Hello!"                   # Post
  ./threads_post.py --text "Hi" --url https://...     # Post with image
  ./threads_post.py --reply POST_ID --text "Reply"    # Reply
  ./threads_post.py --delete POST_ID                  # Delete

  # Read
  ./threads_post.py --search "keyword"  # Search public threads
  ./threads_post.py --mentions          # My mentions
  ./threads_post.py --replies POST_ID   # Replies to a thread
  ./threads_post.py --profile USERNAME  # Look up public profile

  # Analytics
  ./threads_post.py --insights          # Profile insights
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error

API_VERSION = "v1.0"
BASE = f"https://graph.threads.net/{API_VERSION}"

ENV_KEYS = {
    "app_id": "THREADS_APP_ID",
    "app_secret": "THREADS_APP_SECRET",
    "user_id": "THREADS_USER_ID",
    "access_token": "THREADS_ACCESS_TOKEN",
    "redirect_uri": "THREADS_REDIRECT_URI",
}

USERNAME = "huma_nerd"


def _env(key):
    return os.environ.get(ENV_KEYS[key], "")


def _api(path, data=None, method="GET", base=None):
    url = f"{base or BASE}{path}"
    if data:
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        err = json.loads(body) if body else {}
        msg = err.get("error", {}).get("message", body)
        print(f"API error {e.code}: {msg}", file=sys.stderr)
        print(f"Full response: {body[:200]}", file=sys.stderr)
        sys.exit(1)


def _post_json(url, data):
    """POST with JSON body instead of form-encoded (for better compatibility)."""
    payload = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        err = json.loads(body) if body else {}
        msg = err.get("error", {}).get("message", body)
        print(f"API error {e.code}: {msg}", file=sys.stderr)
        sys.exit(1)


def _ensure_token():
    token = _env("access_token")
    uid = _env("user_id")
    if not token or not uid:
        print("Set THREADS_ACCESS_TOKEN and THREADS_USER_ID in .env", file=sys.stderr)
        sys.exit(1)
    return token, uid


def _publish_url(post_id):
    return f"https://www.threads.net/@{USERNAME}/post/{post_id}"


def _create_and_publish(token, uid, data):
    """Create a container and publish it. Returns post_id."""
    container = _api(f"/{uid}/threads", data={**data, "access_token": token}, method="POST")
    cid = container.get("id")
    if not cid:
        print("Failed to create container", file=sys.stderr)
        sys.exit(1)
    result = _api(f"/{uid}/threads_publish", data={
        "access_token": token, "creation_id": cid,
    }, method="POST")
    return result.get("id", "unknown")


# ── Auth ──

def cmd_auth():
    app_id = _env("app_id")
    redirect = _env("redirect_uri") or "https://www.threads.net"
    if not app_id:
        print("Set THREADS_APP_ID in .env", file=sys.stderr)
        sys.exit(1)
    scope = "threads_basic,threads_content_publish,threads_delete,threads_keyword_search,threads_manage_insights,threads_manage_mentions,threads_manage_replies,threads_profile_discovery,threads_read_replies,threads_location_tagging"
    url = (f"https://www.threads.net/oauth/authorize"
           f"?client_id={app_id}"
           f"&redirect_uri={urllib.parse.quote(redirect)}"
           f"&scope={urllib.parse.quote(scope)}"
           f"&response_type=code")
    print("Open this URL and authorize:\n")
    print(url)
    print()
    print("Then run: python3 scripts/threads_post.py --exchange <CODE>")


def cmd_exchange(code):
    app_id = _env("app_id")
    app_secret = _env("app_secret")
    redirect = _env("redirect_uri") or "https://www.threads.net"
    if not app_id or not app_secret:
        print("Set THREADS_APP_ID and THREADS_APP_SECRET in .env", file=sys.stderr)
        sys.exit(1)

    # 1. Exchange code → short-lived token (POST /oauth/access_token)
    oauth_base = "https://graph.threads.net"
    r = _post_json(f"{oauth_base}/oauth/access_token", {
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
        "code": code,
    })
    short_token = r.get("access_token", "")
    uid = r.get("user_id", "")

    if not short_token:
        print("Failed to get short-lived token from code", file=sys.stderr)
        sys.exit(1)

    # 2. Exchange short-lived → long-lived (GET /access_token)
    query = urllib.parse.urlencode({
        "grant_type": "th_exchange_token",
        "client_secret": app_secret,
        "access_token": short_token,
    })
    r2 = _api(f"/access_token?{query}", base=oauth_base)
    long_token = r2.get("access_token", "")
    print(f"THREADS_USER_ID={uid}")
    print(f"THREADS_ACCESS_TOKEN={long_token}")


def cmd_refresh():
    token, _ = _ensure_token()
    oauth_base = "https://graph.threads.net"
    r = _api(f"/refresh_access_token?access_token={token}&grant_type=th_refresh_token", method="GET", base=oauth_base)
    new_token = r.get("access_token", "")
    expires = r.get("expires_in", 0)
    print(f"THREADS_ACCESS_TOKEN={new_token}")
    print(f"Expires in {expires // 86400} days")
    inp = input("Update .env? (y/N): ")
    if inp.lower() == "y":
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        with open(env_path) as f:
            content = f.read()
        if "THREADS_ACCESS_TOKEN=" in content:
            lines = content.split("\n")
            for i, l in enumerate(lines):
                if l.startswith("THREADS_ACCESS_TOKEN="):
                    lines[i] = f"THREADS_ACCESS_TOKEN={new_token}"
                    break
            with open(env_path, "w") as f:
                f.write("\n".join(lines))
            print("Updated .env")


# ── Profile (threads_basic, threads_profile_discovery) ──

def cmd_me():
    token, _ = _ensure_token()
    r = _api(f"/me?fields=id,name,username&access_token={token}")
    print(json.dumps(r, indent=2))


def cmd_profile(username):
    token, _ = _ensure_token()
    r = _api(f"/me?fields=id,name,username,threads_profile_url&access_token={token}")
    uid = r.get("id", "")
    print(f"Logged in as: {r.get('username')} ({r.get('name')})")
    print(f"Profile: {r.get('threads_profile_url')}")


# ── Post (threads_content_publish) ──

def cmd_post(text, media_url=None):
    token, uid = _ensure_token()
    data = {"media_type": "TEXT", "text": text}
    if media_url:
        data["media_type"] = "IMAGE"
        data["image_url"] = media_url
    post_id = _create_and_publish(token, uid, data)
    print(f"Posted: {_publish_url(post_id)}")


# ── Reply (threads_manage_replies) ──

def cmd_reply(post_id, text):
    token, uid = _ensure_token()
    data = {"media_type": "TEXT", "text": text, "reply_to_id": post_id}
    pid = _create_and_publish(token, uid, data)
    print(f"Replied: {_publish_url(pid)}")


# ── Delete (threads_delete) ──

def cmd_delete(post_id):
    token, uid = _ensure_token()
    result = _api(f"/{post_id}?access_token={token}", method="DELETE")
    print(f"Deleted: {post_id}")
    return result


# ── Search (threads_keyword_search) ──

def cmd_search(query):
    token, _ = _ensure_token()
    r = _api(f"/me?fields=id&access_token={token}")
    uid = r.get("id", "")
    results = _api(f"/{uid}/threads_search?q={urllib.parse.quote(query)}&access_token={token}")
    posts = results.get("data", results)
    print(f"Search results for '{query}':")
    for p in (posts if isinstance(posts, list) else [posts]):
        print(f"  {p.get('id', '?')}: {p.get('text', '(no text)')[:80]}")


# ── Mentions (threads_manage_mentions) ──

def cmd_mentions():
    token, uid = _ensure_token()
    r = _api(f"/{uid}/mentions?fields=id,text,media_type,timestamp&access_token={token}")
    data = r.get("data", [r])
    print(f"Mentions ({len(data)}):")
    for m in data:
        txt = m.get("text", "(no text)")[:100]
        print(f"  {m.get('id', '?')}: {txt}")
        print(f"    https://www.threads.net/@{USERNAME}/post/{m.get('id', '')}")


# ── Read replies (threads_read_replies) ──

def cmd_replies(thread_id):
    token, uid = _ensure_token()
    r = _api(f"/{thread_id}/replies?fields=id,text,media_type,timestamp&access_token={token}")
    data = r.get("data", [r])
    print(f"Replies to {thread_id} ({len(data)}):")
    for rep in data:
        txt = rep.get("text", "(no text)")[:100]
        print(f"  {rep.get('id', '?')}: {txt}")


# ── Insights (threads_manage_insights) ──

def cmd_insights():
    token, uid = _ensure_token()
    metrics = "views,likes,replies,reposts,quotes,followers_count"
    r = _api(f"/{uid}/threads_insights?metric={urllib.parse.quote(metrics)}&period=day&access_token={token}")
    data = r.get("data", [r])
    print("Insights:")
    for m in data if isinstance(data, list) else [data]:
        name = m.get("name", "?")
        vals = m.get("values", [])
        if vals:
            print(f"  {name}: {vals[-1].get('value', 'N/A')}")
        else:
            print(f"  {name}: N/A")


# ── Test token help ──

def cmd_test_token():
    print("Meta Developer Center → Threads → API access → User Token Generator")
    print("Select user, scopes: threads_basic, threads_content_publish")
    print("Token → .env as THREADS_ACCESS_TOKEN")


def cmd_scopes():
    print("Submit ALL these permissions in Meta App Review:")
    print()
    print("  Permission                    Used by")
    print("  " + "-" * 60)
    print("  threads_basic                 --me")
    print("  threads_content_publish       --text, --reply")
    print("  threads_delete                --delete")
    print("  threads_keyword_search        --search")
    print("  threads_manage_insights       --insights")
    print("  threads_manage_mentions       --mentions")
    print("  threads_manage_replies        --reply, hide/reply management")
    print("  threads_profile_discovery     --profile")
    print("  threads_read_replies          --replies")
    print("  threads_location_tagging      (location tagging)")
    print()
    print("Submit one review with all 10 permissions and 1 demo video.")


# ── CLI ──

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Threads API client")
    p.add_argument("--auth", action="store_true")
    p.add_argument("--exchange", metavar="CODE")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--test-token", action="store_true")
    p.add_argument("--me", action="store_true")
    p.add_argument("--text")
    p.add_argument("--url")
    p.add_argument("--reply", metavar="POST_ID")
    p.add_argument("--delete", metavar="POST_ID")
    p.add_argument("--search", metavar="KEYWORD")
    p.add_argument("--mentions", action="store_true")
    p.add_argument("--replies", metavar="POST_ID")
    p.add_argument("--insights", action="store_true")
    p.add_argument("--profile", metavar="USERNAME")
    p.add_argument("--scopes", action="store_true", help="Show required permissions for App Review")
    args = p.parse_args()

    if args.auth: cmd_auth()
    elif args.exchange: cmd_exchange(args.exchange)
    elif args.refresh: cmd_refresh()
    elif args.test_token: cmd_test_token()
    elif args.me: cmd_me()
    elif args.text:
        if args.reply:
            cmd_reply(args.reply, args.text)
        else:
            cmd_post(args.text, media_url=args.url)
    elif args.delete: cmd_delete(args.delete)
    elif args.search: cmd_search(args.search)
    elif args.mentions: cmd_mentions()
    elif args.replies: cmd_replies(args.replies)
    elif args.insights: cmd_insights()
    elif args.scopes: cmd_scopes()
    elif args.profile: cmd_profile(args.profile)
    else:
        stdin = sys.stdin.read().strip()
        if stdin:
            cmd_post(stdin)
        else:
            p.print_help()

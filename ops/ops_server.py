# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.10,<2", "httpx", "uvicorn"]
# ///
"""Ops MCP server: the only door agents use to touch GitHub, the VPS and Thor.

Fixed tools, no raw shell. Merging needs a chairman approval that agents cannot
write: approvals arrive on POST /approve with CHAIRMAN_TOKEN, a different
secret from OPS_AGENT_TOKEN (the MCP bearer). An approval is bound to the PR's
head SHA, so a push after approval voids it.

Env: GITHUB_TOKEN, OPS_AGENT_TOKEN, CHAIRMAN_TOKEN, OPS_ALLOWED_REPOS
(comma list of owner/name), OPS_HOSTS ("vps=root@1.2.3.4,thor=vikash191@100.x"),
OPS_STATE (approvals file, default ./approvals.json), PORT (default 8790).
"""
import os, re, time

import anyio
import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO line prints the Telegram bot token in the URL

import trader_job

GH = "https://api.github.com"
REPOS = {r.strip() for r in os.environ.get("OPS_ALLOWED_REPOS", "").split(",") if r.strip()}
HEALTH = dict(h.split("=", 1) for h in os.environ.get("OPS_HEALTH_URLS", "").split(",") if "=" in h)

# stateless + json_response: one plain JSON-RPC POST per call, no session handshake, no SSE
mcp = FastMCP("ops", host="0.0.0.0", port=int(os.environ.get("PORT", "8790")),
              stateless_http=True, json_response=True)


def _gh(method: str, path: str, **kw):
    r = httpx.request(method, GH + path, timeout=20, headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json"}, **kw)
    r.raise_for_status()
    return r.json()


def _repo(repo: str) -> str:
    if repo not in REPOS:
        raise ValueError(f"{repo} is not in OPS_ALLOWED_REPOS")
    return repo


# PR approvals and trade tickets share OPS_STATE; every read-modify-write goes through
# trader_job._state (file lock + atomic replace), so /tg, cron and the MCP tools cannot race.
_state = trader_job._state


@mcp.tool()
def list_prs(repo: str) -> list[dict]:
    """Lists open pull requests for an allowed repo (owner/name)."""
    return [{"number": p["number"], "title": p["title"], "head": p["head"]["ref"],
             "sha": p["head"]["sha"], "url": p["html_url"]}
            for p in _gh("GET", f"/repos/{_repo(repo)}/pulls?state=open")]


@mcp.tool()
def open_pr(repo: str, head: str, title: str, body: str = "", base: str = "main") -> dict:
    """Opens a pull request from an already-pushed branch. Never merges."""
    p = _gh("POST", f"/repos/{_repo(repo)}/pulls",
            json={"head": head, "base": base, "title": title, "body": body})
    return {"number": p["number"], "url": p["html_url"], "sha": p["head"]["sha"]}


@mcp.tool()
def request_merge(repo: str, pr: int, summary: str) -> dict:
    """Asks the chairman to approve merging this PR. Returns the pending request;
    the chairman is told out of band. Call merge_pr only after approval."""
    sha = _gh("GET", f"/repos/{_repo(repo)}/pulls/{pr}")["head"]["sha"]
    key = f"{repo}#{pr}"
    with _state() as d:
        rec = d[key] = {"sha": sha, "summary": summary, "status": "pending", "at": time.time(),
                        **trader_job.new_nonces()}
    kb = trader_job.buttons(key, rec)  # None if callback_data would pass 64 bytes: not sent
    notified = bool(kb) and trader_job.tg_send(f"Merge request {key}\n{summary}\nsha {sha[:8]}", kb)
    return {"status": "pending", "sha": sha, "notified": notified,
            "note": "waiting for chairman approval"}


@mcp.tool()
def merge_pr(repo: str, pr: int) -> dict:
    """Squash-merges a PR only if the chairman approved its current head SHA."""
    with _state() as d:
        rec = d.get(f"{_repo(repo)}#{pr}")
    if not rec or rec["status"] != "approved":
        return {"merged": False, "reason": "no chairman approval"}
    sha = _gh("GET", f"/repos/{repo}/pulls/{pr}")["head"]["sha"]
    if rec["sha"] != sha:
        return {"merged": False, "reason": "PR changed after approval; request again"}
    r = _gh("PUT", f"/repos/{repo}/pulls/{pr}/merge", json={"merge_method": "squash", "sha": sha})
    return {"merged": r.get("merged", False), "message": r.get("message")}


@mcp.tool()
def service_health(name: str = "") -> dict:
    """Probes named HTTP health endpoints (Thor API, Agent Zero, the VPS apps) from
    OPS_HEALTH_URLS. Empty name probes all. Reports status code and latency."""
    out = {}
    for n, url in HEALTH.items():
        if name and n != name:
            continue
        t = time.time()
        try:
            out[n] = {"status": httpx.get(url, timeout=8).status_code, "ms": int((time.time() - t) * 1000)}
        except Exception as e:  # unreachable is a result, not a crash
            out[n] = {"status": None, "error": type(e).__name__}
    return out or {"error": f"unknown {name!r}; known: {sorted(HEALTH)}"}


@mcp.tool()
def coolify_status() -> list[dict]:
    """Lists every Coolify application on the VPS with its running/health status."""
    r = httpx.get(os.environ["COOLIFY_URL"].rstrip("/") + "/api/v1/applications", timeout=20,
                  headers={"Authorization": f"Bearer {os.environ['COOLIFY_API_TOKEN']}"})
    r.raise_for_status()
    return [{"name": a.get("name"), "status": a.get("status"), "fqdn": a.get("fqdn")} for a in r.json()]


@mcp.tool()
def check_url(url: str, expect: str = "") -> dict:
    """Fetches a public https URL and reports status, title, and whether `expect`
    appears in the HTML. Static HTML only: client-rendered features need a browser."""
    if not re.match(r"https://", url):
        return {"ok": False, "reason": "https only"}
    r = httpx.get(url, timeout=20, follow_redirects=True)
    t = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
    return {"status": r.status_code, "title": t.group(1).strip() if t else None,
            "found": (expect in r.text) if expect else None}


@mcp.tool()
def trading_status() -> dict:
    """Paper account equity and cash, open positions, open orders and today's trade
    tickets with their status. Read only. An order is placed only when the chairman
    taps Approve on the Telegram ticket; no tool can place, approve or cancel one."""
    return trader_job.status()


@mcp.tool()
async def trading_digest() -> str:
    """Runs the morning digest now (scan, headlines, Pelosi PTRs, account), sends it to
    Telegram and returns the text. Places nothing: orders need the chairman's Telegram tap."""
    return await anyio.to_thread.run_sync(trader_job.morning)


@mcp.tool()
async def trading_propose() -> str:
    """Runs one trade proposal now (ORB scan, entry, exit plan, sizing, risk check) and
    returns what happened. A ticket that passes goes to Telegram; it is placed on the
    paper account only if the chairman taps Approve within 10 minutes."""
    return await anyio.to_thread.run_sync(lambda: trader_job.propose(notify_none=False))


@mcp.custom_route("/approve", methods=["POST"])
async def approve(request: Request):
    """Chairman only: {"repo","pr","decision":"approve"|"reject"} with CHAIRMAN_TOKEN."""
    if request.headers.get("authorization") != f"Bearer {os.environ['CHAIRMAN_TOKEN']}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = await request.json()
    with _state() as d:
        rec = d.get(f"{b['repo']}#{b['pr']}")
        if rec:
            rec["status"] = "approved" if b.get("decision") == "approve" else "rejected"
    if not rec:
        return JSONResponse({"error": "no pending request"}, status_code=404)
    return JSONResponse({"ok": True, "status": rec["status"], "sha": rec["sha"]})


@mcp.custom_route("/tg", methods=["POST"])
async def telegram_webhook(request: Request):
    """Telegram button presses, forwarded unchanged by the ProjectNexus CEO (which owns the
    bot's long poll; no webhook is registered). Accepted only with the secret header
    (TG_WEBHOOK_SECRET), only from TELEGRAM_CHAT_ID, and only when callback_data
    "a|<key>|<nonce>" carries the nonce stored for that verb. Anything else: 200, no change."""
    if request.headers.get("x-telegram-bot-api-secret-token") != os.environ["TG_WEBHOOK_SECRET"]:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ok = JSONResponse({"ok": True})
    cb = (await request.json()).get("callback_query")
    if not cb or str(cb["message"]["chat"]["id"]) != os.environ["TELEGRAM_CHAT_ID"]:
        return ok
    parts = (cb.get("data") or "").split("|")
    if len(parts) != 3 or parts[0] not in ("a", "r"):
        return ok
    verb, key, nonce = parts
    with _state() as d:
        rec = d.get(key)
        valid = trader_job.nonce_ok(rec, verb, nonce)
        if valid and not key.startswith("T:") and rec.get("status") == "pending":
            rec["status"] = "approved" if verb == "a" else "rejected"
    if not (valid and key.startswith("T:")):
        return ok
    # trade ticket: Approve is the only path to an order
    fn = trader_job.place_ticket if verb == "a" else trader_job.reject_ticket
    try:
        out = await anyio.to_thread.run_sync(fn, key)
    except Exception as e:  # the chairman must learn the tap's outcome is unknown
        out = f"Error on {key}: {type(e).__name__}. Check trading_status before acting."
    await anyio.to_thread.run_sync(trader_job.tg_send, out)
    return ok


def app():
    """ASGI app: MCP at /mcp behind the agent bearer; /approve has its own check."""
    a = mcp.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in ("/approve", "/tg"):
            h = dict(scope["headers"]).get(b"authorization", b"").decode()
            if h != f"Bearer {os.environ['OPS_AGENT_TOKEN']}":
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await a(scope, receive, send)
    return guarded


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8790")))

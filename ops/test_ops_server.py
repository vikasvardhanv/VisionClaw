import json, os
os.environ.update(OPS_AGENT_TOKEN="t", CHAIRMAN_TOKEN="c", TG_WEBHOOK_SECRET="s", TELEGRAM_CHAT_ID="1")

from starlette.testclient import TestClient
import ops_server

H = {"Authorization": "Bearer t", "Accept": "application/json, text/event-stream"}


def rpc(c, method, params=None, headers=H):
    return c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_mcp_plain_json_rpc():
    with TestClient(ops_server.app()) as c:
        assert rpc(c, "tools/list", headers={"Accept": H["Accept"]}).status_code == 401
        r = rpc(c, "tools/call", {"name": "check_url", "arguments": {"url": "http://x"}})
        assert r.status_code == 200 and "application/json" in r.headers["content-type"]
        assert "https only" in r.json()["result"]["content"][0]["text"]
        names = [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]]
        assert "merge_pr" in names

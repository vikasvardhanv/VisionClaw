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
        assert {"trading_status", "trading_digest", "trading_propose", "ask_ceo", "ceo_missions"} <= set(names)


def test_ask_ceo(monkeypatch):
    import asyncio, httpx
    monkeypatch.delenv("CEO_INBOX_URL", raising=False)
    assert "not configured" in asyncio.run(ops_server.ask_ceo("hi"))
    seen = {}

    def handler(req):
        seen["auth"], seen["body"] = req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"reply": "on it"})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("CEO_INBOX_URL", "http://ceo/chairman")
    monkeypatch.setenv("CEO_INBOX_TOKEN", "k")
    assert asyncio.run(ops_server.ask_ceo("ship it")) == "on it"
    assert seen == {"auth": "Bearer k", "body": {"text": "ship it"}}


def test_ceo_missions(monkeypatch):
    import asyncio, httpx
    monkeypatch.delenv("CEO_INBOX_URL", raising=False)
    assert "not configured" in asyncio.run(ops_server.ceo_missions())
    seen = {}
    body = {"missions": [{"id": "m1", "type": "research", "status": "running", "description": "scan", "updated": "t1"}]}

    def handler(req):
        seen["path"], seen["limit"], seen["auth"] = req.url.path, req.url.params["limit"], req.headers["authorization"]
        return httpx.Response(200, json=body)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("CEO_INBOX_URL", "http://ceo/chairman")
    monkeypatch.setenv("CEO_INBOX_TOKEN", "k")
    assert asyncio.run(ops_server.ceo_missions(3)) == "running | research | scan (m1, t1)"
    assert seen == {"path": "/chairman/missions", "limit": "3", "auth": "Bearer k"}
    body["missions"] = []
    assert asyncio.run(ops_server.ceo_missions()) == "The CEO has no missions on record."

# Ops MCP server

Single door for agents to reach GitHub, the VPS and Thor. Fixed tools, no raw shell.

| Tool | Does |
|---|---|
| `list_prs`, `open_pr` | Read and open PRs on allowed repos. Never merges. |
| `request_merge` | Records a pending approval for the PR's current head SHA. |
| `merge_pr` | Squash-merges only if the chairman approved that exact SHA. |
| `service_health` | Probes the HTTP health endpoints named in `OPS_HEALTH_URLS`. |
| `coolify_status` | Lists Coolify applications on the VPS with their status. |
| `check_url` | HTTPS fetch: status, title, expected text. Static HTML only. |

## Approval flow

1. Agent pushes a branch and calls `open_pr`, then `request_merge`.
2. Chairman approves out of band: `POST /approve` with `Authorization: Bearer $CHAIRMAN_TOKEN` and `{"repo","pr","decision":"approve"}`.
3. Agent calls `merge_pr`. A push after approval changes the SHA and voids it.

`OPS_AGENT_TOKEN` (the MCP bearer) cannot call `/approve`. Keep the two secrets separate.

## Run

```bash
envcrypt run .sops.env -- uv run ops/ops_server.py
```

Env: `GITHUB_TOKEN` (fine-grained, only the repos in `OPS_ALLOWED_REPOS`), `OPS_AGENT_TOKEN`, `CHAIRMAN_TOKEN`, `OPS_ALLOWED_REPOS`, `OPS_HOSTS` (`vps=root@host,thor=user@100.x`), `OPS_STATE`, `PORT`.

## Transport

Stateless JSON-RPC: one `POST /mcp` with `Authorization: Bearer $OPS_AGENT_TOKEN` and `Accept: application/json, text/event-stream` returns a JSON reply. No session handshake, no SSE.

## Status

- Deployed at `https://ops.76-13-124-154.sslip.io`. The Telegram webhook (`/tg`) is registered. Env also needs `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TG_WEBHOOK_SECRET`.
- Not registered in `gateway/src/apps.ts` or in Viktron's agent tools.
- `check_url` cannot judge client-rendered features; that needs a browser step.
- Verified locally: auth gates, the no-approval merge refusal, `/approve`, and the Telegram webhook (foreign chat ignored, no flip after approval). Not verified: real GitHub calls (the token in `.sops.env` is dead), SSH from the server.

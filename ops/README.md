# Ops MCP server

Single door for agents to reach GitHub, the VPS and Thor. Fixed tools, no raw shell.

| Tool | Does |
|---|---|
| `list_prs`, `open_pr` | Read and open PRs on allowed repos. Never merges. |
| `request_merge` | Records a pending approval for the PR's current head SHA. |
| `merge_pr` | Squash-merges only if the chairman approved that exact SHA. |
| `host_health` | Fixed read-only check (uptime, disk, containers) on a named host. |
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

## Not done yet

- Not deployed. It needs a public HTTPS URL for Anthropic-hosted agents; the tailnet is unreachable from there.
- Not registered in `gateway/src/apps.ts` or in Viktron's agent tools.
- Telegram approval works locally (`request_merge` sends Approve/Reject buttons; `/tg` webhook applies them, chairman chat only). After deploy, register the webhook: `setWebhook` with `url=<public>/tg` and `secret_token=$TG_WEBHOOK_SECRET`. Env also needs `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TG_WEBHOOK_SECRET`.
- `check_url` cannot judge client-rendered features; that needs a browser step.
- Verified locally: auth gates, the no-approval merge refusal, `/approve`, and the Telegram webhook (foreign chat ignored, no flip after approval). Not verified: real GitHub calls (the token in `.sops.env` is dead), SSH from the server, MCP tool calls from an agent.

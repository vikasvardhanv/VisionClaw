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
| `trading_status` | Paper account equity and cash, positions, open orders, today's trade tickets. Read only. |
| `trading_digest` | Runs the morning digest now, sends it to Telegram, returns the text. |
| `trading_propose` | Runs one trade proposal now and returns what happened. Places nothing. |

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

## Trading

A $50 day-trading loop on an Alpaca **paper** account. `trader_job.py` runs the day-trader-skill scripts (cloned at a pinned commit into `/opt/dts`, never copied into this repo) and sends the results to Telegram. The scripts never place orders.

### Schedule

`ops/crontab` is the source of truth. The controller installs it on the VPS host. The jobs run Monday to Friday at these America/New_York times:

| Time (ET) | Cron (UTC, EDT and EST) | Job | Does |
|---|---|---|---|
| 08:45 | 12:45, 13:45 | `morning` | Scans the candidate list, reads new headlines and Pelosi PTRs, reads the account. Sends one plain message. |
| 10:00 | 14:00, 15:00 | `propose` | Opening-range breakout scan, then entry, exit plan, sizing and risk check. A passed ticket goes to Telegram with Approve/Reject buttons. |
| 10:30 | 14:30, 15:30 | `propose` | The same. If nothing passes, it sends one line: "No trade today: <reason>". |
| 15:55 | 19:55, 20:55 | `flatten` | Cancels all open orders. Closes all positions. Sends the day's P&L, what closed, and what did NOT close. |

Ubuntu's cron ignores `CRON_TZ`, so the crontab lists each job at both UTC times. `trader_job.py` reads the New York clock. If the time is more than 10 minutes from the job's slot, it prints one line and exits 0. So each job runs once a day, in summer and in winter.

Then each job asks Alpaca's calendar for a session. On a market holiday it prints one line and exits 0. On an early-close day (close is not 16:00), `propose` sends no ticket: the bracket's day legs would end before the 15:55 flatten. Output goes to `/var/log/ops-trader.log`.

`propose` sizes a **limit** order at the plan's entry, so a slow tap cannot fill higher and raise the loss at the stop. It skips a breakout whose trigger bar closed more than 10 minutes ago.

If a close fails, or a position is still open after the flatten, the Telegram line says `NOT closed` or `ALERT`. Close that position by hand on the paper dashboard.

### Three gates before an order

1. **Risk PASS.** `check.py` must pass the exact order. If not, no ticket is sent.
2. **Chairman tap.** Only the Telegram webhook (`/tg`) places an order. The press must pass the secret header and come from `TELEGRAM_CHAT_ID`. No MCP tool places, approves or cancels an order.
3. **Paper URL.** `place_ticket` refuses unless the `ALPACA_BASE_URL` host is exactly `paper-api.alpaca.markets`. There is no live switch in code.

A ticket expires **10 minutes** after it is sent. An Approve after that is refused and reported in the chat. A second tap on the same ticket is refused.

### Why the auto-flatten at 15:55

The plan is a day trade: no overnight hold. The bracket's stop-loss and take-profit legs are day orders and end at the close. After the close, a position has no stop. The 15:55 job closes everything first. Exits only reduce risk, so this job needs no tap.

### Env

`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL` (`https://paper-api.alpaca.markets`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TG_WEBHOOK_SECRET`, `OPS_STATE` (tickets are stored next to the PR approvals, key `T:<YYYYMMDD>-<n>`), `TRADER_DIR` (default `/data/trader-home`), `TRADER_SYMBOLS` (comma list; the default is 24 names that usually trade under $45), `SEC_USER_AGENT` (optional; without it the digest skips SEC filings). The scripts run without any `*KEY*`, `*SECRET*` or `*TOKEN*` variable in their environment.

### Change the budget

The limits live in `$TRADER_DIR/trader/config.json` on the `/data` volume. The job copies `ops/trader_config.json` there only when the file is absent.

1. Edit `ops/trader_config.json` so the repo keeps the source of truth.
2. Edit `$TRADER_DIR/trader/config.json` in the container with the same values, or delete it so the next job copies the new file.
3. Keep `max_order_usd` at or under the budget. Keep `paper` true.

The digest's budget line reads from this file, so the next morning message shows the new limits.

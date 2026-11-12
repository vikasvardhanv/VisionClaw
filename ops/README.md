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
2. `request_merge` sends the chairman a Telegram message with Approve/Reject buttons.
3. Chairman taps Approve. As a fallback, the chairman can `POST /approve` with `Authorization: Bearer $CHAIRMAN_TOKEN` and `{"repo","pr","decision":"approve"}`.
4. Agent calls `merge_pr`. A push after approval changes the SHA and voids it.

`OPS_AGENT_TOKEN` (the MCP bearer) cannot call `/approve`. Keep the two secrets separate.

## Run

```bash
envcrypt run .sops.env -- uv run ops/ops_server.py
```

Env: `GITHUB_TOKEN` (fine-grained, only the repos in `OPS_ALLOWED_REPOS`), `OPS_AGENT_TOKEN`, `CHAIRMAN_TOKEN`, `OPS_ALLOWED_REPOS`, `OPS_HOSTS` (`vps=root@host,thor=user@100.x`), `OPS_STATE`, `PORT`.

## Transport

Stateless JSON-RPC: one `POST /mcp` with `Authorization: Bearer $OPS_AGENT_TOKEN` and `Accept: application/json, text/event-stream` returns a JSON reply. No session handshake, no SSE.

## Status

- Deployed at `https://ops.76-13-124-154.sslip.io`. No Telegram webhook is registered. Button presses arrive on `/tg` through the ProjectNexus CEO forwarder (see Deploy).
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
2. **Chairman tap.** Only the `/tg` handler places an order. The press must pass the secret header, come from `TELEGRAM_CHAT_ID`, and carry the ticket's Approve nonce. No MCP tool places, approves or cancels an order.
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

## Deploy

### How button presses reach the ops server

The ops server and the ProjectNexus CEO (`coolify_deploy/telegram_worker.py`) share one Telegram bot. The CEO long-polls that bot, and Telegram refuses a long poll while a webhook is set. So the ops server holds no webhook. The CEO deletes any webhook at boot. When a press has `callback_data` that starts with `a|` or `r|`, the CEO sends the raw update JSON to `/tg` with the header `X-Telegram-Bot-Api-Secret-Token`.

While the CEO restarts, nobody polls the bot, and taps made in that time are lost. A lost tap on a trade ticket does no harm: the ticket expires after 10 minutes. Tap again on a merge request, or call `request_merge` again.

### Tap nonces

Each Approve/Reject keyboard carries a fresh random nonce per verb (`a|<key>|<nonce_a>`, `r|<key>|<nonce_r>`), stored only in `OPS_STATE`. `/tg` acts only when the nonce matches, so the webhook secret alone (which the CEO's LLM can reach) cannot forge a tap, and no MCP tool returns a nonce.

`callback_data` must be 64 bytes or less. If a key is too long, the server does not send the buttons (`notified: false`). A record made before nonces existed cannot be approved by a tap. Call `request_merge` again.

### Env that must match ProjectNexus

| Ops server | ProjectNexus CEO | Rule |
|---|---|---|
| `TG_WEBHOOK_SECRET` | `OPS_TG_SECRET` | Must be set and identical. |
| `OPS_AGENT_TOKEN` | `OPS_AGENT_TOKEN` | Identical. |
| (public URL) | `OPS_MCP_URL` | `https://ops.76-13-124-154.sslip.io/mcp` |
| `TELEGRAM_BOT_TOKEN` | `TELEGRAM_BOT_TOKEN` | The shared bot. |
| `TELEGRAM_CHAT_ID` | `TELEGRAM_CHAT_ID` | The chairman's chat. The CEO ignores every other chat. |

Trading env on the ops server: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL` (`https://paper-api.alpaca.markets`), and optionally `TRADER_SYMBOLS` (see Trading > Env).

### Container

- The compose service keeps `container_name: ops-mcp`. The crontab calls `docker exec ops-mcp`.
- The service keeps the `/data` volume. `OPS_STATE` (`/data/approvals.json`) and `TRADER_DIR` (`/data/trader-home`) live on it.

### Host and crontab

The host must run in UTC. The crontab times are UTC.

1. Examine the host clock: `timedatectl`. The time zone must be `Etc/UTC`.
2. Copy `ops/crontab` to the host.
3. Install the file: `crontab ops/crontab` as a user that can run `docker exec`. This replaces that user's crontab, so merge any existing lines first.
4. Make sure that the entries are present: `crontab -l`.
5. Make sure that `/var/log/ops-trader.log` is writable by that user.

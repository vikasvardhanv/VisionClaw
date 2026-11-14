# EDITH on the VPS: plan

Goal: the chairman talks to EDITH (VisionClaw chairman mode) and EDITH gets work done through our own
stack: CEO (ProjectNexus `telegram_worker` on the VPS) -> Hermes missions -> Agent Zero on Thor, plus the
ops server (GitHub, Coolify, service health, paper trading). Everything runs on the VPS. No Fly, no
LiveKit Cloud.

Already on main: chairman mode + EDITH persona (VisionClaw df10e14), CEO inbox `POST /chairman`
(ProjectNexus e6c46185), ops tool `ask_ceo`.

## Global constraints

- Never print a secret value. Secrets live in sops files; pass them to processes with
  `envcrypt run <file> -- <cmd>`. Key names are fine to print.
- Never create a plaintext `.env` on this Mac. Env files exist only on the VPS, mode 600.
- Minimal diffs, no new dependencies, match the surrounding style. Commit to `main` locally;
  the controller pushes after review.
- VPS: `root@76.13.124.154`, password `VPS_ROOT_SSH_PASSWORD` in
  `~/IdeaProjects/ProjectNexusAI/ProjectNexus-github/.sops.env`. Tailnet IP 100.120.229.126.
  Coolify's Traefik is on docker network `coolify`, cert resolver `letsencrypt`, entrypoint `https`.

## Task 1: CEO mission feed (ProjectNexus)

Repo `~/IdeaProjects/ProjectNexusAI/ProjectNexus-ops` (branch main, `git pull --ff-only` first).
In `coolify_deploy/telegram_worker.py`, next to `_chairman_inbox`, add `GET /chairman/missions`
on the same aiohttp app, same bearer (`CEO_INBOX_TOKEN`, 401 otherwise). It returns
`{"missions": [...]}`: the newest N (query `limit`, default 5, max 20) Hermes missions, each with
id, type, status, a short description (<= 200 chars), and last-update time. Read missions the way
`execution/hermes_supervisor.py` stores them (find its storage dir and format; do not invent one).
Add one small runnable test for the listing helper (pure function over a temp dir). Commit.

## Task 2: ops tool `ceo_missions` + EDITH knows it (VisionClaw)

Repo `~/IdeaProjects/VisionClaw` (main). In `ops/ops_server.py` add `@mcp.tool() ceo_missions(limit: int = 5)`
that GETs `<CEO_INBOX_URL>/missions?limit=N` (CEO_INBOX_URL ends in `/chairman`) with the same bearer as
`ask_ceo`, and returns a short readable list, with the same not-configured / unreachable / non-200
messages style as `ask_ceo`. Test it in `ops/test_ops_server.py` like `test_ask_ceo` (MockTransport) and add it
to the tools/list assertion. Mention it in `ops/README.md`'s tool table. In `gateway/src/provision.ts`
`CHAIRMAN_SYSTEM_PROMPT`, add one bullet: use ceo_missions when asked what the CEO or the company is working on
or how a task is going. Run `uv run -q --no-project --with pytest --with 'mcp>=1.10,<2' --with httpx --with anyio
--with starlette --with uvicorn python -m pytest -q` in `ops/` and `npx tsc --noEmit -p .` in `gateway/`. Commit.

## Task 3: self-hosted VPS stack (VisionClaw)

Add `deploy/vps/` to VisionClaw with:
- `docker-compose.yml`: services `livekit` (image `livekit/livekit-server`, pinned version, config
  `livekit.yaml`, ports `7881:7881/tcp` and `50000-50100:50000-50100/udp` published, signaling 7880 behind
  Traefik at `livekit.76-13-124-154.sslip.io`), `gateway` (build `../../gateway`, container `vc-gateway`,
  env_file `gateway.env`, volume `./data:/data`, Traefik at `visionclaw.76-13-124-154.sslip.io`, port 8788),
  `agent` (build `../../agent`, container `vc-agent`, env_file `agent.env`). All on external network `coolify`.
  Unique Traefik router/service names (prefix `visionclaw`).
- `livekit.yaml`: port 7880, rtc tcp_port 7881, UDP 50000-50100, node_ip 76.13.124.154; keys come from
  the `LIVEKIT_KEYS` env var, not the file.
- `deploy.sh` (run from the Mac): ensures generated secrets exist in `~/IdeaProjects/ProjectNexusAI/.sops.env`
  (use `~/.local/bin/nexus-secret put` with a random value piped in, only when the key is missing):
  `VISIONCLAW_LIVEKIT_KEY`, `VISIONCLAW_LIVEKIT_SECRET`, `VISIONCLAW_STATE_SECRET`,
  `VISIONCLAW_SERVICE_TOKEN`, `VISIONCLAW_CHAIRMAN_CODE`. Then writes `gateway.env` and `agent.env` on the VPS
  under `/opt/visionclaw/deploy/vps/` (mode 600) from those plus `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `GEMINI_API_KEY` (as `GOOGLE_API_KEY` for the agent) from the same sops file, and `OPS_AGENT_TOKEN` read on
  the VPS from `/opt/ops/ops.secrets`. Copy code with `git archive origin/main gateway agent deploy/vps | ssh ... tar -x`.
  `docker compose up -d --build`. Also: copy `ops/ops_server.py` to `/opt/ops/` and add
  `CEO_INBOX_URL=http://100.120.229.126:8791/chairman` + `CEO_INBOX_TOKEN` to `/opt/ops/ops.secrets`
  (backup first), rebuild ops; set `CEO_INBOX_TOKEN` and `GITHUB_TOKEN` on Coolify app
  `lgkk4cg8g4gkso8cgk8sssgs` through the Coolify API (`COOLIFY_URL`, `COOLIFY_API_TOKEN` in the same sops file;
  `PATCH /api/v1/applications/<uuid>/envs/bulk`, then `GET /api/v1/deploy?uuid=<uuid>`).
  Finish with checks that print status only: gateway `/health`, LiveKit signaling reachable, `vc-agent` log shows a
  registered worker, ops -> CEO inbox auth (empty text -> 400), and one real chairman turn:
  `POST /v1/chat/completions` with the chairman code and `X-Chairman: 1` asking EDITH to get a one-line status from
  the CEO; print the reply text (a reply is not a secret).
  Gateway env must cover every `process.env.X` the gateway reads that matters here: find them with grep; set
  `GATEWAY_TOKENS=<code>:chairman`, `CHAIRMAN_USER_ID=chairman`, `PUBLIC_BASE_URL=https://visionclaw.76-13-124-154.sslip.io`,
  `LIVEKIT_URL=wss://livekit.76-13-124-154.sslip.io`, `OPS_MCP_URL=https://ops.76-13-124-154.sslip.io/mcp`, plus the
  model settings from `gateway/fly.toml` `[env]`. Agent: `LIVEKIT_URL=ws://vc-livekit:7880`, `GATEWAY_URL=http://vc-gateway:8788`,
  service token, model settings from `agent/fly.toml`.
- Replace `~/.local/bin/visionclaw-deploy` with a two-line wrapper that runs `deploy/vps/deploy.sh` from the repo.
- `bash -n` and `docker compose config` (with dummy env files in a temp dir) must pass. Commit (not the env files).

## Task 4: deploy and verify (controller)

Push both repos, run `deploy/vps/deploy.sh`, fix what breaks, confirm the real chairman turn returns a CEO answer and
the exchange shows in Telegram. The chairman reads the code with
`envcrypt get ~/IdeaProjects/ProjectNexusAI/.sops.env VISIONCLAW_CHAIRMAN_CODE` and signs in through
"Have an access code or your own gateway?" with gateway URL `https://visionclaw.76-13-124-154.sslip.io`.

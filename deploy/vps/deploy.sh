#!/usr/bin/env bash
# deploy.sh: run VisionClaw's voice stack (LiveKit + gateway + voice agent) on our VPS,
# self-hosted: no Fly.io, no LiveKit Cloud. Chairman mode = EDITH, wired to the ops
# server and the CEO. Run from the Mac. Safe to rerun.
# Prints no secret values and writes no plaintext secret file on this Mac: secrets go
# from sops straight into ssh stdin and land only on the VPS (mode 600).
#
#   1. generated secrets exist in ProjectNexusAI/.sops.env (created only when missing)
#   2. ship gateway/ agent/ deploy/vps/ ops/ops_server.py from origin/main to /opt/visionclaw
#   3. write livekit.env, gateway.env, agent.env next to the compose file
#   4. docker compose up -d --build
#   5. ops server: CEO inbox env.  CEO (Coolify): CEO_INBOX_TOKEN + GITHUB_TOKEN
#   6. checks (status only)
set -euo pipefail
PN=$HOME/IdeaProjects/ProjectNexusAI
SOPS=$PN/.sops.env
VC=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CEO_APP=lgkk4cg8g4gkso8cgk8sssgs
HOST=visionclaw.76-13-124-154.sslip.io
LK_HOST=livekit.76-13-124-154.sslip.io
D=/opt/visionclaw   # code is replaced on every run; gateway data lives in $D/data
V=$D/deploy/vps     # compose file + env files
step() { printf '\n== %s\n' "$*"; }
vps() {  # vps "<remote command>", stdin passes through
  (cd "$PN/ProjectNexus-github" && envcrypt run .sops.env -- sh -c \
    'SSHPASS="$VPS_ROOT_SSH_PASSWORD" exec sshpass -e ssh -o StrictHostKeyChecking=accept-new root@76.13.124.154 "$1"' _ "$1")
}
put_env() {  # put_env <file>: stdin -> $V/<file> on the VPS, mode 600
  vps "umask 077; cat > $V/$1 && echo \"$1: \$(grep -c = $V/$1) vars\""
}

step "1/6 secrets"
for k in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY GITHUB_TOKEN COOLIFY_URL COOLIFY_API_TOKEN CEO_INBOX_TOKEN; do
  grep -q "^$k=" "$SOPS" || { echo "missing $k in $SOPS" >&2; exit 1; }
done
gen() {  # gen <KEY> <command printing a random value>
  local key=$1; shift
  if grep -q "^$key=" "$SOPS"; then echo "$key: present"
  else "$@" | "$HOME/.local/bin/nexus-secret" put "$SOPS" "$key"; fi
}
gen VISIONCLAW_LIVEKIT_KEY sh -c 'printf "API%s" "$(openssl rand -hex 6)"'
gen VISIONCLAW_LIVEKIT_SECRET openssl rand -hex 32
gen VISIONCLAW_STATE_SECRET openssl rand -hex 32
gen VISIONCLAW_SERVICE_TOKEN openssl rand -hex 32
gen VISIONCLAW_CHAIRMAN_CODE openssl rand -hex 16

step "2/6 ship code (origin/main)"
git -C "$VC" fetch -q origin
git -C "$VC" cat-file -e origin/main:deploy/vps/docker-compose.yml 2>/dev/null ||
  { echo "deploy/vps is not on origin/main yet; push first" >&2; exit 1; }
echo "VisionClaw at $(git -C "$VC" log --oneline -1 origin/main)"
git -C "$VC" archive origin/main gateway agent deploy/vps ops/ops_server.py |
  vps "mkdir -p $D/data && chmod 700 $D && cd $D && rm -rf gateway agent deploy ops && tar -x && echo shipped to $D"

step "3/6 env files (secrets only; settings live in docker-compose.yml)"
envcrypt run "$SOPS" -- sh -c 'printf "%s\n" "LIVEKIT_KEYS=$VISIONCLAW_LIVEKIT_KEY: $VISIONCLAW_LIVEKIT_SECRET"' |
  put_env livekit.env
envcrypt run "$SOPS" -- sh -c 'printf "%s\n" \
  "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
  "GATEWAY_TOKENS=$VISIONCLAW_CHAIRMAN_CODE:chairman" \
  "GATEWAY_SERVICE_TOKEN=$VISIONCLAW_SERVICE_TOKEN" \
  "STATE_SECRET=$VISIONCLAW_STATE_SECRET" \
  "LIVEKIT_API_KEY=$VISIONCLAW_LIVEKIT_KEY" \
  "LIVEKIT_API_SECRET=$VISIONCLAW_LIVEKIT_SECRET"' |
  put_env gateway.env
# The ops bearer never leaves the VPS: copied from the ops server's own secrets.
vps "umask 077; grep '^OPS_AGENT_TOKEN=' /opt/ops/ops.secrets >> $V/gateway.env && echo 'gateway.env: OPS_AGENT_TOKEN added' ||
  { echo 'no OPS_AGENT_TOKEN in /opt/ops/ops.secrets' >&2; exit 1; }"
envcrypt run "$SOPS" -- sh -c 'printf "%s\n" \
  "LIVEKIT_API_KEY=$VISIONCLAW_LIVEKIT_KEY" \
  "LIVEKIT_API_SECRET=$VISIONCLAW_LIVEKIT_SECRET" \
  "GATEWAY_SERVICE_TOKEN=$VISIONCLAW_SERVICE_TOKEN" \
  "GOOGLE_API_KEY=$GEMINI_API_KEY" \
  "OPENAI_API_KEY=$OPENAI_API_KEY"' |
  put_env agent.env

step "4/6 build + start livekit, gateway, agent"
vps "cd $V && docker compose up -d --build > /tmp/visionclaw-up.log 2>&1 || { tail -30 /tmp/visionclaw-up.log; exit 1; }; tail -5 /tmp/visionclaw-up.log"

step "5/6 ops server + CEO"
vps "cp $D/ops/ops_server.py /opt/ops/ops_server.py && echo 'ops_server.py updated'"
envcrypt run "$SOPS" -- sh -c \
  'printf "CEO_INBOX_URL=http://100.120.229.126:8791/chairman\nCEO_INBOX_TOKEN=%s\n" "$CEO_INBOX_TOKEN"' |
  vps 'umask 077; cd /opt/ops && cp -p ops.secrets ops.secrets.bak.$(date +%s) && { grep -v "^CEO_INBOX_" ops.secrets; cat; } > s.new &&
    grep -q "^CEO_INBOX_TOKEN=." s.new && chmod 600 s.new && mv s.new ops.secrets || { echo "ops.secrets not updated" >&2; exit 1; }
    ls -t ops.secrets.bak.* 2>/dev/null | tail -n +4 | xargs -r rm -f
    docker compose up -d --build > /tmp/ops-up.log 2>&1 || { tail -30 /tmp/ops-up.log; exit 1; }; tail -1 /tmp/ops-up.log'
# The Coolify API token in $SOPS is stale (401); the live one is in viktron-backend's sops
# file. Nesting envcrypt lets that file's COOLIFY_API_TOKEN override; COOLIFY_URL stays.
envcrypt run "$SOPS" -- envcrypt run "$HOME/IdeaProjects/${COOLIFY_SOPS:-viktron-backend}/.sops.env" -- python3 - "$CEO_APP" <<'EOF'
import json, os, sys, urllib.request
base, tok, app = os.environ["COOLIFY_URL"].rstrip("/"), os.environ["COOLIFY_API_TOKEN"], sys.argv[1]
def call(method, path, body=None):
    req = urllib.request.Request(f"{base}/api/v1{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()
s, b = call("PATCH", f"/applications/{app}/envs/bulk",
            {"data": [{"key": k, "value": os.environ[k]} for k in ("CEO_INBOX_TOKEN", "GITHUB_TOKEN")]})
print("CEO env:", s)
if s < 300:
    print("CEO deploy queued:", call("GET", f"/deploy?uuid={app}&force=false")[0])
else:
    print("Set CEO_INBOX_TOKEN and GITHUB_TOKEN in the Coolify UI and redeploy.")
EOF

step "6/6 checks"
for _ in $(seq 1 24); do curl -sf -m 5 "https://$HOST/health" >/dev/null && break; sleep 5; done
echo "gateway https://$HOST/health: $(curl -s -m 5 "https://$HOST/health" || echo unreachable)"
lk=000
for _ in $(seq 1 12); do
  lk=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "https://$LK_HOST" || true)
  [ "$lk" != 000 ] && break; sleep 5
done
echo "livekit signaling https://$LK_HOST: HTTP $lk (000 = unreachable)"
vps 'for _ in $(seq 1 12); do l=$(docker logs vc-agent 2>&1 | grep -i "registered worker" | tail -1); [ -n "$l" ] && break; sleep 5; done
if [ -n "$l" ]; then echo "agent: $l"; else echo "agent: no registered worker; last errors:"; docker logs vc-agent 2>&1 | grep -iE "error|Traceback" | tail -3; fi; true'
# Room ticket with the chairman code: proves the code signs in and the gateway holds LiveKit keys.
# Bearers go to curl on stdin (-H @-), never in argv where `ps` would show them.
envcrypt run "$SOPS" -- sh -c 'printf "Authorization: Bearer %s\n" "$VISIONCLAW_CHAIRMAN_CODE" |
  curl -s -m 20 -X POST "https://$1/livekit-token" -H @- -H "Content-Type: application/json" -d "{\"chairman\":true}"' _ "$HOST" |
  python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print("room ticket:", d["url"], d["room"]) if "token" in d else print("room ticket: failed", d.get("error"))
except Exception as e: print("room ticket: bad response", type(e).__name__)' || echo "room ticket: request failed"
echo "Waiting 90s for the CEO container..."; sleep 90
vps "docker exec ops-mcp python -c \"import os,httpx
try: print('ops -> CEO inbox:', httpx.post(os.environ['CEO_INBOX_URL'], json={'text': ''}, headers={'Authorization': 'Bearer '+os.environ['CEO_INBOX_TOKEN']}, timeout=10).status_code, '(400 = good)')
except Exception as e: print('ops -> CEO inbox: unreachable', type(e).__name__)\"" || echo "ops -> CEO inbox: check failed"
vps 'c=$(docker ps --format "{{.Names}}" | grep "^nexus_agent-'"$CEO_APP"'" | head -1); docker exec "$c" sh -c '"'"'git ls-remote -q "https://x-access-token:$GITHUB_TOKEN@github.com/vikasvardhanv/ProjectNexus.git" HEAD >/dev/null 2>&1 && echo "CEO repo clone: OK" || echo "CEO repo clone: FAIL"'"'"'' || echo "CEO repo clone: check failed"
# A real chairman turn. It goes through the service token + X-User-Id, the same path the
# voice agent uses: with the chairman code alone, spawn mode answers with an instant
# acknowledgement and delivers the result later over the app's WebSocket.
echo "chairman turn (EDITH -> CEO, up to 200s)..."
envcrypt run "$SOPS" -- sh -c 'printf "Authorization: Bearer %s\n" "$VISIONCLAW_SERVICE_TOKEN" |
  curl -s -m 200 "https://$1/v1/chat/completions" -H @- -H "X-User-Id: chairman" -H "X-Chairman: 1" -H "Content-Type: application/json" -d "$2"' _ "$HOST" \
  '{"messages":[{"role":"user","content":"EDITH, ask the CEO for a one-line status update and tell me what he said."}]}' |
  python3 -c 'import json,sys
try: t = json.load(sys.stdin)["choices"][0]["message"]["content"]
except Exception as e: print("chairman turn: no reply", type(e).__name__); sys.exit()
# SPAWN_ACK in gateway/src/server.ts: the turn outran the 110s service budget.
print("chairman turn: still running after the service budget (slow, not a pass)") if t.startswith("[task started]") else print("EDITH:", t)' || echo "chairman turn: request failed"

cat <<MSG

Chairman sign-in:
  1. Read the access code (only you, in your own terminal):
       envcrypt get ~/IdeaProjects/ProjectNexusAI/.sops.env VISIONCLAW_CHAIRMAN_CODE
  2. In the app: "Have an access code or your own gateway?"
       Gateway URL: https://$HOST
       Access code: the value from step 1
  3. Turn Chairman mode on and start a call.
MSG

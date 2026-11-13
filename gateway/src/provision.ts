import { anthropic } from "./cma.js";
import { config } from "./config.js";
import { loadStore, saveStore, userResources, type UserResources } from "./store.js";
import { activeApps } from "./apps.js";

const AGENT_SYSTEM_PROMPT = `You are the action agent behind a voice assistant that runs on smart glasses and phones.
The user talks to a real-time voice layer; that layer delegates tasks to you and speaks your replies aloud.

Ground rules:
- Reply in plain spoken prose. No markdown, no headers, no bullet lists, no URLs read out character by character.
- Lead with the answer in one or two sentences. Add detail only when the task genuinely needs it.
- Connected apps (calendar and similar) appear as tools when the owner has linked them. If a tool reports that
  authorization is required, do not retry it and never read a URL aloud: say in one sentence that the app needs to be
  connected in the app's settings, and carry on with what you can do.
- For multi-step work, start immediately and keep intermediate narration to a single short sentence.
- If a task cannot be completed, say what you tried and what is missing, in one sentence.`;

const CHAIRMAN_SYSTEM_PROMPT = `${AGENT_SYSTEM_PROMPT}

Chairman mode: the person speaking is the chairman who owns the company, and you act for them through the ops tools.
- ask_ceo hands an instruction to the CEO agent, which runs the business and executes through Agent Zero on Thor.
  Use it for anything addressed to the CEO, and for business work the CEO owns (content, revenue, missions, research).
- Use the GitHub, Coolify, service health and trading tools directly for checks and for opening PRs.
- Merges and trades happen only after the chairman taps Approve in Telegram. After request_merge or a trade
  proposal, say it is waiting for their tap. Never say a merge or trade happened unless a tool result says so.
- Relay what the CEO said in one or two spoken sentences, not its whole reply.`;

/** An agent config the gateway owns. The chairman profile carries only the ops server. */
interface Profile {
  storeKey: "agentId" | "chairmanAgentId";
  name: string;
  system: string;
  servers: () => ReturnType<typeof mcpServers>;
  tools: () => ReturnType<typeof agentTools>;
}

const DEFAULT_PROFILE: Profile = {
  storeKey: "agentId",
  name: "VisionClaw Action Agent",
  system: AGENT_SYSTEM_PROMPT,
  servers: () => mcpServers(),
  tools: () => agentTools(),
};

const CHAIRMAN_PROFILE: Profile = {
  storeKey: "chairmanAgentId",
  name: "VisionClaw Chairman Agent",
  system: CHAIRMAN_SYSTEM_PROMPT,
  servers: () => [{ type: "url" as const, name: "ops", url: config.opsMcpUrl! }],
  tools: () => [
    { type: "agent_toolset_20260401" as const },
    // always_allow is safe here only because the ops server itself refuses merges
    // and trades without the chairman's Telegram tap.
    { type: "mcp_toolset" as const, mcp_server_name: "ops",
      default_config: { permission_policy: { type: "always_allow" as const } } },
  ],
};

/** Store key for the chairman's separate vault + session. */
export const chairmanKey = (userId: string) => `${userId}~chairman`;

// Drift checks cost Anthropic API round-trips before every task; config only
// changes on deploy, so verify at most once per interval per process.
const RECONCILE_TTL_MS = 10 * 60 * 1000;
const sharedVerifiedAt = new Map<string, number>();
const sessionVerifiedAt = new Map<string, number>();

/** Create the shared environment + agent once; IDs persist in the store. */
export async function ensureShared(p: Profile = DEFAULT_PROFILE): Promise<{ agentId: string; environmentId: string }> {
  const store = await loadStore();
  if (!store.shared.environmentId) {
    const env = await anthropic.beta.environments.create({
      name: "visionclaw-cloud",
      config: { type: "cloud", networking: { type: "unrestricted" } },
    });
    store.shared.environmentId = env.id;
    console.log("[provision] environment created:", env.id);
  }
  const version = p.storeKey === "agentId" ? "agentVersion" : "chairmanAgentVersion";
  const existing = store.shared[p.storeKey];
  if (!existing) {
    const agent = await anthropic.beta.agents.create({
      name: p.name,
      model: wantModel(),
      system: p.system,
      mcp_servers: p.servers(),
      tools: p.tools(),
    });
    store.shared[p.storeKey] = agent.id;
    store.shared[version] = agent.version;
    console.log(`[provision] ${p.name} created:`, agent.id, "version", agent.version);
  } else if (Date.now() - (sharedVerifiedAt.get(p.storeKey) ?? 0) > RECONCILE_TTL_MS) {
    sharedVerifiedAt.set(p.storeKey, Date.now());
    // Reconcile: adding an app to the registry must reach an agent that already
    // exists, or its new tools are silently missing. Version-bump only on drift.
    const agent = await anthropic.beta.agents.retrieve(existing);
    // Compare servers *and* tool config: a changed permission policy is drift
    // just as much as a new app is.
    const appsDrifted =
      appSignature(agent.mcp_servers ?? [], agent.tools ?? []) !== appSignature(p.servers(), p.tools());
    const modelDrifted = modelSignature(agent.model) !== modelSignature(wantModel());
    const systemDrifted = (agent.system ?? "") !== p.system;
    if (appsDrifted || modelDrifted || systemDrifted) {
      const updated = await anthropic.beta.agents.update(existing, {
        mcp_servers: p.servers(),
        tools: p.tools(),
        model: wantModel(),
        system: p.system,
      });
      store.shared[version] = updated.version;
      const what = [appsDrifted && "apps", modelDrifted && "model", systemDrifted && "system"]
        .filter(Boolean)
        .join(" + ");
      console.log(`[provision] ${p.name} ${what} reconciled, version`, updated.version);
    }
  }
  await saveStore();
  return { agentId: store.shared[p.storeKey]!, environmentId: store.shared.environmentId };
}

/**
 * Model config as stored on the agent. Reconciled alongside the app surface,
 * because AGENT_MODEL / AGENT_EFFORT are otherwise read only when the agent is
 * first created -- changing them later looks like it works and silently does
 * nothing, which cost us a round of latency measurements against a setting that
 * was never applied.
 */
function modelSignature(model: unknown): string {
  const m = model as { id?: string; effort?: { type?: string } } | null;
  return `${m?.id ?? ""}:${m?.effort?.type ?? ""}`;
}

function wantModel() {
  return { id: config.agentModel, effort: config.agentEffort ? { type: config.agentEffort } : null };
}

/** Canonical projection of the app surface, for drift detection. */
function appSignature(servers: readonly unknown[], tools: readonly unknown[]): string {
  const s = servers
    .map((raw) => {
      const x = raw as { name?: string; url?: string };
      return `${x.name}@${x.url}`;
    })
    .sort();
  const t = tools
    .map((raw) => {
      const x = raw as {
        type?: string;
        mcp_server_name?: string;
        default_config?: { permission_policy?: { type?: string } } | null;
      };
      // Compare only what we manage. The server normalizes stored configs --
      // agent_toolset comes back with a default_config we never sent -- so
      // comparing the whole shape meant have != want on every request: an
      // agents.update (deduped server-side, version never moved) plus a
      // sessions.update before every single turn, forever.
      if (x.type !== "mcp_toolset") return `${x.type}`;
      return `${x.type}:${x.mcp_server_name ?? ""}:${x.default_config?.permission_policy?.type ?? ""}`;
    })
    .sort();
  return JSON.stringify({ s, t });
}

function mcpServers() {
  return activeApps().map((a) => ({ type: "url" as const, name: a.id, url: a.mcpUrl }));
}

function agentTools() {
  return [
    { type: "agent_toolset_20260401" as const },
    ...activeApps().map((a) => ({
      type: "mcp_toolset" as const,
      mcp_server_name: a.id,
      // Connected apps are read-only today, so a confirmation prompt would only
      // stall a hands-free voice turn. Destructive tools should flip to
      // always_ask and be surfaced as a spoken confirmation instead.
      default_config: { permission_policy: { type: "always_allow" as const } },
    })),
  ];
}

async function sessionUsable(sessionId: string): Promise<boolean> {
  try {
    const s = await anthropic.beta.sessions.retrieve(sessionId);
    return s.status !== "terminated" && s.archived_at == null;
  } catch {
    return false;
  }
}

/**
 * Lazily provision a user's vault and long-lived session.
 *
 * Memory stores are deliberately NOT provisioned or mounted (2026-08-06): the
 * agent dutifully wrote owner notes mid-task, putting 5-10s of bookkeeping on
 * the critical path of a voice answer. Existing memoryStoreId values in the
 * store are kept but unused; re-mounting is one resources entry away if a
 * memory feature earns its latency.
 */
export async function ensureUser(
  realUserId: string,
  chairman = false,
): Promise<UserResources & { sessionId: string; vaultId: string }> {
  const p = chairman ? CHAIRMAN_PROFILE : DEFAULT_PROFILE;
  const userId = chairman ? chairmanKey(realUserId) : realUserId;
  const { agentId, environmentId } = await ensureShared(p);
  const u = await userResources(userId);

  if (!u.vaultId) {
    const vault = await anthropic.beta.vaults.create({ display_name: `visionclaw-vault-${userId}` });
    u.vaultId = vault.id;
    console.log(`[provision] vault for ${userId}:`, vault.id);
    if (chairman) {
      // The ops bearer lives only in the chairman's vault; Anthropic injects it at
      // egress, so it never enters the sandbox. ponytail: written once at vault
      // creation -- rotating OPS_AGENT_TOKEN means deleting this store entry.
      await anthropic.beta.vaults.credentials.create(vault.id, {
        display_name: "ops MCP (chairman)",
        auth: { type: "static_bearer", mcp_server_url: config.opsMcpUrl!, token: config.opsAgentToken! },
      });
    }
  }

  const verifiedAt = u.sessionId ? (sessionVerifiedAt.get(u.sessionId) ?? 0) : 0;
  const needsCheck = Date.now() - verifiedAt > RECONCILE_TTL_MS;
  if (!u.sessionId || (needsCheck && !(await sessionUsable(u.sessionId)))) {
    const session = await anthropic.beta.sessions.create({
      agent: agentId,
      environment_id: environmentId,
      title: `visionclaw:${userId}`,
      vault_ids: [u.vaultId],
    });
    u.sessionId = session.id;
    sessionVerifiedAt.set(session.id, Date.now());
    console.log(`[provision] session for ${userId}:`, session.id);
  } else if (needsCheck) {
    sessionVerifiedAt.set(u.sessionId, Date.now());
    // A live session keeps the tool slate it was created with, so newly added
    // apps need a session-local update too. History is preserved.
    try {
      const live = await anthropic.beta.sessions.retrieve(u.sessionId);
      const have = appSignature(live.agent?.mcp_servers ?? [], live.agent?.tools ?? []);
      const want = appSignature(p.servers(), p.tools());
      if (have !== want && live.status === "idle") {
        await anthropic.beta.sessions.update(u.sessionId, {
          agent: { mcp_servers: p.servers(), tools: p.tools() },
        });
        console.log(`[provision] session apps reconciled for ${userId}`);
      }
    } catch (err) {
      console.warn(`[provision] could not reconcile session apps for ${userId}:`, err);
    }
  }

  await saveStore();
  return u as UserResources & { sessionId: string; vaultId: string };
}

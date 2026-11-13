import "dotenv/config";

export interface GatewayConfig {
  port: number;
  storePath: string;
  /** token -> userId. Parsed from GATEWAY_TOKENS="tokenA:alice,tokenB:bob". */
  tokens: Map<string, string>;
  agentModel: string;
  /** Reasoning effort for the action agent; null = no extended reasoning. */
  agentEffort: "low" | "medium" | "high" | "xhigh" | "max" | null;
  /** How long a /v1/chat/completions call waits before converting to a background task. */
  quickAnswerTimeoutMs: number;
  /**
   * Return an acknowledgement immediately instead of racing the agent against a
   * deadline. A voice turn wants a reply in the assistant's own voice within a
   * beat, not the right answer eight seconds later -- and once the request never
   * carries the result, there is no deadline left to pick and no inline-vs-
   * deferred branch to land on the wrong side of.
   */
  spawnMode: boolean;
  /**
   * Trusted backend credential: a caller presenting this token plus an
   * X-User-Id header acts as that user. Held only by the LiveKit agent worker,
   * which authenticates users itself via room-token identity.
   */
  serviceToken?: string;
  /** Google sign-in: whether unknown accounts may register at all. */
  registrationOpen: boolean;
  /** Email domains whose new accounts skip the approval queue. */
  autoApproveDomains: string[];
  /** Approve every new account (study-window convenience; the kill switch is registrationOpen). */
  autoApproveAll: boolean;
  /**
   * Chairman mode: the one userId whose app switch may route turns to the
   * chairman agent (ops MCP: CEO, GitHub, VPS, Thor). Any other user's switch is
   * ignored server-side. Off unless all three are set.
   */
  chairmanUserId?: string;
  opsMcpUrl?: string;
  opsAgentToken?: string;
}

function parseTokens(raw: string | undefined): Map<string, string> {
  const map = new Map<string, string>();
  if (!raw) return map;
  for (const pair of raw.split(",")) {
    const [token, userId] = pair.split(":").map((s) => s.trim());
    if (token && userId) map.set(token, userId);
  }
  return map;
}

export const config: GatewayConfig = {
  port: Number(process.env.PORT ?? 8788),
  storePath: process.env.STORE_PATH ?? "./data/gateway-store.json",
  tokens: parseTokens(process.env.GATEWAY_TOKENS),
  // Sonnet with no reasoning effort: the voice loop's tasks are mostly tool
  // dispatch, where per-step thinking passes dominate latency (a 2s pair of
  // lookups measured 32s end-to-end on opus/low).
  agentModel: process.env.AGENT_MODEL ?? "claude-sonnet-5",
  agentEffort:
    !process.env.AGENT_EFFORT || process.env.AGENT_EFFORT === "none"
      ? null
      : (process.env.AGENT_EFFORT as Exclude<GatewayConfig["agentEffort"], null>),
  quickAnswerTimeoutMs: Number(process.env.QUICK_ANSWER_TIMEOUT_MS ?? 30_000),
  spawnMode: process.env.SPAWN_MODE !== "false",
  serviceToken: process.env.GATEWAY_SERVICE_TOKEN || undefined,
  registrationOpen: process.env.REGISTRATION_OPEN !== "false",
  autoApproveDomains: (process.env.AUTO_APPROVE_DOMAINS ?? "")
    .split(",")
    .map((d) => d.trim().toLowerCase())
    .filter(Boolean),
  autoApproveAll: process.env.AUTO_APPROVE_ALL === "true",
  chairmanUserId: process.env.CHAIRMAN_USER_ID || undefined,
  opsMcpUrl: process.env.OPS_MCP_URL || undefined,
  opsAgentToken: process.env.OPS_AGENT_TOKEN || undefined,
};

/** True only for the configured chairman, and only when the ops server is wired. */
export function isChairman(userId: string): boolean {
  return !!(config.chairmanUserId && config.opsMcpUrl && config.opsAgentToken) &&
    userId === config.chairmanUserId;
}

if (config.tokens.size === 0) {
  console.warn(
    "[config] GATEWAY_TOKENS is empty - no client will be able to authenticate. " +
      'Set GATEWAY_TOKENS="sometoken:someUserId" in .env',
  );
}

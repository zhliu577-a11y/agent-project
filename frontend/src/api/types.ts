export type ViewId = "overview" | "plugins" | "chat" | "capabilities" | "activity";

export interface PluginRuntimeState {
  name: string;
  scope: "builtin" | "external";
  status: string;
  enabled: boolean;
  contributions: string[];
  error: string | null;
}

export interface McpRuntimeState {
  name: string;
  status: string;
  preload: boolean;
}

export interface McpPreloadState {
  preload: string[];
  available: string[];
  mcp: McpRuntimeState[];
}

export interface McpPreloadResult extends McpPreloadState {
  restartRequired?: boolean;
}

export interface ActivityEvent {
  seq: number;
  eventId: string;
  timestamp: string;
  name: string;
  publisher: string | null;
  actorKind: string | null;
  actorName: string | null;
  sessionId: string | null;
  runId: string | null;
  turnId: string | null;
  traceId: string | null;
  causationId: string | null;
  correlationId: string | null;
  status: string;
  target: string | null;
  summary: string;
  payload: Record<string, unknown>;
}

export interface ActivityEventPage {
  events: ActivityEvent[];
  latestSeq: number;
}

export interface ModelRuntimeState {
  name: string;
  status: string;
  active: boolean;
}

export interface ModelActivationResult {
  activeModel: string;
  models: ModelRuntimeState[];
}

export interface ModelSettings {
  name: string;
  hasApiKey: boolean;
  baseUrl?: string;
  model?: string;
  timeout?: number;
  maxRetries?: number;
  disableResponseStorage?: boolean;
}

export interface ModelSettingsUpdate {
  apiKey?: string;
  clearApiKey?: boolean;
  baseUrl?: string | null;
  model?: string | null;
  timeout?: number | null;
  maxRetries?: number | null;
  disableResponseStorage?: boolean;
  activate?: boolean;
}

export interface ModelSettingsResult {
  settings: ModelSettings;
  models: ModelRuntimeState[];
}

export interface SkillUsage {
  requests: number;
  loads: number;
  resource_loads: number;
  denied: number;
  approval_requests: number;
  approvals_granted: number;
  approvals_denied: number;
  load_failures: number;
  resource_failures: number;
  trigger_hits: number;
}

export interface SkillRuntimeState {
  name: string;
  status: string;
  preload: boolean;
  loaded: boolean;
  bytes: number;
  access: string;
  priority: number;
  listing: string;
  usage: SkillUsage;
  error: string | null;
}

export interface RuntimeSnapshot {
  ready: boolean;
  plugins: PluginRuntimeState[];
  tools: string[];
  mcp: McpRuntimeState[];
  models: ModelRuntimeState[];
  skills: SkillRuntimeState[];
}

export interface RuntimeStatus {
  ready: boolean;
  started: boolean;
  sessionId: string | null;
  startedAt: string | null;
  busy: boolean;
  error: string | null;
  snapshot?: RuntimeSnapshot | null;
}

export interface InstalledPlugin {
  name: string;
  installed: boolean;
  enabled: boolean;
  version: string;
  source: string;
  path: string;
  digest: string;
  installedAt: string;
  updatedAt: string;
  runtimeStatus: string;
  lastError: string | null;
  contributions: string[];
  runtimeError: string | null;
}

export interface ContributionInspection {
  kind: string;
  id: string;
  name: string;
  version: string;
  description: string;
  protocolVersion: number;
  contract: string;
  requires: Array<{
    kind: string;
    name: string | null;
    contract: string | null;
    required: boolean;
    inject: string | null;
  }>;
}

export interface PackageInspection {
  name: string;
  version: string;
  description: string;
  contributions: ContributionInspection[];
}

export interface ApprovalInfo {
  id: string;
  kind: "skill" | "tool" | "prompt" | string;
  name: string;
  resource: string | null;
  arguments: Record<string, unknown> | null;
  created_at: string;
}

export interface RuntimeEvent {
  name: string;
  eventId: string;
  sessionId: string | null;
  runId: string | null;
  turnId: string | null;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  streaming?: boolean;
}

export interface ChatResult {
  sessionId: string;
  content: string;
  stopReason: string;
  messages: Array<Record<string, unknown>>;
  error: Record<string, unknown> | null;
}

export interface SessionSummary {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
  revision: number;
  active: boolean;
}

export interface SessionMessage {
  id: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  timestamp?: string;
}

export interface SessionDetail extends SessionSummary {
  messages: SessionMessage[];
}

export interface SessionDeleteResult {
  deleted: string;
  activeSessionId: string;
}

export interface StreamCallbacks {
  onToken: (delta: string) => void;
  onRuntime: (event: RuntimeEvent) => void;
  onApproval: (approval: ApprovalInfo) => void;
  onDone: (result: ChatResult) => void;
  onError: (error: Error) => void;
}

export interface PluginMutationResult {
  plugin: InstalledPlugin;
  restartRequired: boolean;
}

export interface ApprovalResolution {
  approval: ApprovalInfo;
  approved: boolean;
  reason: string | null;
}

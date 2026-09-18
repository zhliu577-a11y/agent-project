import type {
  ActivityEventPage,
  ApprovalInfo,
  ApprovalResolution,
  ChatResult,
  InstalledPlugin,
  McpPreloadResult,
  McpPreloadState,
  ModelActivationResult,
  ModelSettings,
  ModelSettingsResult,
  ModelSettingsUpdate,
  PackageInspection,
  PluginMutationResult,
  RuntimeEvent,
  RuntimeStatus,
  StreamCallbacks
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "/api/v1").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    throw await toApiError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

async function toApiError(response: Response): Promise<ApiError> {
  let message = `请求失败（${response.status}）`;
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string" && payload.detail) {
      message = payload.detail;
    } else if (Array.isArray(payload.detail)) {
      message = payload.detail
        .map((item) => {
          if (typeof item === "object" && item && "msg" in item) {
            return String((item as { msg: unknown }).msg);
          }
          return String(item);
        })
        .join("; ");
    }
  } catch {
    // The response body is not JSON.
  }
  return new ApiError(message, response.status);
}

export async function getRuntime(): Promise<RuntimeStatus> {
  return request<RuntimeStatus>("/runtime");
}

export async function startRuntime(): Promise<RuntimeStatus> {
  return request<RuntimeStatus>("/runtime/start", { method: "POST" });
}

export async function restartRuntime(): Promise<RuntimeStatus> {
  return request<RuntimeStatus>("/runtime/restart", { method: "POST" });
}

export async function getPlugins(): Promise<InstalledPlugin[]> {
  return request<InstalledPlugin[]>("/plugins");
}

export async function activateModel(name: string): Promise<ModelActivationResult> {
  return request<ModelActivationResult>(`/models/${encodeURIComponent(name)}/activate`, {
    method: "POST"
  });
}

export async function getModelSettings(): Promise<ModelSettings[]> {
  const payload = await request<{ settings: ModelSettings[] }>("/models/settings");
  return payload.settings;
}

export async function updateModelSettings(
  name: string,
  settings: ModelSettingsUpdate
): Promise<ModelSettingsResult> {
  return request<ModelSettingsResult>(`/models/${encodeURIComponent(name)}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings)
  });
}

export async function getMcpPreload(): Promise<McpPreloadState> {
  return request<McpPreloadState>("/mcp/preload");
}

export async function updateMcpPreload(
  preload: string[],
  restart = true
): Promise<McpPreloadResult> {
  return request<McpPreloadResult>("/mcp/preload", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preload, restart })
  });
}

export async function getActivityEvents(options: {
  after?: number;
  limit?: number;
  name?: string;
  publisher?: string;
  sessionId?: string;
  runId?: string;
  traceId?: string;
} = {}): Promise<ActivityEventPage> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(options)) {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  }
  const query = params.toString();
  return request<ActivityEventPage>(`/events${query ? `?${query}` : ""}`);
}

export async function inspectPlugin(file: File): Promise<PackageInspection> {
  return request<PackageInspection>("/plugins/inspect", {
    method: "POST",
    body: toFormData(file)
  });
}

export async function installPlugin(file: File): Promise<PluginMutationResult> {
  return request<PluginMutationResult>("/plugins/install", {
    method: "POST",
    body: toFormData(file)
  });
}

export async function enablePlugin(name: string): Promise<PluginMutationResult> {
  return request<PluginMutationResult>(`/plugins/${encodeURIComponent(name)}/enable`, {
    method: "POST"
  });
}

export async function disablePlugin(name: string): Promise<PluginMutationResult> {
  return request<PluginMutationResult>(`/plugins/${encodeURIComponent(name)}/disable`, {
    method: "POST"
  });
}

export async function removePlugin(name: string): Promise<{ removed: string; restartRequired: boolean }> {
  return request<{ removed: string; restartRequired: boolean }>(
    `/plugins/${encodeURIComponent(name)}`,
    { method: "DELETE" }
  );
}

export async function getApprovals(): Promise<ApprovalInfo[]> {
  const payload = await request<{ approvals: ApprovalInfo[] }>("/approvals");
  return payload.approvals;
}

export async function resolveApproval(
  id: string,
  approved: boolean,
  reason?: string
): Promise<ApprovalResolution> {
  return request<ApprovalResolution>(`/approvals/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved, reason })
  });
}

export async function streamChat(
  message: string,
  callbacks: StreamCallbacks,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, maxTurns: 20 }),
    signal
  });
  if (!response.ok) {
    throw await toApiError(response);
  }
  if (!response.body) {
    throw new ApiError("浏览器不支持流式响应", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        dispatchSseEvent(block, callbacks);
      }
      if (done) {
        if (buffer.trim()) {
          dispatchSseEvent(buffer, callbacks);
        }
        break;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function dispatchSseEvent(block: string, callbacks: StreamCallbacks): void {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) {
    return;
  }
  let data: unknown;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    data = dataLines.join("\n");
  }

  if (eventName === "token") {
    const delta = (data as { delta?: unknown }).delta;
    callbacks.onToken(typeof delta === "string" ? delta : "");
  } else if (eventName === "runtime") {
    callbacks.onRuntime(data as RuntimeEvent);
  } else if (eventName === "approval") {
    callbacks.onApproval(data as ApprovalInfo);
  } else if (eventName === "done") {
    callbacks.onDone(data as ChatResult);
  } else if (eventName === "error") {
    const message = (data as { message?: unknown }).message;
    callbacks.onError(new ApiError(typeof message === "string" ? message : "对话失败", 500));
  }
}

function toFormData(file: File): FormData {
  const form = new FormData();
  form.append("package", file, file.name);
  return form;
}

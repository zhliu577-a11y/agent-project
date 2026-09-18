import {
  BookOpen,
  Boxes,
  Braces,
  CheckCircle2,
  Cpu,
  KeyRound,
  RefreshCw,
  Save,
  Server,
  Settings2,
  X,
  Wrench
} from "lucide-react";
import { useState } from "react";

import {
  activateModel,
  updateMcpPreload,
  getModelSettings,
  updateModelSettings
} from "../api/client";
import type {
  ModelSettingsUpdate,
  RuntimeStatus
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { StatusPill } from "../components/StatusPill";
import { formatBytes } from "../lib/format";

type CapabilityTab = "tools" | "mcp" | "models" | "skills";

interface ModelSettingsForm {
  apiKey: string;
  baseUrl: string;
  model: string;
  timeout: string;
  maxRetries: string;
  disableResponseStorage: boolean;
}

const EMPTY_MODEL_SETTINGS: ModelSettingsForm = {
  apiKey: "",
  baseUrl: "",
  model: "",
  timeout: "",
  maxRetries: "",
  disableResponseStorage: true
};

interface CapabilitiesViewProps {
  runtime: RuntimeStatus | null;
  onRefresh: (silent?: boolean) => Promise<void>;
  onNotify: (message: string, tone?: "success" | "error" | "info") => void;
}

export function CapabilitiesView({
  runtime,
  onRefresh,
  onNotify
}: CapabilitiesViewProps) {
  const [tab, setTab] = useState<CapabilityTab>("tools");
  const [activating, setActivating] = useState<string | null>(null);
  const [configuring, setConfiguring] = useState<string | null>(null);
  const [settingsLoading, setSettingsLoading] = useState<string | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [mcpSaving, setMcpSaving] = useState<string | null>(null);
  const [settingsForm, setSettingsForm] =
    useState<ModelSettingsForm>(EMPTY_MODEL_SETTINGS);
  const [hasApiKey, setHasApiKey] = useState(false);
  const [hasResponseStorage, setHasResponseStorage] = useState(false);
  const snapshot = runtime?.snapshot;

  if (!runtime?.ready || !snapshot) {
    return (
      <div className="stack">
        <EmptyState icon={Boxes} title="Runtime 未就绪" detail="启动 Runtime 后读取能力目录。" />
      </div>
    );
  }

  const tabs = [
    { id: "tools" as const, label: "工具", count: snapshot.tools.length, icon: Wrench },
    { id: "mcp" as const, label: "MCP", count: snapshot.mcp.length, icon: Server },
    { id: "models" as const, label: "模型", count: snapshot.models.length, icon: Cpu },
    { id: "skills" as const, label: "Skill", count: snapshot.skills.length, icon: BookOpen }
  ];

  const activate = async (name: string) => {
    setActivating(name);
    try {
      await activateModel(name);
      await onRefresh(true);
      onNotify(`已切换到模型 ${name}`, "success");
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "模型切换失败", "error");
    } finally {
      setActivating(null);
    }
  };

  const openSettings = async (name: string) => {
    setSettingsLoading(name);
    try {
      const settings = (await getModelSettings()).find((item) => item.name === name);
      if (!settings) {
        throw new Error(`未找到模型 ${name} 的配置`);
      }
      setSettingsForm({
        apiKey: "",
        baseUrl: settings.baseUrl ?? "",
        model: settings.model ?? "",
        timeout: settings.timeout === undefined ? "" : String(settings.timeout),
        maxRetries: settings.maxRetries === undefined ? "" : String(settings.maxRetries),
        disableResponseStorage: settings.disableResponseStorage ?? true
      });
      setHasApiKey(settings.hasApiKey);
      setHasResponseStorage(settings.disableResponseStorage !== undefined);
      setConfiguring(name);
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "读取模型配置失败", "error");
    } finally {
      setSettingsLoading(null);
    }
  };

  const saveSettings = async () => {
    if (!configuring) {
      return;
    }
    setSettingsSaving(true);
    try {
      const payload: ModelSettingsUpdate = {
        baseUrl: settingsForm.baseUrl.trim() || null,
        model: settingsForm.model.trim() || null,
        timeout: settingsForm.timeout.trim() ? Number(settingsForm.timeout) : null,
        maxRetries: settingsForm.maxRetries.trim() ? Number(settingsForm.maxRetries) : null,
        activate: true
      };
      if (settingsForm.apiKey.trim()) {
        payload.apiKey = settingsForm.apiKey.trim();
      }
      if (hasResponseStorage) {
        payload.disableResponseStorage = settingsForm.disableResponseStorage;
      }
      await updateModelSettings(configuring, payload);
      await onRefresh(true);
      onNotify(`已保存并切换到模型 ${configuring}`, "success");
      setConfiguring(null);
      setSettingsForm(EMPTY_MODEL_SETTINGS);
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "模型配置保存失败", "error");
    } finally {
      setSettingsSaving(false);
    }
  };

  const toggleMcpPreload = async (name: string) => {
    const selected = new Set(
      snapshot.mcp.filter((item) => item.preload).map((item) => item.name)
    );
    if (selected.has(name)) {
      selected.delete(name);
    } else {
      selected.add(name);
    }
    setMcpSaving(name);
    try {
      await updateMcpPreload([...selected], true);
      await onRefresh(true);
      onNotify(
        selected.has(name) ? `已静态启用 ${name}` : `已改为按需连接 ${name}`,
        "success"
      );
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "MCP 静态启用保存失败", "error");
    } finally {
      setMcpSaving(null);
    }
  };

  return (
    <>
      <div className="stack">
      <section className="capability-tabs" role="tablist" aria-label="能力类型">
        {tabs.map(({ id, label, count, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className={tab === id ? "is-active" : ""}
            onClick={() => setTab(id)}
          >
            <Icon size={17} />
            <span>{label}</span>
            <strong>{count}</strong>
          </button>
        ))}
      </section>

      {tab === "tools" ? (
        <section className="panel">
          <div className="panel__header">
            <div>
              <span className="section-kicker">Tool Registry</span>
              <h3>模型可见工具</h3>
            </div>
            <Braces size={18} />
          </div>
          <div className="capability-grid">
            {snapshot.tools.map((tool) => (
              <article className="capability-item" key={tool}>
                <div className="capability-item__icon">
                  <Wrench size={17} />
                </div>
                <div>
                  <strong>{tool}</strong>
                  <span>{tool.includes("__") ? tool.split("__", 1)[0] : "host"}</span>
                </div>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {tab === "mcp" ? (
        <section className="panel">
          <div className="panel__header">
            <div>
              <span className="section-kicker">MCP Gateway</span>
              <h3>MCP 插件</h3>
            </div>
            <Server size={18} />
          </div>
          <div className="capability-list">
            {snapshot.mcp.map((item) => (
              <article className="capability-row" key={item.name}>
                <div className="capability-row__main">
                  <strong>{item.name}</strong>
                  <span>
                    {item.status === "loaded"
                      ? "工具已注册"
                      : item.status === "failed"
                        ? "连接失败，可稍后重试"
                        : "按需连接"}
                  </span>
                </div>
                <div className="capability-row__controls">
                  <label className="toggle-row toggle-row--compact">
                    <input
                      type="checkbox"
                      checked={item.preload}
                      disabled={mcpSaving !== null}
                      onChange={() => void toggleMcpPreload(item.name)}
                    />
                    <span>{item.preload ? "启动时加载" : "按需连接"}</span>
                  </label>
                  <StatusPill value={item.status} />
                </div>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {tab === "models" ? (
        <section className="panel">
          <div className="panel__header">
            <div>
              <span className="section-kicker">Model Gateway</span>
              <h3>模型提供方</h3>
            </div>
            <Cpu size={18} />
          </div>
          <div className="model-grid">
            {snapshot.models.map((model) => (
              <article className={`model-card ${model.active ? "is-active" : ""}`} key={model.name}>
                <div className="model-card__top">
                  <StatusPill value={model.status} />
                  <div className="model-card__controls">
                    {model.active ? <span className="badge">active</span> : null}
                    <button
                      className="icon-button icon-button--compact"
                      type="button"
                      title="配置模型连接"
                      disabled={settingsLoading !== null || settingsSaving}
                      onClick={() => void openSettings(model.name)}
                    >
                      <Settings2 size={16} className={settingsLoading === model.name ? "spin" : ""} />
                    </button>
                  </div>
                </div>
                <Cpu size={22} />
                <strong>{model.name}</strong>
                <span>{model.active ? "当前默认路由目标" : "由路由或 fallback 选择"}</span>
                <button
                  className="button button--secondary button--block"
                  type="button"
                  disabled={model.active || runtime.busy || activating !== null}
                  onClick={() => void activate(model.name)}
                >
                  {model.active ? <CheckCircle2 size={16} /> : <RefreshCw size={16} />}
                  {model.active
                    ? "当前模型"
                    : activating === model.name
                      ? "切换中…"
                      : "切换到此模型"}
                </button>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {tab === "skills" ? (
        <section className="panel">
          <div className="panel__header">
            <div>
              <span className="section-kicker">Skill Gateway</span>
              <h3>技能目录</h3>
            </div>
            <BookOpen size={18} />
          </div>
          {snapshot.skills.length ? (
            <div className="skill-grid">
              {snapshot.skills.map((skill) => (
                <article className="skill-card" key={skill.name}>
                  <div className="skill-card__header">
                    <strong>{skill.name}</strong>
                    <StatusPill value={skill.status} />
                  </div>
                  <div className="skill-card__meta">
                    <span>access {skill.access}</span>
                    <span>priority {skill.priority}</span>
                    <span>{formatBytes(skill.bytes)}</span>
                  </div>
                  <div className="skill-card__usage">
                    <span>
                      <strong>{skill.usage.requests}</strong> requests
                    </span>
                    <span>
                      <strong>{skill.usage.loads}</strong> loads
                    </span>
                    <span>
                      <strong>{skill.usage.approvals_granted}</strong> approvals
                    </span>
                  </div>
                  {skill.error ? <p className="inline-error">{skill.error}</p> : null}
                </article>
              ))}
            </div>
          ) : (
            <EmptyState icon={BookOpen} title="没有可见 Skill" />
          )}
        </section>
      ) : null}
      </div>

      {configuring ? (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !settingsSaving) {
              setConfiguring(null);
            }
          }}
        >
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="model-settings-title"
          >
            <div className="modal__header">
              <div>
                <span className="section-kicker">Model Provider</span>
                <h2 id="model-settings-title">{configuring}</h2>
              </div>
              <button
                className="icon-button"
                type="button"
                title="关闭"
                disabled={settingsSaving}
                onClick={() => setConfiguring(null)}
              >
                <X size={18} />
              </button>
            </div>

            <div className="model-settings-form">
              <label className="field field--wide">
                <span>
                  <KeyRound size={14} />
                  API Key
                </span>
                <input
                  type="password"
                  value={settingsForm.apiKey}
                  placeholder={hasApiKey ? "已配置，留空保持不变" : "输入 API Key"}
                  autoComplete="off"
                  onChange={(event) =>
                    setSettingsForm((current) => ({
                      ...current,
                      apiKey: event.target.value
                    }))
                  }
                />
              </label>

              <label className="field field--wide">
                <span>Base URL</span>
                <input
                  value={settingsForm.baseUrl}
                  placeholder={configuring === "deepseek" ? "https://api.deepseek.com" : "https://api.example.com/v1"}
                  onChange={(event) =>
                    setSettingsForm((current) => ({
                      ...current,
                      baseUrl: event.target.value
                    }))
                  }
                />
              </label>

              <label className="field field--wide">
                <span>模型名称</span>
                <input
                  value={settingsForm.model}
                  placeholder={configuring === "sub2api" ? "deepseek-v4-flash" : "模型 ID"}
                  onChange={(event) =>
                    setSettingsForm((current) => ({
                      ...current,
                      model: event.target.value
                    }))
                  }
                />
              </label>

              <label className="field">
                <span>超时（秒）</span>
                <input
                  type="number"
                  min="1"
                  max="600"
                  value={settingsForm.timeout}
                  placeholder="60"
                  onChange={(event) =>
                    setSettingsForm((current) => ({
                      ...current,
                      timeout: event.target.value
                    }))
                  }
                />
              </label>

              <label className="field">
                <span>最大重试</span>
                <input
                  type="number"
                  min="0"
                  max="20"
                  value={settingsForm.maxRetries}
                  placeholder="3"
                  onChange={(event) =>
                    setSettingsForm((current) => ({
                      ...current,
                      maxRetries: event.target.value
                    }))
                  }
                />
              </label>

              {hasResponseStorage ? (
                <label className="toggle-row field--wide">
                  <input
                    type="checkbox"
                    checked={settingsForm.disableResponseStorage}
                    onChange={(event) =>
                      setSettingsForm((current) => ({
                        ...current,
                        disableResponseStorage: event.target.checked
                      }))
                    }
                  />
                  <span>禁用服务端响应存储</span>
                </label>
              ) : null}
            </div>

            <div className="modal__actions">
              <button
                className="button button--secondary"
                type="button"
                disabled={settingsSaving}
                onClick={() => setConfiguring(null)}
              >
                取消
              </button>
              <button
                className="button button--primary"
                type="button"
                disabled={settingsSaving}
                onClick={() => void saveSettings()}
              >
                <Save size={16} />
                {settingsSaving ? "保存并重装配…" : "保存并激活"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </>
  );
}

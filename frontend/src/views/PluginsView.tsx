import {
  Boxes,
  ExternalLink,
  Power,
  PowerOff,
  RefreshCw,
  Search,
  Trash2,
  UploadCloud
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  disablePlugin,
  enablePlugin,
  removePlugin
} from "../api/client";
import type { InstalledPlugin, RuntimeStatus } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { PluginUploadPanel } from "../components/PluginUploadPanel";
import { StatusPill } from "../components/StatusPill";
import { contributionKind, formatDateTime } from "../lib/format";

interface PluginsViewProps {
  runtime: RuntimeStatus | null;
  plugins: InstalledPlugin[];
  onRefresh: (silent?: boolean) => Promise<void>;
  onRestart: () => Promise<void>;
  onNotify: (message: string, tone?: "success" | "error" | "info") => void;
}

export function PluginsView({
  runtime,
  plugins,
  onRefresh,
  onRestart,
  onNotify
}: PluginsViewProps) {
  const [scope, setScope] = useState<"external" | "builtin">("external");
  const [kind, setKind] = useState("all");
  const [query, setQuery] = useState("");
  const [busyName, setBusyName] = useState<string | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);

  const builtins = runtime?.snapshot?.plugins ?? [];
  const filteredExternal = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) {
      return plugins;
    }
    return plugins.filter((plugin) =>
      [plugin.name, plugin.version, plugin.contributions.join(" ")]
        .join(" ")
        .toLowerCase()
        .includes(normalized)
    );
  }, [plugins, query]);
  const filteredBuiltins = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) {
      return builtins;
    }
    return builtins.filter((plugin) =>
      [plugin.name, plugin.contributions.join(" ")]
        .join(" ")
        .toLowerCase()
        .includes(normalized)
    );
  }, [builtins, query]);
  const categories = useMemo(() => {
    const source = scope === "external" ? filteredExternal : filteredBuiltins;
    const counts = new Map<string, number>();
    for (const plugin of source) {
      for (const value of new Set(plugin.contributions.map(contributionKind))) {
        counts.set(value, (counts.get(value) ?? 0) + 1);
      }
    }
    return [...counts.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [filteredBuiltins, filteredExternal, scope]);
  const visibleExternal = useMemo(
    () => filterByKind(filteredExternal, kind),
    [filteredExternal, kind]
  );
  const visibleBuiltins = useMemo(
    () => filterByKind(filteredBuiltins, kind),
    [filteredBuiltins, kind]
  );

  const mutate = async (
    name: string,
    action: () => Promise<{ restartRequired: boolean }>,
    success: string
  ) => {
    setBusyName(name);
    try {
      const result = await action();
      setRestartRequired((current) => current || result.restartRequired);
      await onRefresh(true);
      onNotify(success, "success");
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "插件操作失败", "error");
    } finally {
      setBusyName(null);
    }
  };

  const remove = async (plugin: InstalledPlugin) => {
    if (!window.confirm(`确认移除插件 ${plugin.name}？`)) {
      return;
    }
    setBusyName(plugin.name);
    try {
      const result = await removePlugin(plugin.name);
      setRestartRequired((current) => current || result.restartRequired);
      await onRefresh(true);
      onNotify(`已移除 ${plugin.name}`, "success");
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "插件移除失败", "error");
    } finally {
      setBusyName(null);
    }
  };

  const restart = async () => {
    try {
      await onRestart();
      setRestartRequired(false);
      onNotify("Runtime 已按最新 Registry 重新装配", "success");
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "Runtime 重启失败", "error");
    }
  };

  return (
    <div className="stack">
      {restartRequired ? (
        <section className="notice notice--warning">
          <div>
            <strong>Registry 已更新</strong>
            <p>当前 Runtime 仍使用旧对象图，重新装配后插件变更才会生效。</p>
          </div>
          <button className="button button--primary" type="button" onClick={() => void restart()}>
            <RefreshCw size={16} />
            重启 Runtime
          </button>
        </section>
      ) : null}

      <div className="plugins-layout">
        <div className="plugins-layout__main">
          <section className="panel">
            <div className="panel__header panel__header--wrap">
              <div>
                <span className="section-kicker">Registry</span>
                <h3>插件目录</h3>
              </div>
              <div className="toolbar">
                <div className="segmented" role="tablist" aria-label="插件范围">
                  <button
                    type="button"
                    className={scope === "external" ? "is-active" : ""}
                    onClick={() => {
                      setScope("external");
                      setKind("all");
                    }}
                  >
                    外部安装
                    <span>{plugins.length}</span>
                  </button>
                  <button
                    type="button"
                    className={scope === "builtin" ? "is-active" : ""}
                    onClick={() => {
                      setScope("builtin");
                      setKind("all");
                    }}
                  >
                    内置
                    <span>{builtins.length}</span>
                  </button>
                </div>
                <label className="search-field">
                  <Search size={16} />
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="搜索插件"
                  />
                </label>
              </div>
            </div>

            <div className="plugin-categories" aria-label="插件分类">
              <button
                type="button"
                className={kind === "all" ? "is-active" : ""}
                onClick={() => setKind("all")}
              >
                全部
                <strong>
                  {(scope === "external" ? filteredExternal : filteredBuiltins).length}
                </strong>
              </button>
              {categories.map(([value, count]) => (
                <button
                  key={value}
                  type="button"
                  className={kind === value ? "is-active" : ""}
                  onClick={() => setKind(value)}
                >
                  {kindLabel(value)}
                  <strong>{count}</strong>
                </button>
              ))}
            </div>

            {scope === "external" ? (
              visibleExternal.length ? (
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>插件</th>
                        <th>状态</th>
                        <th>贡献</th>
                        <th>更新时间</th>
                        <th className="align-right">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleExternal.map((plugin) => (
                        <tr key={plugin.name}>
                          <td>
                            <div className="table-primary">
                              <strong>{plugin.name}</strong>
                              <span>{plugin.version || "unversioned"}</span>
                            </div>
                          </td>
                          <td>
                            <div className="status-stack">
                              <StatusPill value={plugin.enabled ? "enabled" : "disabled"} />
                              <StatusPill value={plugin.runtimeStatus} />
                            </div>
                          </td>
                          <td>
                            <div className="tag-list">
                              {plugin.contributions.slice(0, 3).map((item) => (
                                <span className="kind-tag" key={item}>
                                  {item.split(":", 1)[0]}
                                </span>
                              ))}
                            </div>
                          </td>
                          <td>{formatDateTime(plugin.updatedAt)}</td>
                          <td>
                            <div className="row-actions">
                              {plugin.enabled ? (
                                <button
                                  className="icon-button"
                                  type="button"
                                  title="禁用"
                                  disabled={busyName === plugin.name}
                                  onClick={() =>
                                    void mutate(
                                      plugin.name,
                                      () => disablePlugin(plugin.name),
                                      `已禁用 ${plugin.name}`
                                    )
                                  }
                                >
                                  <PowerOff size={16} />
                                </button>
                              ) : (
                                <button
                                  className="icon-button"
                                  type="button"
                                  title="启用"
                                  disabled={busyName === plugin.name}
                                  onClick={() =>
                                    void mutate(
                                      plugin.name,
                                      () => enablePlugin(plugin.name),
                                      `已启用 ${plugin.name}`
                                    )
                                  }
                                >
                                  <Power size={16} />
                                </button>
                              )}
                              <button
                                className="icon-button icon-button--danger"
                                type="button"
                                title="移除"
                                disabled={busyName === plugin.name}
                                onClick={() => void remove(plugin)}
                              >
                                <Trash2 size={16} />
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState
                  icon={UploadCloud}
                  title={query ? "没有匹配的插件" : "尚未安装外部插件"}
                  detail={query ? undefined : "上传符合清单协议的功能包。"}
                />
              )
            ) : visibleBuiltins.length ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>包</th>
                      <th>状态</th>
                      <th>贡献</th>
                      <th>范围</th>
                    </tr>
                  </thead>
                  <tbody>
                      {visibleBuiltins.map((plugin) => (
                      <tr key={plugin.name}>
                        <td>
                          <strong>{plugin.name}</strong>
                        </td>
                        <td>
                          <StatusPill value={plugin.status} />
                        </td>
                        <td>
                          <div className="tag-list">
                            {plugin.contributions.map((item) => (
                              <span className="kind-tag" key={item}>
                                {item}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td>{plugin.scope}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState icon={Boxes} title="没有匹配的内置插件" />
            )}
          </section>
        </div>

        <aside className="plugins-layout__side">
          <PluginUploadPanel
            onInstalled={async (required) => {
              setRestartRequired((current) => current || required);
              await onRefresh(true);
            }}
            onNotify={onNotify}
          />

          <section className="panel">
            <div className="panel__header">
              <div>
                <span className="section-kicker">Rules</span>
                <h3>生命周期</h3>
              </div>
              <ExternalLink size={18} />
            </div>
            <ol className="process-list">
              <li>
                <span>01</span>
                <div>
                  <strong>静态检查</strong>
                  <p>Manifest、契约、文件边界</p>
                </div>
              </li>
              <li>
                <span>02</span>
                <div>
                  <strong>原子安装</strong>
                  <p>摘要计算后写入 registry</p>
                </div>
              </li>
              <li>
                <span>03</span>
                <div>
                  <strong>显式启用</strong>
                  <p>安装后默认保持禁用</p>
                </div>
              </li>
              <li>
                <span>04</span>
                <div>
                  <strong>重新装配</strong>
                  <p>新依赖图由 Runtime 接管</p>
                </div>
              </li>
            </ol>
          </section>
        </aside>
      </div>
    </div>
  );
}

function filterByKind<T extends { contributions: string[] }>(
  plugins: T[],
  kind: string
): T[] {
  if (kind === "all") {
    return plugins;
  }
  return plugins.filter((plugin) =>
    plugin.contributions.some((value) => contributionKind(value) === kind)
  );
}

function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    tool: "工具",
    mcp: "MCP",
    hook: "Hook",
    skill: "Skill",
    memory: "记忆",
    "memory-index": "记忆索引",
    "memory-retriever": "召回",
    "memory-policy": "策略",
    "memory-extractor": "提取",
    session: "Session",
    model: "模型",
    embedding: "Embedding",
    listener: "监听器",
    "event-transport": "事件传输",
    "retry-policy": "重试",
    context: "上下文",
    compaction: "压缩"
  };
  return labels[kind] ?? kind;
}

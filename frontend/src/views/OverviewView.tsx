import {
  Activity,
  Boxes,
  Clock3,
  Cpu,
  PackageCheck,
  Play,
  RefreshCw,
  Server,
  ShieldCheck,
  Wrench
} from "lucide-react";

import type {
  ApprovalInfo,
  InstalledPlugin,
  RuntimeStatus
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../lib/format";

interface OverviewViewProps {
  runtime: RuntimeStatus | null;
  plugins: InstalledPlugin[];
  approvals: ApprovalInfo[];
  loading: boolean;
  error: string | null;
  onStart: () => Promise<void>;
  onRestart: () => Promise<void>;
  onNotify: (message: string, tone?: "success" | "error" | "info") => void;
}

export function OverviewView({
  runtime,
  plugins,
  approvals,
  loading,
  error,
  onStart,
  onRestart,
  onNotify
}: OverviewViewProps) {
  if (loading && !runtime) {
    return <div className="page-loading">正在连接 Runtime…</div>;
  }

  if (!runtime?.ready) {
    return (
      <div className="stack">
        <section className="notice notice--warning">
          <div>
            <strong>Runtime 尚未就绪</strong>
            <p>{error ?? runtime?.error ?? "启动 Runtime 后才能使用对话和插件能力。"}</p>
          </div>
          <button
            className="button button--primary"
            type="button"
            onClick={() =>
              void onStart()
                .then(() => onNotify("Runtime 已启动", "success"))
                .catch((reason: unknown) =>
                  onNotify(reason instanceof Error ? reason.message : "启动失败", "error")
                )
            }
          >
            <Play size={16} />
            启动 Runtime
          </button>
        </section>
      </div>
    );
  }

  const snapshot = runtime.snapshot;
  const activeModel = snapshot?.models.find((model) => model.active);
  const activeMcp = snapshot?.mcp.filter((item) => item.status === "loaded").length ?? 0;
  const externalPlugins = plugins.filter((plugin) => plugin.enabled).length;
  const runtimePlugins = snapshot?.plugins ?? [];

  return (
    <div className="stack">
      <section className="overview-hero">
        <div className="overview-hero__copy">
          <div className="overview-hero__status">
            <StatusPill value="active" label="Runtime 在线" />
            <span>Session {runtime.sessionId ?? "-"}</span>
          </div>
          <h2>固定 Loop，能力由插件装配</h2>
          <p>
            当前模型、工具、MCP、Skill、记忆和事件总线均已进入运行对象图。
          </p>
        </div>
        <div className="overview-hero__actions">
          <button
            className="button button--secondary"
            type="button"
            onClick={() =>
              void onRestart()
                .then(() => onNotify("Runtime 已重新装配", "success"))
                .catch((reason: unknown) =>
                  onNotify(reason instanceof Error ? reason.message : "重启失败", "error")
                )
            }
          >
            <RefreshCw size={16} />
            重新装配
          </button>
        </div>
      </section>

      <section className="metric-grid">
        <MetricTile icon={Boxes} label="插件能力" value={runtimePlugins.length} hint="built-in + external" />
        <MetricTile icon={Wrench} label="可用工具" value={snapshot?.tools.length ?? 0} hint="模型可见" />
        <MetricTile
          icon={Cpu}
          label="活动模型"
          value={activeModel?.name ?? "-"}
          hint={activeModel?.status ?? "idle"}
        />
        <MetricTile
          icon={Activity}
          label="已挂载 MCP"
          value={activeMcp}
          hint={`${snapshot?.mcp.length ?? 0} 个可挂载`}
        />
        <MetricTile icon={PackageCheck} label="外部启用" value={externalPlugins} hint="registry" />
      </section>

      <div className="split-grid">
        <section className="panel">
          <div className="panel__header">
            <div>
              <span className="section-kicker">Runtime</span>
              <h3>运行状态</h3>
            </div>
            <Server size={18} />
          </div>
          <dl className="detail-list">
            <div>
              <dt>启动时间</dt>
              <dd>{formatDateTime(runtime.startedAt)}</dd>
            </div>
            <div>
              <dt>Session</dt>
              <dd>{runtime.sessionId ?? "-"}</dd>
            </div>
            <div>
              <dt>当前状态</dt>
              <dd>
                <StatusPill value={runtime.busy ? "busy" : "active"} label={runtime.busy ? "处理中" : "空闲"} />
              </dd>
            </div>
            <div>
              <dt>待审批</dt>
              <dd>{approvals.length}</dd>
            </div>
          </dl>
        </section>

        <section className="panel">
          <div className="panel__header">
            <div>
              <span className="section-kicker">Models</span>
              <h3>模型资源</h3>
            </div>
            <Cpu size={18} />
          </div>
          <div className="compact-list">
            {snapshot?.models.map((model) => (
              <div className="compact-row" key={model.name}>
                <div className="compact-row__main">
                  <strong>{model.name}</strong>
                  <span>{model.active ? "当前路由目标" : "备用提供方"}</span>
                </div>
                <StatusPill value={model.status} label={model.active ? "active" : model.status} />
              </div>
            ))}
          </div>
        </section>
      </div>

      <div className="split-grid">
        <section className="panel">
          <div className="panel__header">
            <div>
              <span className="section-kicker">MCP</span>
              <h3>网关连接</h3>
            </div>
            <Clock3 size={18} />
          </div>
          <div className="compact-list">
            {snapshot?.mcp.map((item) => (
              <div className="compact-row" key={item.name}>
                <div className="compact-row__main">
                  <strong>{item.name}</strong>
                  <span>{item.status === "loaded" ? "已连接并注册工具" : "等待按需挂载"}</span>
                </div>
                <StatusPill value={item.status} />
              </div>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel__header">
            <div>
              <span className="section-kicker">Safety</span>
              <h3>审批队列</h3>
            </div>
            <ShieldCheck size={18} />
          </div>
          {approvals.length ? (
            <div className="compact-list">
              {approvals.map((approval) => (
                <div className="compact-row" key={approval.id}>
                  <div className="compact-row__main">
                    <strong>{approval.name}</strong>
                    <span>{approval.kind}</span>
                  </div>
                  <span className="muted">{formatDateTime(approval.created_at)}</span>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState icon={ShieldCheck} title="没有待处理审批" />
          )}
        </section>
      </div>

      <section className="panel">
        <div className="panel__header">
          <div>
            <span className="section-kicker">Registry</span>
            <h3>外部插件</h3>
          </div>
          <PackageCheck size={18} />
        </div>
        {plugins.length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>插件</th>
                  <th>版本</th>
                  <th>期望状态</th>
                  <th>运行状态</th>
                  <th>贡献</th>
                </tr>
              </thead>
              <tbody>
                {plugins.slice(0, 8).map((plugin) => (
                  <tr key={plugin.name}>
                    <td>
                      <strong>{plugin.name}</strong>
                    </td>
                    <td>{plugin.version || "-"}</td>
                    <td>
                      <StatusPill value={plugin.enabled ? "enabled" : "disabled"} />
                    </td>
                    <td>
                      <StatusPill value={plugin.runtimeStatus} />
                    </td>
                    <td>{plugin.contributions.join(", ") || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={PackageCheck} title="尚未安装外部插件" detail="在插件页上传功能包。" />
        )}
      </section>
    </div>
  );
}

interface MetricTileProps {
  icon: typeof Boxes;
  label: string;
  value: string | number;
  hint: string;
}

function MetricTile({ icon: Icon, label, value, hint }: MetricTileProps) {
  return (
    <article className="metric-tile">
      <div className="metric-tile__icon">
        <Icon size={18} />
      </div>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{hint}</small>
    </article>
  );
}

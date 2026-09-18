import {
  Boxes,
  History,
  LayoutDashboard,
  MessageSquareText,
  Plug,
  RefreshCw
} from "lucide-react";
import type { ReactNode } from "react";

import type { RuntimeStatus, ViewId } from "../api/types";
import { StatusPill } from "./StatusPill";

interface AppShellProps {
  view: ViewId;
  onViewChange: (view: ViewId) => void;
  runtime: RuntimeStatus | null;
  refreshing: boolean;
  onRefresh: () => Promise<void>;
  children: ReactNode;
}

const NAV_ITEMS = [
  { id: "overview" as const, label: "概览", icon: LayoutDashboard },
  { id: "plugins" as const, label: "插件", icon: Plug },
  { id: "chat" as const, label: "对话", icon: MessageSquareText },
  { id: "capabilities" as const, label: "能力", icon: Boxes },
  { id: "activity" as const, label: "活动", icon: History }
];

const TITLES: Record<ViewId, { title: string; eyebrow: string }> = {
  overview: { title: "运行概览", eyebrow: "Harness" },
  plugins: { title: "插件管理", eyebrow: "Registry" },
  chat: { title: "Agent 对话", eyebrow: "Session" },
  capabilities: { title: "能力目录", eyebrow: "Runtime" },
  activity: { title: "工作区活动", eyebrow: "Trace" }
};

export function AppShell({
  view,
  onViewChange,
  runtime,
  refreshing,
  onRefresh,
  children
}: AppShellProps) {
  const runtimeState = runtime?.ready ? "active" : runtime?.error ? "error" : "idle";
  const title = TITLES[view];

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand__mark">H</div>
          <div>
            <strong>Harness</strong>
            <span>Agent Console</span>
          </div>
        </div>

        <nav className="sidebar__nav" aria-label="主导航">
          {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className={`nav-item ${view === id ? "is-active" : ""}`}
              onClick={() => onViewChange(id)}
            >
              <Icon size={18} />
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar__footer">
          <div className="sidebar__runtime">
            <span className="sidebar__runtime-label">Runtime</span>
            <StatusPill value={runtimeState} label={runtime?.ready ? "在线" : "未就绪"} />
          </div>
          <div className="sidebar__session">
            <span>Session</span>
            <strong>{runtime?.sessionId ?? "-"}</strong>
          </div>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div>
            <span className="topbar__eyebrow">{title.eyebrow}</span>
            <h1>{title.title}</h1>
          </div>
          <div className="topbar__actions">
            <StatusPill
              value={runtime?.busy ? "busy" : runtimeState}
              label={runtime?.busy ? "运行中" : runtime?.ready ? "Runtime 在线" : "Runtime 离线"}
            />
            <button
              className="icon-button"
              type="button"
              title="刷新状态"
              onClick={() => void onRefresh()}
              disabled={refreshing}
            >
              <RefreshCw size={18} className={refreshing ? "spin" : ""} />
            </button>
          </div>
        </header>

        <main className="workspace__content">{children}</main>
      </div>

      <nav className="mobile-nav" aria-label="移动端导航">
        {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className={view === id ? "is-active" : ""}
            onClick={() => onViewChange(id)}
          >
            <Icon size={18} />
            <span>{label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}

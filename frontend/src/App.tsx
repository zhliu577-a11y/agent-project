import { useCallback, useEffect, useState } from "react";

import { ApprovalDrawer } from "./components/ApprovalDrawer";
import { AppShell } from "./components/AppShell";
import { ToastRegion, type ToastItem } from "./components/ToastRegion";
import type { ViewId } from "./api/types";
import { useHarness } from "./hooks/useHarness";
import { ActivityView } from "./views/ActivityView";
import { CapabilitiesView } from "./views/CapabilitiesView";
import { ChatView } from "./views/ChatView";
import { OverviewView } from "./views/OverviewView";
import { PluginsView } from "./views/PluginsView";

function parseView(): ViewId {
  const value = window.location.hash.replace(/^#\/?/, "");
  return ["overview", "plugins", "chat", "capabilities", "activity"].includes(value)
    ? (value as ViewId)
    : "overview";
}

export function App() {
  const harness = useHarness();
  const [view, setView] = useState<ViewId>(parseView);
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const notify = useCallback(
    (message: string, tone: "success" | "error" | "info" = "info") => {
      const id = Date.now() + Math.floor(Math.random() * 1000);
      setToasts((current) => [...current, { id, message, tone }]);
      window.setTimeout(() => {
        setToasts((current) => current.filter((item) => item.id !== id));
      }, 4200);
    },
    []
  );

  const changeView = useCallback((next: ViewId) => {
    setView(next);
    window.location.hash = `/${next}`;
  }, []);

  useEffect(() => {
    const onHashChange = () => setView(parseView());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return (
    <>
      <AppShell
        view={view}
        onViewChange={changeView}
        runtime={harness.runtime}
        refreshing={harness.refreshing}
        onRefresh={() => harness.refresh()}
      >
        {view === "overview" ? (
          <OverviewView
            runtime={harness.runtime}
            plugins={harness.plugins}
            approvals={harness.approvals}
            loading={harness.loading}
            error={harness.error}
            onStart={harness.start}
            onRestart={harness.restart}
            onNotify={notify}
          />
        ) : null}
        {view === "plugins" ? (
          <PluginsView
            runtime={harness.runtime}
            plugins={harness.plugins}
            onRefresh={harness.refresh}
            onRestart={harness.restart}
            onNotify={notify}
          />
        ) : null}
        {view === "chat" ? (
          <ChatView
            runtime={harness.runtime}
            approvals={harness.approvals}
            onRefresh={harness.refresh}
            onNotify={notify}
          />
        ) : null}
        {view === "capabilities" ? (
          <CapabilitiesView
            runtime={harness.runtime}
            onRefresh={harness.refresh}
            onNotify={notify}
          />
        ) : null}
        {view === "activity" ? <ActivityView runtime={harness.runtime} /> : null}
      </AppShell>
      <ApprovalDrawer
        approvals={harness.approvals}
        onResolved={() => harness.refresh(true)}
        onNotify={notify}
      />
      <ToastRegion
        items={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((item) => item.id !== id))}
      />
    </>
  );
}

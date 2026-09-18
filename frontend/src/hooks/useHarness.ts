import { useCallback, useEffect, useRef, useState } from "react";

import {
  getApprovals,
  getPlugins,
  getRuntime,
  restartRuntime,
  startRuntime
} from "../api/client";
import type { ApprovalInfo, InstalledPlugin, RuntimeStatus } from "../api/types";

export interface HarnessState {
  runtime: RuntimeStatus | null;
  plugins: InstalledPlugin[];
  approvals: ApprovalInfo[];
  loading: boolean;
  refreshing: boolean;
  error: string | null;
}

export function useHarness() {
  const [state, setState] = useState<HarnessState>({
    runtime: null,
    plugins: [],
    approvals: [],
    loading: true,
    refreshing: false,
    error: null
  });
  const mounted = useRef(true);

  const refresh = useCallback(async (silent = false) => {
    setState((current) => ({
      ...current,
      refreshing: !silent,
      error: null
    }));
    try {
      const [runtime, plugins, approvals] = await Promise.all([
        getRuntime(),
        getPlugins(),
        getApprovals()
      ]);
      if (!mounted.current) {
        return;
      }
      setState({
        runtime,
        plugins,
        approvals,
        loading: false,
        refreshing: false,
        error: null
      });
    } catch (error) {
      if (!mounted.current) {
        return;
      }
      setState((current) => ({
        ...current,
        loading: false,
        refreshing: false,
        error: error instanceof Error ? error.message : "无法连接后端 API"
      }));
    }
  }, []);

  const start = useCallback(async () => {
    await startRuntime();
    await refresh(true);
  }, [refresh]);

  const restart = useCallback(async () => {
    await restartRuntime();
    await refresh(true);
  }, [refresh]);

  useEffect(() => {
    mounted.current = true;
    void refresh(true);
    const timer = window.setInterval(() => {
      void refresh(true);
    }, 8000);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  return {
    ...state,
    refresh,
    start,
    restart
  };
}

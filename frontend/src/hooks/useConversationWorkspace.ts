import { useCallback, useEffect, useRef, useState } from "react";

import {
  createSession,
  deleteSession,
  getSession,
  getSessions,
  streamChat
} from "../api/client";
import type {
  ApprovalInfo,
  ChatMessage,
  RuntimeEvent,
  RuntimeStatus,
  SessionDetail,
  SessionSummary
} from "../api/types";

const ACTIVE_SESSION_KEY = "harness.activeSessionId";

interface UseConversationWorkspaceOptions {
  runtime: RuntimeStatus | null;
  onRefresh: (silent?: boolean) => Promise<void>;
  onNotify: (message: string, tone?: "success" | "error" | "info") => void;
}

export function useConversationWorkspace({
  runtime,
  onRefresh,
  onNotify
}: UseConversationWorkspaceOptions) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [activities, setActivities] = useState<RuntimeEvent[]>([]);
  const [approvalHint, setApprovalHint] = useState<ApprovalInfo | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const initializedRuntimeRef = useRef<string | null>(null);
  const initializationGenerationRef = useRef(0);
  const historyRequestRef = useRef(0);

  const rememberSession = useCallback((sessionId: string | null) => {
    setActiveSessionId(sessionId);
    if (sessionId) {
      window.localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    } else {
      window.localStorage.removeItem(ACTIVE_SESSION_KEY);
    }
  }, []);

  const loadSession = useCallback(
    async (sessionId: string) => {
      const requestId = ++historyRequestRef.current;
      setHistoryLoading(true);
      try {
        const detail = await getSession(sessionId);
        if (requestId !== historyRequestRef.current) {
          return;
        }
        rememberSession(detail.id);
        setMessages(toChatMessages(detail));
        setActivities([]);
        setApprovalHint(null);
      } finally {
        if (requestId === historyRequestRef.current) {
          setHistoryLoading(false);
        }
      }
    },
    [rememberSession]
  );

  const refreshSessions = useCallback(async () => {
    const items = await getSessions();
    setSessions(items);
    return items;
  }, []);

  useEffect(() => {
    if (!runtime?.ready) {
      return;
    }
    const runtimeKey = `${runtime.startedAt ?? "started"}:${runtime.sessionId ?? "session"}`;
    if (initializedRuntimeRef.current === runtimeKey) {
      return;
    }
    initializedRuntimeRef.current = runtimeKey;
    const generation = ++initializationGenerationRef.current;
    void (async () => {
      setSessionsLoading(true);
      try {
        let items = await getSessions();
        if (!items.length) {
          const created = await createSession();
          items = [created];
        }
        if (generation !== initializationGenerationRef.current) {
          return;
        }
        setSessions(items);
        const remembered = window.localStorage.getItem(ACTIVE_SESSION_KEY);
        const selected =
          items.find((item) => item.id === remembered) ??
          items.find((item) => item.active) ??
          items[0];
        await loadSession(selected.id);
      } catch (error) {
        if (generation === initializationGenerationRef.current) {
          onNotify(
            error instanceof Error ? error.message : "Failed to load conversations",
            "error"
          );
        }
      } finally {
        if (generation === initializationGenerationRef.current) {
          setSessionsLoading(false);
        }
      }
    })();
  }, [loadSession, onNotify, runtime?.ready, runtime?.sessionId, runtime?.startedAt]);

  const createNewSession = useCallback(async () => {
    if (streaming) {
      return;
    }
    setHistoryLoading(true);
    try {
      const created = await createSession();
      setSessions((current) => [created, ...current]);
      rememberSession(created.id);
      setMessages([]);
      setActivities([]);
      setApprovalHint(null);
      setInput("");
    } catch (error) {
      onNotify(
        error instanceof Error ? error.message : "Failed to create conversation",
        "error"
      );
    } finally {
      setHistoryLoading(false);
    }
  }, [onNotify, rememberSession, streaming]);

  const selectSession = useCallback(
    async (sessionId: string) => {
      if (streaming || historyLoading || sessionId === activeSessionId) {
        return;
      }
      try {
        await loadSession(sessionId);
      } catch (error) {
        onNotify(
          error instanceof Error ? error.message : "Failed to load conversation",
          "error"
        );
      }
    },
    [activeSessionId, historyLoading, loadSession, onNotify, streaming]
  );

  const removeSession = useCallback(
    async (sessionId: string) => {
      if (streaming) {
        return;
      }
      try {
        const result = await deleteSession(sessionId);
        const items = await getSessions();
        setSessions(items);
        if (sessionId === activeSessionId) {
          await loadSession(result.activeSessionId);
        }
      } catch (error) {
        onNotify(
          error instanceof Error ? error.message : "Failed to delete conversation",
          "error"
        );
      }
    },
    [activeSessionId, loadSession, onNotify, streaming]
  );

  const submit = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming || !runtime?.ready || historyLoading) {
      return;
    }

    let sessionId = activeSessionId;
    if (!sessionId) {
      try {
        const created = await createSession();
        sessionId = created.id;
        setSessions((current) => [created, ...current]);
        rememberSession(created.id);
      } catch (error) {
        onNotify(
          error instanceof Error ? error.message : "Failed to create conversation",
          "error"
        );
        return;
      }
    }

    const userId = `user-${Date.now()}`;
    const assistantId = `assistant-${Date.now()}`;
    setMessages((current) => [
      ...current,
      { id: userId, role: "user", content: text },
      { id: assistantId, role: "assistant", content: "", streaming: true }
    ]);
    setInput("");
    setStreaming(true);
    setActivities([]);
    setApprovalHint(null);

    const controller = new AbortController();
    abortRef.current = controller;
    let streamedContent = "";

    try {
      await streamChat(
        text,
        {
          onToken: (delta) => {
            streamedContent += delta;
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, content: streamedContent }
                  : message
              )
            );
          },
          onRuntime: (event) => {
            setActivities((current) => [...current.slice(-49), event]);
          },
          onApproval: (approval) => {
            setApprovalHint(approval);
            void onRefresh(true);
          },
          onDone: (result) => {
            const errorMessage = result.error ? formatTurnError(result.error) : "";
            const finalContent =
              result.content ||
              streamedContent ||
              errorMessage ||
              "The model returned no response.";
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, content: finalContent, streaming: false }
                  : message
              )
            );
            if (result.error) {
              onNotify(errorMessage || "The turn ended with an error.", "error");
            }
          },
          onError: (error) => {
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, content: error.message, streaming: false }
                  : message
              )
            );
          }
        },
        {
          sessionId,
          signal: controller.signal
        }
      );
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        const message = error instanceof Error ? error.message : "Conversation failed";
        setMessages((current) =>
          current.map((item) =>
            item.id === assistantId
              ? { ...item, content: message, streaming: false }
              : item
          )
        );
        onNotify(message, "error");
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId ? { ...message, streaming: false } : message
        )
      );
      await onRefresh(true);
      try {
        await refreshSessions();
      } catch {
        // Conversation content is already visible; history refresh can retry later.
      }
    }
  }, [
    activeSessionId,
    historyLoading,
    input,
    onNotify,
    onRefresh,
    refreshSessions,
    rememberSession,
    runtime?.ready,
    streaming
  ]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
  }, []);

  return {
    sessions,
    activeSessionId,
    messages,
    input,
    setInput,
    streaming,
    sessionsLoading,
    historyLoading,
    activities,
    approvalHint,
    submit,
    stop,
    createNewSession,
    selectSession,
    removeSession
  };
}

function toChatMessages(detail: SessionDetail): ChatMessage[] {
  return detail.messages
    .filter(
      (message) =>
        (message.role === "user" || message.role === "assistant") &&
        Boolean(message.content?.trim())
    )
    .map((message) => ({
      id: message.id || `${message.role}-${message.timestamp ?? Math.random()}`,
      role: message.role,
      content: message.content
    }));
}

function formatTurnError(error: Record<string, unknown>): string {
  const message = error.message;
  if (typeof message === "string" && message.trim()) {
    return message;
  }
  const category = error.category;
  if (typeof category === "string" && category.trim()) {
    return `Model request failed (${category}).`;
  }
  try {
    return JSON.stringify(error);
  } catch {
    return "Model request failed.";
  }
}

import {
  Bot,
  Clock3,
  CornerDownLeft,
  MessageSquareText,
  Send,
  ShieldAlert,
  Square,
  User,
  Wrench
} from "lucide-react";
import { useRef, useState } from "react";

import { streamChat } from "../api/client";
import type {
  ApprovalInfo,
  ChatMessage,
  RuntimeEvent,
  RuntimeStatus
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime, truncate } from "../lib/format";

interface ChatViewProps {
  runtime: RuntimeStatus | null;
  approvals: ApprovalInfo[];
  onRefresh: (silent?: boolean) => Promise<void>;
  onNotify: (message: string, tone?: "success" | "error" | "info") => void;
}

export function ChatView({
  runtime,
  approvals,
  onRefresh,
  onNotify
}: ChatViewProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [activities, setActivities] = useState<RuntimeEvent[]>([]);
  const [approvalHint, setApprovalHint] = useState<ApprovalInfo | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const submit = async () => {
    const text = input.trim();
    if (!text || streaming || !runtime?.ready) {
      return;
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
            const finalContent = result.content || streamedContent || "本轮没有文本输出。";
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, content: finalContent, streaming: false }
                  : message
              )
            );
            if (result.error) {
              onNotify("本轮以错误状态结束，请查看运行轨迹。", "error");
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
        controller.signal
      );
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        const message = error instanceof Error ? error.message : "对话失败";
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
    }
  };

  const stop = () => {
    abortRef.current?.abort();
    setStreaming(false);
  };

  return (
    <div className="chat-layout">
      <section className="chat-main">
        <div className="chat-toolbar">
          <div className="chat-toolbar__session">
            <StatusPill
              value={runtime?.ready ? "active" : "idle"}
              label={runtime?.ready ? "Session 在线" : "Runtime 离线"}
            />
            <span>{runtime?.sessionId ?? "-"}</span>
          </div>
          <span className="muted">{messages.length} 条消息</span>
        </div>

        <div className="message-list">
          {!messages.length ? (
            <EmptyState
              icon={MessageSquareText}
              title="当前 Session 尚无消息"
              detail="输入内容后，模型、工具和审批事件会显示在这里。"
            />
          ) : null}

          {messages.map((message) => (
            <article className={`message message--${message.role}`} key={message.id}>
              <div className="message__avatar">
                {message.role === "user" ? <User size={17} /> : <Bot size={17} />}
              </div>
              <div className="message__body">
                <div className="message__meta">
                  <strong>{message.role === "user" ? "你" : "Agent"}</strong>
                  {message.streaming ? <span className="streaming-label">streaming</span> : null}
                </div>
                <div className="message__content">
                  {message.content || (message.streaming ? <span className="typing-dots">...</span> : "")}
                </div>
              </div>
            </article>
          ))}
        </div>

        <div className="composer">
          {approvalHint ? (
            <div className="composer__approval">
              <ShieldAlert size={16} />
              <span>
                {approvalHint.kind} / {approvalHint.name} 等待审批
              </span>
            </div>
          ) : null}
          <div className="composer__input">
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submit();
                }
              }}
              placeholder="输入消息"
              rows={3}
              disabled={!runtime?.ready}
            />
            <div className="composer__meta">
              <span>
                <CornerDownLeft size={14} />
                Enter 发送，Shift + Enter 换行
              </span>
              {streaming ? (
                <button className="button button--secondary" type="button" onClick={stop}>
                  <Square size={14} />
                  停止
                </button>
              ) : (
                <button
                  className="button button--primary"
                  type="button"
                  disabled={!input.trim() || !runtime?.ready}
                  onClick={() => void submit()}
                >
                  <Send size={15} />
                  发送
                </button>
              )}
            </div>
          </div>
        </div>
      </section>

      <aside className="chat-inspector">
        <section className="inspector-section">
          <div className="inspector-section__header">
            <div>
              <span className="section-kicker">Live</span>
              <h3>运行轨迹</h3>
            </div>
            <Clock3 size={17} />
          </div>
          {activities.length ? (
            <div className="activity-list">
              {activities.map((activity) => (
                <div className="activity-row" key={activity.eventId}>
                  <span className="activity-row__dot" />
                  <div>
                    <strong>{activity.name}</strong>
                    <span>{truncate(JSON.stringify(activity.payload), 70)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="inspector-empty">尚未收到运行事件。</p>
          )}
        </section>

        <section className="inspector-section">
          <div className="inspector-section__header">
            <div>
              <span className="section-kicker">Approval</span>
              <h3>审批</h3>
            </div>
            <ShieldAlert size={17} />
          </div>
          {approvals.length ? (
            <div className="approval-mini-list">
              {approvals.map((approval) => (
                <div className="approval-mini" key={approval.id}>
                  <strong>{approval.name}</strong>
                  <span>{approval.kind}</span>
                  <small>{formatDateTime(approval.created_at)}</small>
                </div>
              ))}
            </div>
          ) : (
            <p className="inspector-empty">没有待处理审批。</p>
          )}
        </section>

        <section className="inspector-section">
          <div className="inspector-section__header">
            <div>
              <span className="section-kicker">Tools</span>
              <h3>可用工具</h3>
            </div>
            <Wrench size={17} />
          </div>
          <div className="tool-cloud">
            {(runtime?.snapshot?.tools ?? []).map((tool) => (
              <span key={tool}>{tool}</span>
            ))}
          </div>
        </section>
      </aside>
    </div>
  );
}

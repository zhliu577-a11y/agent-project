import {
  Bot,
  Clock3,
  CornerDownLeft,
  MessageSquareText,
  Plus,
  Send,
  ShieldAlert,
  Square,
  Trash2,
  User,
  Wrench
} from "lucide-react";
import { useEffect, useRef } from "react";

import type {
  ApprovalInfo,
  ChatMessage,
  RuntimeEvent,
  RuntimeStatus,
  SessionSummary
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime, truncate } from "../lib/format";

interface ChatViewProps {
  runtime: RuntimeStatus | null;
  approvals: ApprovalInfo[];
  sessions: SessionSummary[];
  activeSessionId: string | null;
  messages: ChatMessage[];
  input: string;
  streaming: boolean;
  sessionsLoading: boolean;
  historyLoading: boolean;
  activities: RuntimeEvent[];
  approvalHint: ApprovalInfo | null;
  onInputChange: (value: string) => void;
  onSubmit: () => Promise<void>;
  onStop: () => void;
  onCreateSession: () => Promise<void>;
  onSelectSession: (sessionId: string) => Promise<void>;
  onDeleteSession: (sessionId: string) => Promise<void>;
}

export function ChatView({
  runtime,
  approvals,
  sessions,
  activeSessionId,
  messages,
  input,
  streaming,
  sessionsLoading,
  historyLoading,
  activities,
  approvalHint,
  onInputChange,
  onSubmit,
  onStop,
  onCreateSession,
  onSelectSession,
  onDeleteSession
}: ChatViewProps) {
  const messageEndRef = useRef<HTMLDivElement | null>(null);
  const activeSession = sessions.find((session) => session.id === activeSessionId);
  const controlsLocked = streaming || historyLoading || !runtime?.ready;

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, streaming]);

  const remove = (session: SessionSummary) => {
    if (window.confirm(`确认删除对话“${session.title || "新对话"}”吗？`)) {
      void onDeleteSession(session.id);
    }
  };

  return (
    <div className="chat-layout">
      <aside className="chat-sessions">
        <div className="chat-sessions__header">
          <div>
            <span className="section-kicker">History</span>
            <h3>对话历史</h3>
          </div>
          <button
            className="icon-button"
            type="button"
            title="新建对话"
            onClick={() => void onCreateSession()}
            disabled={controlsLocked}
          >
            <Plus size={17} />
          </button>
        </div>

        <div className="chat-sessions__list">
          {sessionsLoading ? <p className="inspector-empty">正在加载对话...</p> : null}
          {!sessionsLoading && !sessions.length ? (
            <p className="inspector-empty">暂无对话</p>
          ) : null}
          {sessions.map((session) => (
            <div
              className={`chat-session ${
                session.id === activeSessionId ? "is-active" : ""
              }`}
              key={session.id}
            >
              <button
                className="chat-session__main"
                type="button"
                onClick={() => void onSelectSession(session.id)}
                disabled={controlsLocked}
                aria-current={session.id === activeSessionId ? "page" : undefined}
              >
                <strong>{session.title || "新对话"}</strong>
                <span>
                  {session.messageCount} 条消息 / {formatDateTime(session.updatedAt)}
                </span>
              </button>
              <button
                className="chat-session__delete"
                type="button"
                title="删除对话"
                onClick={() => remove(session)}
                disabled={controlsLocked}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      </aside>

      <section className="chat-main">
        <div className="chat-toolbar">
          <div className="chat-toolbar__session">
            <StatusPill
              value={runtime?.ready ? "active" : "idle"}
              label={runtime?.ready ? "Session 在线" : "Runtime 离线"}
            />
            <span>{activeSession?.title || activeSessionId || "-"}</span>
          </div>
          <span className="muted">{messages.length} 条消息</span>
        </div>

        <div className="message-list" aria-busy={historyLoading}>
          {historyLoading ? (
            <p className="inspector-empty">正在加载对话...</p>
          ) : null}
          {!historyLoading && !messages.length ? (
            <EmptyState
              icon={MessageSquareText}
              title="新建的对话尚无消息"
              detail="消息、工具和审批事件会保存在本地会话历史中。"
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
                  {message.content ||
                    (message.streaming ? <span className="typing-dots">...</span> : "")}
                </div>
              </div>
            </article>
          ))}
          <div ref={messageEndRef} />
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
              onChange={(event) => onInputChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void onSubmit();
                }
              }}
              placeholder="输入消息"
              rows={3}
              disabled={controlsLocked}
            />
            <div className="composer__meta">
              <span>
                <CornerDownLeft size={14} />
                Enter 发送，Shift + Enter 换行
              </span>
              {streaming ? (
                <button className="button button--secondary" type="button" onClick={onStop}>
                  <Square size={14} />
                  停止
                </button>
              ) : (
                <button
                  className="button button--primary"
                  type="button"
                  disabled={!input.trim() || controlsLocked}
                  onClick={() => void onSubmit()}
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
            <p className="inspector-empty">本轮尚未收到运行事件。</p>
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

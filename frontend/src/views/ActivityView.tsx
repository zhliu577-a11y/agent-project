import { Activity, RefreshCw, Search, TerminalSquare } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getActivityEvents } from "../api/client";
import type { ActivityEvent, RuntimeStatus } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { StatusPill } from "../components/StatusPill";
import { formatDateTime } from "../lib/format";

interface ActivityViewProps {
  runtime: RuntimeStatus | null;
}

const POLL_INTERVAL_MS = 2500;
const PAGE_LIMIT = 250;
const MAX_VISIBLE_EVENTS = 500;

export function ActivityView({ runtime }: ActivityViewProps) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [eventName, setEventName] = useState("");
  const [publisher, setPublisher] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cursor = useRef(0);

  const load = useCallback(
    async (reset: boolean) => {
      if (reset) {
        cursor.current = 0;
        setLoading(true);
      } else {
        setRefreshing(true);
      }
      try {
        const page = await getActivityEvents({
          after: reset ? 0 : cursor.current,
          limit: PAGE_LIMIT,
          name: eventName.trim() || undefined,
          publisher: publisher.trim() || undefined,
          sessionId: sessionId.trim() || undefined
        });
        setEvents((current) => {
          const next = reset ? page.events : mergeEvents(current, page.events);
          return next.slice(-MAX_VISIBLE_EVENTS);
        });
        cursor.current = Math.max(cursor.current, page.latestSeq);
        setError(null);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "无法读取活动记录");
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [eventName, publisher, sessionId]
  );

  useEffect(() => {
    void load(true);
    const timer = window.setInterval(() => {
      void load(false);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const sessionHint = useMemo(
    () => sessionId || runtime?.sessionId || "",
    [runtime?.sessionId, sessionId]
  );

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel__header panel__header--wrap">
          <div>
            <span className="section-kicker">Event Journal</span>
            <h3>谁在什么时候做了什么</h3>
          </div>
          <button
            className="button button--secondary"
            type="button"
            disabled={refreshing}
            onClick={() => void load(true)}
          >
            <RefreshCw size={16} className={refreshing ? "spin" : ""} />
            刷新
          </button>
        </div>

        <div className="activity-filters">
          <label className="search-field">
            <Search size={16} />
            <input
              value={eventName}
              onChange={(event) => setEventName(event.target.value)}
              placeholder="事件名"
            />
          </label>
          <label className="search-field">
            <TerminalSquare size={16} />
            <input
              value={publisher}
              onChange={(event) => setPublisher(event.target.value)}
              placeholder="发布者"
            />
          </label>
          <label className="search-field">
            <Activity size={16} />
            <input
              value={sessionId}
              onChange={(event) => setSessionId(event.target.value)}
              placeholder={sessionHint || "Session"}
            />
          </label>
        </div>

        {loading && !events.length ? (
          <div className="page-loading">正在读取活动...</div>
        ) : events.length ? (
          <div className="event-timeline">
            {events
              .slice()
              .reverse()
              .map((event) => (
                <article className="event-row" key={event.seq}>
                  <div className={`event-row__marker event-row__marker--${event.status}`}>
                    <span />
                  </div>
                  <div className="event-row__body">
                    <div className="event-row__topline">
                      <strong>{event.name}</strong>
                      <span>{formatDateTime(event.timestamp)}</span>
                    </div>
                    <div className="event-row__meta">
                      <span>{event.publisher ?? "unknown publisher"}</span>
                      {event.target ? <code>{event.target}</code> : null}
                      <StatusPill value={event.status} />
                    </div>
                    <p>{event.summary}</p>
                    <details className="event-row__details">
                      <summary>详情</summary>
                      <dl>
                        <div>
                          <dt>session</dt>
                          <dd>{event.sessionId ?? "-"}</dd>
                        </div>
                        <div>
                          <dt>run</dt>
                          <dd>{event.runId ?? "-"}</dd>
                        </div>
                        <div>
                          <dt>turn</dt>
                          <dd>{event.turnId ?? "-"}</dd>
                        </div>
                        <div>
                          <dt>trace</dt>
                          <dd>{event.traceId ?? "-"}</dd>
                        </div>
                      </dl>
                      <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                    </details>
                  </div>
                </article>
              ))}
          </div>
        ) : (
          <EmptyState
            icon={Activity}
            title={error ? "活动记录不可用" : "还没有活动记录"}
            detail={error ?? "Runtime 启动和 Agent 操作会出现在这里。"}
          />
        )}
      </section>
    </div>
  );
}

function mergeEvents(current: ActivityEvent[], incoming: ActivityEvent[]): ActivityEvent[] {
  if (!incoming.length) {
    return current;
  }
  const bySequence = new Map(current.map((event) => [event.seq, event]));
  for (const event of incoming) {
    bySequence.set(event.seq, event);
  }
  return [...bySequence.values()].sort((left, right) => left.seq - right.seq);
}

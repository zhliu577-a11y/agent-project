import { ShieldAlert, X } from "lucide-react";

import { resolveApproval } from "../api/client";
import type { ApprovalInfo } from "../api/types";
import { formatDateTime } from "../lib/format";

interface ApprovalDrawerProps {
  approvals: ApprovalInfo[];
  onResolved: () => Promise<void>;
  onNotify: (message: string, tone?: "success" | "error" | "info") => void;
}

export function ApprovalDrawer({ approvals, onResolved, onNotify }: ApprovalDrawerProps) {
  if (!approvals.length) {
    return null;
  }
  const approval = approvals[0];

  const resolve = async (approved: boolean) => {
    try {
      await resolveApproval(approval.id, approved);
      onNotify(approved ? "已批准当前请求" : "已拒绝当前请求", approved ? "success" : "info");
      await onResolved();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "审批操作失败", "error");
    }
  };

  return (
    <aside className="approval-drawer" aria-label="待审批请求">
      <div className="approval-drawer__header">
        <div className="approval-drawer__title">
          <ShieldAlert size={19} />
          <strong>待审批</strong>
        </div>
        {approvals.length > 1 ? <span className="badge">{approvals.length}</span> : null}
      </div>
      <div className="approval-drawer__body">
        <div className="approval-drawer__meta">
          <span>{approval.kind}</span>
          <span>{formatDateTime(approval.created_at)}</span>
        </div>
        <strong className="approval-drawer__name">{approval.name}</strong>
        {approval.resource ? <code>{approval.resource}</code> : null}
        {approval.arguments ? (
          <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
        ) : null}
      </div>
      <div className="approval-drawer__actions">
        <button className="button button--ghost" type="button" onClick={() => void resolve(false)}>
          <X size={16} />
          拒绝
        </button>
        <button className="button button--primary" type="button" onClick={() => void resolve(true)}>
          批准
        </button>
      </div>
    </aside>
  );
}

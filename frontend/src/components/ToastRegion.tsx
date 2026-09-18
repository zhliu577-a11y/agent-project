import { CheckCircle2, Info, X, XCircle } from "lucide-react";

export interface ToastItem {
  id: number;
  tone: "success" | "error" | "info";
  message: string;
}

interface ToastRegionProps {
  items: ToastItem[];
  onDismiss: (id: number) => void;
}

export function ToastRegion({ items, onDismiss }: ToastRegionProps) {
  if (!items.length) {
    return null;
  }
  return (
    <div className="toast-region" aria-live="polite">
      {items.map((item) => {
        const Icon = item.tone === "success" ? CheckCircle2 : item.tone === "error" ? XCircle : Info;
        return (
          <div key={item.id} className={`toast toast--${item.tone}`}>
            <Icon size={17} />
            <span>{item.message}</span>
            <button
              className="icon-button icon-button--compact"
              type="button"
              onClick={() => onDismiss(item.id)}
              title="关闭"
            >
              <X size={15} />
            </button>
          </div>
        );
      })}
    </div>
  );
}

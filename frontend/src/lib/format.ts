export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

export function formatBytes(value: number): string {
  if (!value) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const size = value / 1024 ** index;
  return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

export function truncate(value: string, length = 80): string {
  if (value.length <= length) {
    return value;
  }
  return `${value.slice(0, length - 1)}…`;
}

export function statusTone(status: string): "ok" | "warn" | "danger" | "neutral" {
  const normalized = status.toLowerCase();
  if (["active", "ready", "loaded", "ok", "allow", "enabled"].includes(normalized)) {
    return "ok";
  }
  if (["idle", "stopped", "disabled", "name-only", "ask"].includes(normalized)) {
    return "neutral";
  }
  if (["error", "deny", "failed"].includes(normalized)) {
    return "danger";
  }
  return "warn";
}

export function contributionKind(value: string): string {
  return value.includes(":") ? value.split(":", 1)[0] : value;
}
